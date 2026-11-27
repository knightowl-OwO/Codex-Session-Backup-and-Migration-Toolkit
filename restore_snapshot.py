"""Stage a repair and compare independent reads of its pinned source and destination."""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp

from codex_desktop import Desktop
from codex_session_common import (
    DATABASES, LOG_DIRECTORIES, SESSION_FILES, SNAPSHOT_NAME, Repository, backup_database, connect, directory_lock,
)
from restore_client import app_server, compare_history, history
from restore_rollouts import (
    Snapshot, Workspaces, local_directory, recover_interrupted_tail, relocate, rewrite_logs, update_indexes,
)

RECEIPT = ".session-restore.json"
TEST_MARKER = ".session-test.json"
SESSION_LAYER = (*LOG_DIRECTORIES, *SESSION_FILES)


class RepairRollbackError(RuntimeError):
    """The session-layer install failed and its automatic rollback was incomplete."""


def remove_scratch(path):
    def retry(action, filename, error):
        if os.name != "nt" or not isinstance(error[1], PermissionError) or os.path.islink(filename):
            raise error[1]
        os.chmod(filename, stat.S_IWRITE | stat.S_IREAD)
        action(filename)
    shutil.rmtree(path, onerror=retry)


def pin(repo, revision, name, home):
    repo.git("update-ref", f"refs/codex-session-restore/{revision}", revision)
    receipt = {"format": 1, "commit": revision, "snapshot": name, "origin": repo.config.remote}
    (home / RECEIPT).write_text(json.dumps(receipt) + "\n", encoding="utf-8")


def provenance(config):
    f = config.test / RECEIPT
    if not f.is_file() or f.resolve() != f:
        raise ValueError("Missing prepared snapshot receipt. Run prepare with this version of the toolkit.")
    info = json.loads(f.read_text(encoding="utf-8"))
    if (not isinstance(info, dict) or info.get("format") != 1 or info.get("origin") != config.remote
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", str(info.get("commit")))
            or not SNAPSHOT_NAME.fullmatch(str(info.get("snapshot")))):
        raise ValueError("Unknown or incompatible prepared snapshot receipt. Run prepare again.")
    repo = Repository(config)
    if repo.output("remote", "get-url", "origin") != config.remote:
        raise ValueError("Prepared snapshot origin differs from the backup checkout")
    ref = f"refs/codex-session-restore/{info['commit']}"
    if not repo.has_ref(ref) or repo.output("rev-parse", ref) != info["commit"]:
        raise ValueError(f"Missing pinned commit {info['commit']} in the backup checkout; run prepare again")
    return repo, info


def session_copy(home, target):
    target.mkdir()
    for directory in LOG_DIRECTORIES:
        source = home / directory
        if source.is_dir():
            shutil.copytree(source, target / directory, symlinks=True)
        elif source.exists():
            raise ValueError(f"Expected a session directory: {source}")
        else:
            (target / directory).mkdir()
    for path in SESSION_FILES:
        if path in DATABASES:
            backup_database(home / path, target / path)
        else:
            shutil.copyfile(home / path, target / path)


def match_local_migrations(extracted, formal):
    """Adapt SQLx bookkeeping in disposable databases when the destination schema matches."""
    migrations = "SELECT version, description, success, checksum FROM _sqlx_migrations ORDER BY version"
    schema = "SELECT type, name, replace(sql, char(13)||char(10), char(10)) FROM sqlite_schema ORDER BY type, name"
    for name in DATABASES[:2]:
        if not (formal / name).is_file():
            continue
        with connect(extracted / name, "rw") as src, connect(formal / name) as ref:
            if not all(db.execute("SELECT 1 FROM sqlite_schema WHERE name='_sqlx_migrations'").fetchone()
                       for db in (src, ref)):
                continue
            old, local = (db.execute(migrations).fetchall() for db in (src, ref))
            if old == local or not old:
                continue
            if ([r[:3] for r in old] != [r[:3] for r in local] or not all(r[2] == 1 for r in old)
                    or src.execute(schema).fetchall() != ref.execute(schema).fetchall()):
                continue
            with src:
                src.executemany("UPDATE _sqlx_migrations SET checksum=? WHERE version=?",
                                [(r[3], r[0]) for r in local])


def validate_workspace_targets(targets, disposable_homes):
    for target in targets:
        if target is None:
            continue
        resolved = Path(target).resolve()
        for home in disposable_homes:
            if resolved.is_relative_to(home):
                raise ValueError(f"Workspace {target} is inside a disposable test home: {home}")


def write_map_draft(path, plan):
    """Write an exact-path mapping draft while preserving still-valid prior decisions."""
    path = Path(path).expanduser().resolve()
    previous = {}
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(previous, dict):
            raise ValueError(f"Existing map draft is not a JSON object: {path}")
    draft, unresolved, history_only = {}, [], []
    for item in plan:
        source = item["source"]
        if item["available"]:
            target = source
        elif source in previous and previous[source] is None:
            target = None
            history_only.append(source)
        elif source in previous and isinstance(previous[source], str) and local_directory(previous[source]):
            target = previous[source]
        else:
            target = None
            unresolved.append(source)
        draft[source] = target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(draft, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path, unresolved, history_only


def select_client(args, info, config):
    binary = getattr(args, "app_server", None) or info.get("app_server")
    if not binary:
        launcher = Path(Desktop(config).command()[0]).resolve()
        name = "codex.exe" if launcher.suffix.lower() == ".exe" else "codex"
        candidates = [launcher.parent / "resources" / name, launcher.parent.parent / "Resources" / name]
        found = {p.resolve() for p in candidates if p.is_file()}
        if len(found) == 1:
            binary = found.pop()
    if not binary:
        raise ValueError("Pass --app-server with the path to the Codex CLI shipped with the target desktop")
    binary = Path(binary).expanduser().resolve(strict=True)
    version = subprocess.check_output([str(binary), "--version"], text=True, timeout=15).strip()
    if not version.startswith("codex-cli "):
        raise ValueError(f"Expected a Codex app-server executable, got {version!r}")
    return binary, version


def read_probe(model, binary, scratch, *, resume=False, size=32, workspaces=None):
    scratch.mkdir()
    home = scratch / "home"
    session_copy(model.home, home)
    relocate(model, home, home)
    # Keep the probe offline, without copying auth, project trust, plugins or MCP configuration.
    with connect(home / DATABASES[0]) as db:
        providers = {row[0] for row in db.execute("SELECT DISTINCT model_provider FROM threads")}
    builtin = {"openai", "ollama", "lmstudio", "migration_offline"}
    offline_providers = "".join(
        f'\n[model_providers.{json.dumps(provider)}]\nname = "Offline history"\nbase_url = "http://127.0.0.1:9/v1"\n'
        for provider in sorted(p for p in providers if p and p not in builtin))
    (home / "config.toml").write_text(
        'model_provider = "migration_offline"\n'
        '[model_providers.migration_offline]\nname = "Offline history"\n'
        'base_url = "http://127.0.0.1:9/v1"\n' + offline_providers, encoding="utf-8")
    with app_server(binary, home, scratch) as call:
        result = history(call, model.threads, size, resume=resume, workspaces=workspaces)
        if resume:
            compare_history(result, history(call, model.threads, size))
    if resume:
        with app_server(binary, home, scratch) as call:
            compare_history(result, history(call, model.threads, size, resume=True, workspaces=workspaces))
    remove_scratch(scratch)
    return result


def accept(source, target, binary, work, workspaces, overrides, *, size=32):
    dest = Snapshot(target)
    if source.threads.keys() != dest.threads.keys():
        raise ValueError(f"Thread IDs differ: {sorted(source.threads.keys() ^ dest.threads.keys())}")
    for tid, t in source.threads.items():
        d = dest.threads[tid]
        if (t.path, t.title, t.archived) != (d.path, d.title, d.archived):
            raise ValueError(f"{tid}: source entry, title or archive state differs")
        if [s.key for s in t.chain] != [s.key for s in d.chain]:
            raise ValueError(f"{tid}: segment chain differs from the source")
    unresolved = [p for p in dest.plan() if not p["available"] and
                  any(use["thread"] not in overrides or use["field"] in ("threads.cwd", "catalog.cwd")
                      for use in p["uses"])]
    if unresolved:
        raise ValueError(f"Unresolved runtime workspaces: {unresolved}")
    if (source.home / "session_index.jsonl").read_bytes() != (target / "session_index.jsonl").read_bytes():
        raise ValueError("Session title index differs from the snapshot")
    if source.catalog.keys() != dest.catalog.keys():
        raise ValueError(f"Desktop catalog IDs differ: {sorted(source.catalog.keys() ^ dest.catalog.keys())}")
    for key, (title, _) in source.catalog.items():
        if dest.catalog[key][0] != title:
            raise ValueError(f"{key}: desktop catalog title differs")
    update_indexes(dest, target, check_only=True)
    expected = read_probe(source, binary, work / "baseline", size=size)
    actual = read_probe(dest, binary, work / "candidate", resume=True, size=size, workspaces=workspaces)
    compare_history(expected, actual)
    return sum(len(r[2]) for r in expected.values())


def swap_candidate(candidate, test, work):
    """Install only the repaired session layer, leaving the local runtime profile untouched."""
    previous = work / "previous-session-layer"
    manifest = work / "session-install.json"
    if previous.exists():
        raise ValueError(f"Unexpected existing session rollback directory: {previous}")
    if not test.is_dir() or test.resolve() != test or candidate.resolve() != candidate:
        raise ValueError("Repair requires physical candidate and test-home directories")
    missing = [relative for relative in LOG_DIRECTORIES if not (candidate / relative).is_dir()]
    missing.extend(relative for relative in (*SESSION_FILES, RECEIPT) if not (candidate / relative).is_file())
    if missing:
        raise ValueError(f"Repaired session candidate is incomplete: {missing}")
    for parent in (test, candidate):
        for relative in (*SESSION_LAYER, RECEIPT, TEST_MARKER):
            entry = parent / relative
            try:
                metadata = entry.lstat()
            except FileNotFoundError:
                continue
            if entry.resolve() != entry or stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"Redirected session path: {entry}")
            if stat.S_ISDIR(metadata.st_mode):
                for child in entry.rglob("*"):
                    if child.resolve() != child:
                        raise ValueError(f"Redirected session path: {child}")
        if any(Path(str(parent / db) + suffix).exists() for db in DATABASES
               for suffix in ("-wal", "-shm", "-journal")):
            raise ValueError(f"SQLite sidecar still present under {parent}; close the test client")

    def rename(source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(6):
            try:
                source.rename(destination)
                return
            except PermissionError:
                if os.name != "nt" or attempt == 5:
                    raise
                time.sleep(0.25 * (attempt + 1))

    previous.mkdir()
    saved, installed = [], []
    try:
        # Remove the old receipt first; publish the new receipt only after all data is in place.
        for relative in (RECEIPT, TEST_MARKER, *SESSION_LAYER):
            source = test / relative
            if source.exists():
                rename(source, previous / relative)
                saved.append(relative)
        for relative in (*SESSION_LAYER, RECEIPT):
            source = candidate / relative
            if source.exists():
                rename(source, test / relative)
                installed.append(relative)
    except BaseException as failure:
        rollback_errors = []
        for relative in reversed(installed):
            try:
                rename(test / relative, candidate / relative)
            except BaseException as error:
                rollback_errors.append((relative, repr(error)))
        for relative in reversed(saved):
            try:
                rename(previous / relative, test / relative)
            except BaseException as error:
                rollback_errors.append((relative, repr(error)))
        if rollback_errors:
            manifest.write_text(json.dumps({
                "format": 1,
                "test_home": str(test),
                "candidate": str(candidate),
                "rollback": str(previous),
                "saved": list(saved),
                "installed": list(installed),
                "rollback_errors": rollback_errors,
            }, indent=2) + "\n", encoding="utf-8")
            raise RepairRollbackError(
                f"Session install and rollback both failed; recovery data retained at {work}"
            ) from failure
        raise


def repair(args, config, extract):
    if args.test_home and args.test_home.expanduser().resolve() != config.test:
        raise ValueError(f"Supplied test home differs from the configured test home: {config.test}")
    with directory_lock(config.test_lock), directory_lock(config.checkout):
        Desktop(config).require_test_idle()
        repo, info = provenance(config)
        work = Path(mkdtemp(prefix=config.scratch_prefix("session-repair"), dir=config.test.parent))
        keep_staging = bool(getattr(args, "keep_staging", False))
        try:
            extracted = extract(repo, info["commit"], info["snapshot"], work)
            if not args.plan:
                match_local_migrations(extracted, config.formal)
            source = Snapshot(extracted)
            if args.plan:
                plan = source.plan()
                result = {"commit": info["commit"], "snapshot": info["snapshot"], "workspaces": plan}
                draft = getattr(args, "write_map_draft", None)
                if draft:
                    path, unresolved, history_only = write_map_draft(draft, plan)
                    result.update(map_draft=str(path), unresolved=unresolved, history_only=history_only)
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                recover_interrupted_tail(source)
                supplied = json.loads(args.map.read_text(encoding="utf-8-sig")) if args.map else {}
                legacy = config.test.parent / ".codex-history-view"
                maps = Workspaces(source, supplied, config.history_view,
                                  legacy_view=legacy if legacy != config.history_view else None)
                validate_workspace_targets(maps.targets.values(), (config.test, config.test_runtime))
                binary, version = select_client(args, info, config)
                candidate = work / "repaired"
                session_copy(extracted, candidate)
                expected = {}
                for tid, thread in source.threads.items():
                    roots = thread.effective_roots()
                    expected[tid] = {"cwd": maps.cwd(thread.cwd), "roots": None if roots is None else
                                     [maps.targets[r] for r in roots if maps.targets[r] is not None]}
                overrides = rewrite_logs(source, candidate, maps, prefer_override=True)
                update_indexes(source, candidate)
                relocate(source, candidate, candidate, maps)
                try:
                    turns = accept(source, candidate, binary, work, expected, overrides)
                except ValueError as error:
                    if not overrides:
                        raise
                    print(f"Native workspace override did not pass acceptance ({error}). Retrying with path edits.",
                          file=sys.stderr)
                    # Retry from pinned bytes if this client could not use the appended settings.
                    for rel in source.segments:
                        shutil.copyfile(extracted / rel, candidate / rel)
                        source.segments[rel].rewritten = None
                    shutil.copyfile(extracted / DATABASES[1], candidate / DATABASES[1])
                    rewrite_logs(source, candidate, maps)
                    update_indexes(source, candidate)
                    retry = work / "without-override"
                    retry.mkdir()
                    overrides.clear()
                    turns = accept(source, candidate, binary, retry, expected, overrides)
                relocate(source, candidate, config.test, maps)
                info.update(app_server=str(binary), client_version=version,
                            workspaces=expected, overrides=sorted(overrides),
                            history_only=[v for v, target in maps.targets.items() if target is None])
                (candidate / RECEIPT).write_text(json.dumps(info) + "\n", encoding="utf-8")
                Desktop(config).require_test_idle()
                swap_candidate(candidate, config.test, work)
                print(f"Verified {len(source.threads)} threads and {turns} turns with {version} at {info['commit']}")
                if info["history_only"]:
                    print(f"History available; map these workspaces to continue in the original projects: {info['history_only']}")
        except BaseException as error:
            preserve = keep_staging or isinstance(error, RepairRollbackError)
            if preserve:
                print(f"Prepared test home retained at {config.test}. Repair staging retained at {work}")
            else:
                try:
                    remove_scratch(work)
                except OSError as cleanup_error:
                    error.add_note(f"Could not remove repair staging {work}: {cleanup_error}")
                    print(f"Prepared test home retained at {config.test}. Repair staging could not be removed: {work}")
                else:
                    print(f"Prepared test home retained at {config.test}. Temporary repair staging removed.")
            raise
        else:
            try:
                remove_scratch(work)
            except OSError as cleanup_error:
                print(f"Repair succeeded, but temporary staging could not be removed: {work} ({cleanup_error})",
                      file=sys.stderr)


def verify(config, args, extract):
    with directory_lock(config.checkout):
        repo, info = provenance(config)
        if not {"client_version", "workspaces", "overrides"} <= info.keys():
            raise ValueError("Prepared snapshot awaits workspace repair and client verification. Run repair first.")
        binary, version = select_client(args, info, config)
        with TemporaryDirectory(prefix=config.scratch_prefix("history-check"), dir=config.test.parent) as temp:
            work = Path(temp)
            extracted = extract(repo, info["commit"], info["snapshot"], work)
            match_local_migrations(extracted, config.formal)
            source = Snapshot(extracted)
            recover_interrupted_tail(source)
            turns = accept(source, config.test, binary, work, info["workspaces"], set(info["overrides"]))
        print(f"Verified {len(source.threads)} threads and {turns} visible turns with {version}")
