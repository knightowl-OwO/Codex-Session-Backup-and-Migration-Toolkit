"""Snapshot graph and byte-accurate relocation of Codex rollout history."""

import hashlib
import json
import re
import shutil
import subprocess
from array import array
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from codex_session_common import DATABASES, LOG_DIRECTORIES, check_database, connect

UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
ROLLOUT = re.compile(rf"rollout-.+?-({UUID})(?:_({UUID}))?\.jsonl(?:\.zst)?")
ROOT_FIELDS = ("workspace_roots", "runtime_workspace_roots")
RECORDS = {"session_meta", "turn_context", "event_msg", "response_item", "compacted"}


def log_bytes(path, data=None):
    if not path.name.endswith(".zst"):
        if data is None:
            return path.read_bytes()
        path.write_bytes(data)
        return
    try:
        from compression import zstd
    except ImportError:
        try:
            import zstandard as zstd
        except ImportError:
            binary = shutil.which("zstd")
            if not binary:
                raise ValueError(f"{path.name}: install zstandard with this Python or install zstd")
            args = ["-dc", str(path)] if data is None else ["-c"]
            proc = subprocess.run([binary, "-q", *args], input=data, capture_output=True)
            if proc.returncode:
                raise ValueError(f"{path.name}: Zstandard failed")
            if data is None:
                return proc.stdout
            path.write_bytes(proc.stdout)
            return
    try:
        if data is None:
            with zstd.open(path, "rb") as src:
                return src.read()
        with zstd.open(path, "wb") as dst:
            dst.write(data)
    except (OSError, zstd.ZstdError) as exc:
        raise ValueError(f"{path.name}: Zstandard stream error: {exc}") from exc


def record_lines(path):
    # splitlines() also splits on control characters other than the JSONL delimiter.
    raw = log_bytes(path)
    start = 0
    for match in re.finditer(b"\n", raw):
        yield raw[start:match.end()]
        start = match.end()
    if start < len(raw):
        yield raw[start:]


def records(path):
    for number, line in enumerate(record_lines(path), 1):
        try:
            data = json.loads(line)
            if data["type"] not in RECORDS or not isinstance(data["payload"], dict):
                raise ValueError("unsupported record type or payload")
            if number == 1 and data["type"] != "session_meta":
                raise ValueError("first record must be session_meta")
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"{path.name}, line {number}: {exc}") from exc
        yield line, data


def text_digest(texts):
    return hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode("utf-8")).digest()


def completed_message(record):
    payload = record["payload"]
    if record["type"] != "event_msg" or payload.get("type") != "item_completed":
        return None
    item = payload["item"]
    kind = item["type"]
    if kind not in ("UserMessage", "AgentMessage"):
        return None
    texts = [part["text"] for part in item["content"] if part["type"] in ("text", "Text")]
    return (payload["turn_id"], item["id"]), text_digest(texts if kind == "UserMessage" else "".join(texts))


def relative_log(value):
    if not isinstance(value, str):
        raise ValueError(f"missing rollout_path: {value!r}")
    path = PureWindowsPath(value) if PureWindowsPath(value).drive else PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid absolute rollout_path: {value!r}")
    roots = [i for i, part in enumerate(path.parts[:-1]) if part in LOG_DIRECTORIES]
    if len(roots) != 1:
        raise ValueError(f"ambiguous or missing session directory: {value!r}")
    parts = path.parts[roots[0]:]
    if any(":" in part or "\\" in part for part in parts):
        raise ValueError(f"incompatible rollout_path: {value!r}")
    return Path(*parts)


def settings(record):
    p = record["payload"]
    if record["type"] in ("session_meta", "turn_context"):
        return p
    if record["type"] == "event_msg" and p.get("type") == "thread_settings_applied":
        if not isinstance(p.get("thread_settings"), dict):
            raise ValueError("unsupported thread_settings_applied record")
        return p["thread_settings"]
    return {}


def workspace_fields(record):
    p = settings(record)
    for key in ("cwd", *ROOT_FIELDS):
        value = p.get(key)
        if value is None:
            continue
        values = [value] if key == "cwd" else value
        if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
            raise ValueError(f"unsupported {key} representation")
        yield key, values


@dataclass
class Segment:
    path: Path
    owner: str
    key: str
    base: dict | None
    start: int
    session: str | None
    turns: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    workspace_events: list = field(default_factory=list)
    saved_settings: list = field(default_factory=list)
    offsets: array = field(default_factory=lambda: array("Q", [0]))
    rewritten: array | None = None

    def offset(self, ordinal, old):
        index = ordinal - self.start
        if not 0 <= index < len(self.offsets) or self.offsets[index] != old:
            raise ValueError(f"{self.owner} ({self.path.name}): offset {old} is not the boundary for ordinal {ordinal}")
        return (self.rewritten if self.rewritten is not None else self.offsets)[index]


@dataclass
class Thread:
    path: Path
    archived: bool
    title: str
    cwd: str
    chain: list
    attached_to: str | None

    def visible_records(self, attribute):
        for i, seg in enumerate(self.chain):
            stop = self.chain[i + 1].start if i + 1 < len(self.chain) else seg.start + len(seg.offsets) - 1
            for ordinal, value in getattr(seg, attribute):
                if ordinal >= stop:
                    break
                yield value

    def recorded_turns(self):
        return list(self.visible_records("turns"))

    def effective_roots(self):
        roots = context = None
        for fields in self.visible_records("workspace_events"):
            roots = fields.get("runtime_workspace_roots", roots)
            context = fields.get("workspace_roots", context)
        return roots if roots is not None else context


class Snapshot:
    def __init__(self, home):
        self.home = home
        self.segments = {}
        self.threads = {}
        self.places = defaultdict(set)
        self.by_key = {}
        for directory in LOG_DIRECTORIES:
            for path in sorted((home / directory).rglob("*")):
                if path.is_dir() and path.resolve() == path:
                    continue
                if not path.is_file() or path.resolve() != path:
                    raise ValueError(f"missing or redirected rollout: {path.name}")
                match = ROLLOUT.fullmatch(path.name)
                if not match:
                    raise ValueError(f"unknown rollout format: {path.name}")
                owner, suffix = match.groups()
                segment = None
                for i, (line, data) in enumerate(records(path)):
                    if i == 0:
                        meta = data["payload"]
                        if meta.get("id") != owner:
                            raise ValueError(f"{owner}: header ID differs in {path.name}")
                        if meta.get("session_id") is not None and (
                            not isinstance(meta["session_id"], str) or not re.fullmatch(UUID, meta["session_id"])
                        ):
                            raise ValueError(f"{owner}: invalid session_id in {path.name}")
                        base = meta.get("history_base")
                        if base is not None and (
                            not isinstance(base, dict) or set(base) != {"thread_id", "end_ordinal_exclusive", "end_byte_offset"}
                            or not isinstance(base["thread_id"], str) or not re.fullmatch(UUID, base["thread_id"])
                            or any(type(base[k]) is not int or base[k] < 0 for k in ("end_ordinal_exclusive", "end_byte_offset"))
                        ):
                            raise ValueError(f"{owner}: unknown history_base in {path.name}")
                        segment = Segment(path.relative_to(home), owner, suffix or owner, base,
                                          base["end_ordinal_exclusive"] if base else 0, meta.get("session_id"))
                    ordinal = segment.start + i
                    if "ordinal" in data and (type(data["ordinal"]) is not int or data["ordinal"] != ordinal):
                        raise ValueError(f"{owner}, {path.name}: expected log ordinal {ordinal}, found {data['ordinal']}")
                    payload = data["payload"]
                    if (data["type"] == "event_msg" and payload.get("type") in ("item_started", "item_completed")
                            and "thread_id" in payload and payload["thread_id"] not in (owner, segment.key)):
                        raise ValueError(f"{owner}, {path.name}: unrelated item thread_id at ordinal {ordinal}")
                    if data["type"] == "event_msg" and data["payload"].get("type") == "task_started":
                        turn = data["payload"].get("turn_id")
                        if not isinstance(turn, str):
                            raise ValueError(f"{owner}, {path.name}: missing turn ID at ordinal {ordinal}")
                        segment.turns.append((ordinal, turn))
                    segment.offsets.append(segment.offsets[-1] + len(line))
                    try:
                        message = completed_message(data)
                        if message:
                            segment.messages.append((ordinal, message))
                        fields = {}
                        for key, values in workspace_fields(data):
                            fields[key] = values
                            for v in values:
                                self.places[v].add((owner, key))
                        if fields:
                            segment.workspace_events.append((ordinal, fields))
                        if data["type"] == "event_msg" and data["payload"].get("type") == "thread_settings_applied":
                            if data["payload"].get("thread_id") != owner:
                                raise ValueError("thread_settings_applied belongs to a different thread")
                            segment.saved_settings.append((ordinal, data))
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ValueError(f"{owner}, {path.name}, line {i + 1}: {exc}") from exc
                if not segment or segment.key in self.by_key:
                    raise ValueError(f"{owner}: empty log or ambiguous segment ID in {path.name}")
                self.segments[segment.path] = self.by_key[segment.key] = segment

        with connect(home / DATABASES[0]) as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(threads)")}
            parent = "attached_thread_parent_id" if "attached_thread_parent_id" in columns else "NULL"
            for tid, path, archived, title, cwd, attached in db.execute(
                f"SELECT id, rollout_path, archived, title, cwd, {parent} FROM threads"
            ):
                try:
                    relative = relative_log(path)
                    if relative not in self.segments and relative.suffix == ".jsonl":
                        compressed = relative.with_suffix(".jsonl.zst")
                        if compressed in self.segments:
                            relative = compressed
                    entry = self.segments.get(relative)
                    if not entry or entry.owner != tid:
                        raise ValueError(f"source SQLite entry has no matching log and header: {relative}")
                    chain = self.chain(entry)
                    if archived not in (0, 1) or not isinstance(cwd, str) or not cwd:
                        raise ValueError("invalid thread archive flag or cwd")
                    self.threads[tid] = Thread(relative, bool(archived), title, cwd, chain, attached)
                    self.places[cwd].add((tid, "threads.cwd"))
                except ValueError as exc:
                    raise ValueError(f"{tid}: {exc}") from exc
        reached = {seg.key for t in self.threads.values() for seg in t.chain}
        unused = self.by_key.keys() - reached
        if unused:
            raise ValueError("Rollouts unreachable from source SQLite entries: " +
                             ", ".join(self.by_key[k].path.name for k in sorted(unused)))
        with connect(home / DATABASES[2]) as db:
            self.catalog = catalog_rows(db)
        for (host, tid), (_, cwd) in self.catalog.items():
            if host == "local" and tid in self.threads and cwd:
                self.places[cwd].add((tid, "catalog.cwd"))

    def chain(self, segment):
        chain = []
        seen = set()
        while True:
            if segment.key in seen:
                raise ValueError(f"{segment.owner}: cycle at segment {segment.key}")
            chain.append(segment)
            seen.add(segment.key)
            base = segment.base
            if not base:
                return list(reversed(chain))
            parent = self.by_key.get(base["thread_id"])
            if base["thread_id"] in seen:
                raise ValueError(f"{segment.owner}: cycle through segment {base['thread_id']}")
            if (not parent or parent.owner != segment.owner
                    or (parent.session and segment.session and parent.session != segment.session)):
                raise ValueError(f"{segment.owner}: missing or unrelated parent segment {base['thread_id']}")
            parent.offset(base["end_ordinal_exclusive"], base["end_byte_offset"])
            segment = parent

    def plan(self):
        return [{"source": v, "available": local_directory(v),
                 "uses": [{"thread": tid, "field": key} for tid, key in sorted(uses)]}
                for v, uses in sorted(self.places.items())]


def local_directory(value):
    path = Path(value)
    return path.is_absolute() and path.is_dir()


def catalog_rows(db):
    if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='local_thread_catalog'").fetchone():
        return {}
    return {(host, tid): (title, cwd) for host, tid, title, cwd in
            db.execute("SELECT host_id, thread_id, display_title, cwd FROM local_thread_catalog")}


class Workspaces:
    def __init__(self, snapshot, supplied, viewing):
        if not isinstance(supplied, dict) or supplied.keys() - snapshot.places.keys():
            raise ValueError("Mapping must be a JSON object keyed by exact source workspace paths from repair --plan")
        self.targets = {}
        self.viewing = viewing
        missing = []
        for v, uses in snapshot.places.items():
            if v in supplied:
                target = supplied[v]
                if target is not None and (not isinstance(target, str) or not local_directory(target)):
                    raise ValueError(f"{v}: mapping target must be an existing absolute local directory or null")
            elif local_directory(v):
                target = v
            else:
                missing.append(f"{v} ({', '.join(sorted({tid for tid, _ in uses}))})")
                continue
            self.targets[v] = target
        if missing:
            raise ValueError("Confirm mappings or choose null for history-only viewing: " + "; ".join(missing))
        if None in self.targets.values():
            viewing.mkdir(exist_ok=True)
            if viewing.resolve() != viewing or any(viewing.iterdir()):
                raise ValueError(f"History viewing requires an empty physical directory: {viewing}")

    def cwd(self, old):
        return self.targets[old] or str(self.viewing)

    def update(self, record):
        p = settings(record)
        changed = False
        for key, values in workspace_fields(record):
            new = self.cwd(values[0]) if key == "cwd" else [self.targets[v] for v in values if self.targets[v] is not None]
            if new != p[key]:
                p[key] = new
                changed = True
        return changed


def replace_json_values(line, replacements):
    """Splice individual JSON values. Keep adjacent fields, escapes, whitespace and EOLs."""
    text = line.decode("utf-8")
    decoder = json.JSONDecoder()
    spaces = re.compile(r"\s*")
    edits = []

    def visit(pos, fields):
        pos = spaces.match(text, pos).end()
        if text[pos] != "{":
            raise ValueError("Expected JSON object in structured metadata")
        pos = spaces.match(text, pos + 1).end()
        while text[pos] != "}":
            name, pos = decoder.raw_decode(text, pos)
            pos = spaces.match(text, pos).end()
            start = spaces.match(text, pos + 1).end()
            _, pos = decoder.raw_decode(text, start)
            if name in fields:
                value = fields[name]
                if isinstance(value, dict):
                    visit(start, value)
                else:
                    edits.append((start, pos, json.dumps(value, ensure_ascii=False)))
            pos = spaces.match(text, pos).end()
            if text[pos] == ",":
                pos = spaces.match(text, pos + 1).end()
        return pos

    visit(0, replacements)
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    return text.encode("utf-8")


def append_workspace_override(thread, target, workspaces):
    """Reuse the client's own full settings. Never synthesize permission or model settings."""
    head = thread.chain[-1]
    if thread.archived or head.path.suffix != ".jsonl" or not head.saved_settings:
        return False
    roots = thread.effective_roots()
    template = head.saved_settings[-1][1]
    saved = settings(template)
    if (roots is None or "ordinal" not in template or "cwd" not in saved
            or "runtime_workspace_roots" not in saved):
        return False
    cwd = workspaces.cwd(thread.cwd)
    mapped = [workspaces.targets[v] for v in roots if workspaces.targets[v] is not None]
    if (saved["cwd"], saved["runtime_workspace_roots"]) == (cwd, mapped):
        return False
    changes = {**saved, "cwd": cwd, "runtime_workspace_roots": mapped}
    payload = {**template["payload"], "thread_settings": changes}
    record = {**template, "ordinal": head.start + len(head.offsets) - 1, "payload": payload}
    with (target / head.path).open("r+b") as out:
        out.seek(-1, 2)
        if out.read(1) != b"\n":
            return False
        out.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
    return True


def rewrite_logs(snapshot, target, workspaces, *, prefer_override=False):
    done = set()
    overrides = set()
    for tid, thread in snapshot.threads.items():
        if prefer_override and append_workspace_override(thread, target, workspaces):
            overrides.add(tid)
            continue
        for seg in thread.chain:
            if seg.key in done:
                continue
            done.add(seg.key)
            new_offsets = array("Q", [0])
            output = bytearray()
            edited = False
            for i, (line, record) in enumerate(records(snapshot.home / seg.path)):
                fields = settings(record)
                before = fields.copy()
                workspaces.update(record)
                patches = {k: v for k, v in fields.items() if before[k] != v}
                if patches and fields is not record["payload"]:
                    patches = {"thread_settings": patches}
                if i == 0 and seg.base:
                    base = record["payload"]["history_base"]
                    parent = snapshot.by_key[base["thread_id"]]
                    offset = parent.offset(seg.start, base["end_byte_offset"])
                    if offset != base["end_byte_offset"]:
                        patches["history_base"] = {"end_byte_offset": offset}
                if patches:
                    line = replace_json_values(line, {"payload": patches})
                    edited = True
                output.extend(line)
                new_offsets.append(len(output))
            if edited:
                log_bytes(target / seg.path, bytes(output))
                seg.rewritten = new_offsets
    return overrides


def translate(segment, ordinal, offset, end=False):
    if offset is None:
        return None
    if type(ordinal) is not int or type(offset) is not int or ordinal < 0:
        raise ValueError(f"{segment.path.name}: invalid indexed ordinal or offset {ordinal}, {offset}")
    # Turn ends are inclusive. The byte offset is immediately after that record.
    return segment.offset(ordinal + int(end), offset)


def update_indexes(snapshot, target, *, check_only=False):
    specs = {
        "thread_turns": (("rollout_ordinal", "rollout_byte_offset", False),
                         ("rollout_end_ordinal", "rollout_end_byte_offset", True)),
        "thread_history_projection_state": (("next_rollout_ordinal", "next_rollout_byte_offset", False),),
    }
    with connect(target / DATABASES[1], "ro" if check_only else "rw") as db, db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        for table in tables:
            columns = {c[0] for c in db.execute("SELECT name FROM pragma_table_info(?)", (table,))}
            offsets = {c for c in columns if "byte_offset" in c}
            known = {off for _, off, _ in specs.get(table, ())}
            if offsets - known:
                raise ValueError(f"Unsupported pagination offsets in {table}: {sorted(offsets - known)}")
        for table, pairs in specs.items():
            for order, col, end in pairs:
                for rowid, tid, ordinal, old in db.execute(f"SELECT rowid, thread_id, {order}, {col} FROM {table}"):
                    segment = snapshot.by_key.get(tid)
                    if segment is None:
                        raise ValueError(f"{tid}: {table} row without a source segment")
                    new = translate(segment, ordinal, old, end)
                    if not check_only and new != old:
                        db.execute(f"UPDATE {table} SET {col}=? WHERE rowid=?", (new, rowid))
        check_database(db, target / DATABASES[1])


def relocate(snapshot, target, physical, workspaces=None):
    with connect(target / DATABASES[0], "rw") as db, db:
        for tid, t in snapshot.threads.items():
            db.execute("UPDATE threads SET rollout_path=?, cwd=? WHERE id=?",
                       (str(physical / t.path), workspaces.cwd(t.cwd) if workspaces else t.cwd, tid))
        if db.execute("SELECT 1 FROM sqlite_schema WHERE name='rollout_migration_skipped_rollouts'").fetchone():
            for (old,) in list(db.execute("SELECT DISTINCT rollout_path FROM rollout_migration_skipped_rollouts")):
                db.execute("UPDATE rollout_migration_skipped_rollouts SET rollout_path=? WHERE rollout_path=?",
                           (str(physical / relative_log(old)), old))
        check_database(db, target / DATABASES[0])
    if workspaces and snapshot.catalog:
        with connect(target / DATABASES[2], "rw") as db, db:
            for (host, tid), (_, old) in snapshot.catalog.items():
                if host == "local" and tid in snapshot.threads and old:
                    db.execute("UPDATE local_thread_catalog SET cwd=? WHERE host_id=? AND thread_id=?",
                               (workspaces.cwd(old), host, tid))
