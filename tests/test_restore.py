"""Small synthetic snapshots; set CODEX_TEST_APP_SERVER to run the real reader too."""

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch
from uuid import UUID

import codex_session_migrate as cli
from restore_client import app_server, compare_history, digest
from restore_rollouts import Snapshot, Workspaces, log_bytes, records, relocate, rewrite_logs, update_indexes
from restore_snapshot import read_probe, session_copy

SERVER = os.environ.get("CODEX_TEST_APP_SERVER")
LIVE, ARCHIVE, MIDDLE, LAST = (str(UUID(int=n)) for n in range(101, 105))
WINDOWS = r"Q:\example\missing-project"
TS = "2026-01-01T00:00:00.000Z"


@contextlib.contextmanager
def database(path):
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        yield db


def rollout(tid, segment=None, archived=False, compressed=False):
    root = "archived_sessions" if archived else "sessions/2026/01/01"
    return Path(root) / ("rollout-2026-01-01T00-00-00-" + tid +
                         ("_" + segment if segment else "") + (".jsonl.zst" if compressed else ".jsonl"))


def segment(home, path, tid, turn_num, base, workspace, metadata=False):
    start = base["end_ordinal_exclusive"] if base else 0
    meta = dict(id=tid, session_id=tid, timestamp=TS, cwd=workspace,
                originator="codex_cli_rs", cli_version="0.171.0-alpha.13", source="cli",
                model_provider="openai", runtime_workspace_roots=[workspace], history_mode="paginated")
    if base:
        meta["history_base"] = base
    turn = str(UUID(int=turn_num))
    text = f"Fixture turn {turn_num}: {WINDOWS}\nline two"
    uid = str(UUID(int=turn_num + 1000))
    aid = str(UUID(int=turn_num + 2000))
    user = {"type": "UserMessage", "id": uid, "content": [{"type": "text", "text": text, "text_elements": []}]}
    agent = {"type": "AgentMessage", "id": aid, "content": [{"type": "Text", "text": text}], "phase": "final_answer"}
    events = [("session_meta", meta), ("event_msg", {"type": "task_started", "turn_id": turn, "started_at": 1767225600}),
              ("event_msg", {"type": "item_completed", "thread_id": tid, "turn_id": turn, "item": user}),
              ("event_msg", {"type": "item_completed", "thread_id": tid, "turn_id": turn, "item": agent}),
              ("event_msg", {"type": "task_complete", "turn_id": turn, "last_agent_message": text})]
    if metadata:
        events.extend([
            ("world_state", {"full": True, "state": {"environments": [workspace]}}),
            ("token_usage_record", {"turn_id": turn, "usage": {}}),
            ("inter_agent_communication_metadata", {"trigger_turn": turn}),
        ])
    lines = [json.dumps(dict(timestamp=TS, ordinal=start + i, type=t, payload=v)).encode() + b"\r\n"
             for i, (t, v) in enumerate(events)]
    data = b"".join(lines)
    (home / path).parent.mkdir(parents=True, exist_ok=True)
    log_bytes(home / path, data)
    boundary = {"thread_id": path.name.split(".")[0].split("_")[-1] if "_" in path.name else tid,
                "end_ordinal_exclusive": start + len(lines), "end_byte_offset": len(data)}
    u = {"type": "userMessage", "id": uid, "clientId": None, "content": user["content"]}
    a = {"type": "agentMessage", "id": aid, "text": text, "phase": "final_answer",
         "memoryCitation": None, "moderation": None, "delivery": None, "questions": []}
    return boundary, (boundary["thread_id"], turn, start + 1, len(lines[0]), start + 4, sum(map(len, lines[:5])), u, a)


def fixture(home, workspace=WINDOWS, binary=None):
    home.mkdir()
    if binary:
        with app_server(binary, home, home.parent) as call:
            call("thread/list", {})
    with database(home / cli.DATABASES[0]) as db:
        if not binary:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, created_at, updated_at, "
                       "source, model_provider, cwd, title, name, sandbox_policy, approval_mode, has_user_event, "
                       "history_mode, archived, first_user_message, preview, thread_source)")
        for tid, path, arch in [(LIVE, rollout(LIVE, LAST), 0), (ARCHIVE, rollout(ARCHIVE, archived=True, compressed=True), 1)]:
            db.execute("INSERT INTO threads (id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,name,"
                       "sandbox_policy,approval_mode,has_user_event,history_mode,archived,first_user_message,preview,thread_source) "
                       "VALUES (?,?,1767225600,1767225600,'cli','openai',?,?,?,'{\"type\":\"read-only\"}',"
                       "'never',1,'paginated',?,'fixture','fixture','user')",
                       (tid, str(home / path), workspace, "Fixture " + tid, "Fixture " + tid, arch))
    rows = []
    for tid, paths in [(LIVE, [rollout(LIVE), rollout(LIVE, MIDDLE, compressed=True), rollout(LIVE, LAST)]),
                       (ARCHIVE, [rollout(ARCHIVE, archived=True, compressed=True)])]:
        base = None
        for path in paths:
            base, row = segment(home, path, tid, 400 + len(rows), base, workspace,
                                metadata=not binary and path == rollout(LIVE, MIDDLE, compressed=True))
            rows.append(row)
    if binary:
        with app_server(binary, home, home.parent) as call:
            call("thread/turns/list", {"threadId": LIVE})
    with database(home / cli.DATABASES[1]) as db:
        if not binary:
            db.executescript("""
                CREATE TABLE thread_turns(thread_id,turn_id,rollout_ordinal,rollout_byte_offset,
                    rollout_end_ordinal,rollout_end_byte_offset,status,first_user_item_id,final_agent_item_id);
                CREATE TABLE thread_history_projection_state(thread_id PRIMARY KEY,next_rollout_byte_offset,next_rollout_ordinal);
                CREATE TABLE thread_items(thread_id,turn_id,item_id,item_json,item_type,rollout_ordinal,created_at_ms,updated_at_ordinal);
            """)
        for tid, turn, ordinal, begin, endord, end, u, a in rows:
            db.execute("INSERT OR REPLACE INTO thread_turns (thread_id,turn_id,rollout_ordinal,rollout_byte_offset,"
                       "rollout_end_ordinal,rollout_end_byte_offset,status,first_user_item_id,final_agent_item_id) "
                       "VALUES (?,?,?,?,?,?,'completed',?,?)", (tid, turn, ordinal, begin, endord, end, u["id"], a["id"]))
            db.execute("INSERT OR REPLACE INTO thread_history_projection_state "
                       "(thread_id,next_rollout_byte_offset,next_rollout_ordinal) VALUES (?,?,?)", (tid, end, endord + 1))
            for i, item in enumerate([u, a], 1):
                db.execute("INSERT OR REPLACE INTO thread_items (thread_id,turn_id,item_id,item_json,item_type,"
                           "rollout_ordinal,created_at_ms,updated_at_ordinal) VALUES (?,?,?,?,?,?,1,?)",
                           (tid, turn, item["id"], json.dumps(item), item["type"], ordinal + i, ordinal + i))
    (home / "sqlite").mkdir(exist_ok=True)
    with database(home / cli.DATABASES[2]) as db:
        db.execute("CREATE TABLE local_thread_catalog(host_id, thread_id, display_title, cwd, PRIMARY KEY(host_id,thread_id))")
        db.executemany("INSERT INTO local_thread_catalog VALUES(?,?,?,?)", [
            ("local", LIVE, "Desktop fixture title", workspace), ("remote-fixture", LIVE, "Remote title", workspace),
        ])
    with (home / "session_index.jsonl").open("w", encoding="utf-8") as f:
        for tid in (LIVE, ARCHIVE):
            f.write(json.dumps({"id": tid, "thread_name": "Fixture " + tid, "updated_at": TS}) + "\n")
    # Encode the source's Windows path without requiring a Windows test runner.
    with database(home / cli.DATABASES[0]) as db:
        for tid, path in db.execute("SELECT id,rollout_path FROM threads"):
            db.execute("UPDATE threads SET rollout_path=? WHERE id=?",
                       (str(PureWindowsPath("R:/synthetic-home") / Path(path).relative_to(home)), tid))
    for relative in cli.DATABASES:
        with contextlib.closing(sqlite3.connect(home / relative)) as checkpoint:
            checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()


class Snapshots(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        fixture(self.source)
        self.dest = self.root / "dest"
        session_copy(self.source, self.dest)

    def test_mapping_round_trip_and_indexes(self):
        src = Snapshot(self.source)
        with self.assertRaisesRegex(ValueError, "Confirm mappings"):
            Workspaces(src, {}, self.root / "view")
        workspace = self.root / "new local \u9879\u76ee"
        workspace.mkdir()
        maps = Workspaces(src, {WINDOWS: str(workspace)}, self.root / "view")
        rewrite_logs(src, self.dest, maps)
        update_indexes(src, self.dest)
        relocate(src, self.dest, self.dest, maps)
        restored = Snapshot(self.dest)
        self.assertEqual(restored.catalog["local", LIVE], ("Desktop fixture title", str(workspace)))
        self.assertEqual(restored.catalog["remote-fixture", LIVE], ("Remote title", WINDOWS))
        update_indexes(restored, self.dest, check_only=True)
        self.assertEqual(len(restored.threads), 2)
        self.assertEqual(len(restored.threads[LIVE].chain), 3)
        self.assertEqual(src.threads[LIVE].recorded_turns(), restored.threads[LIVE].recorded_turns())
        for rel in src.segments:
            before = list(records(self.source / rel))
            after = list(records(self.dest / rel))
            self.assertEqual(before[1:], after[1:])
            self.assertTrue(all(line.endswith(b"\r\n") for line, _ in after))
        # Second leg uses Windows path semantics for workspace availability on a Unix test host.
        returned = self.root / "returned"
        session_copy(self.dest, returned)
        with patch("restore_rollouts.local_directory", side_effect=lambda s: PureWindowsPath(s).is_absolute()):
            maps = Workspaces(restored, {str(workspace): WINDOWS}, self.root / "view")
            rewrite_logs(restored, returned, maps)
            update_indexes(restored, returned)
            relocate(restored, returned, PureWindowsPath("S:/restored-home"), maps)
        again = Snapshot(returned)
        self.assertEqual(again.threads[LIVE].cwd, WINDOWS)
        update_indexes(again, returned, check_only=True)
        for rel in src.segments:
            self.assertEqual([r for _, r in records(self.source / rel)], [r for _, r in records(returned / rel)])

    def test_preserve_unmodified_compressed_bytes(self):
        source = Snapshot(self.source)
        with patch("restore_rollouts.local_directory", return_value=True):
            rewrite_logs(source, self.dest, Workspaces(source, {}, self.root / "view"))
        for p in source.segments:
            self.assertEqual((self.source / p).read_bytes(), (self.dest / p).read_bytes())

    def test_crlf_offset_requires_byte_boundary(self):
        source = Snapshot(self.source)
        with database(self.dest / cli.DATABASES[1]) as db:
            db.execute("UPDATE thread_turns SET rollout_byte_offset=rollout_byte_offset-1 WHERE thread_id=?", (MIDDLE,))
        with self.assertRaisesRegex(ValueError, f"{LIVE}.*boundary"):
            update_indexes(source, self.dest)

    def test_exact_compressed_counterpart_of_sqlite_entry(self):
        with database(self.dest / cli.DATABASES[0]) as db:
            db.execute("UPDATE threads SET rollout_path=substr(rollout_path,1,length(rollout_path)-4) WHERE id=?",
                       (ARCHIVE,))
        snap = Snapshot(self.dest)
        self.assertEqual(snap.threads[ARCHIVE].path, rollout(ARCHIVE, archived=True, compressed=True))
        self.assertEqual(snap.threads[LIVE].path, rollout(LIVE, LAST))

    def test_unrecognized_record_identifies_segment(self):
        logfile = self.dest / rollout(LIVE, LAST)
        with logfile.open("ab") as out:
            out.write(b'{"type":"future_format","payload":{}}\n')
        with self.assertRaisesRegex(ValueError, f"{LAST}.*line 6.*unsupported"):
            Snapshot(self.dest)

    def test_unknown_workspace_and_history_only(self):
        source = Snapshot(self.source)
        maps = Workspaces(source, {WINDOWS: None}, self.root / "view")
        rewrite_logs(source, self.dest, maps)
        header = next(records(self.dest / rollout(LIVE)))[1]["payload"]
        self.assertEqual(header["runtime_workspace_roots"], [])
        self.assertEqual(header["cwd"], str(self.root / "view"))
        context = {"type": "turn_context", "payload": {"cwd": WINDOWS, "workspace_roots": [WINDOWS]}}
        maps.update(context)
        self.assertEqual(context["payload"], {"cwd": str(self.root / "view"), "workspace_roots": []})

    def test_missing_cycle_wrong_id_and_boundary(self):
        path = self.dest / rollout(LIVE, LAST)
        lines = list(records(path))
        for key, value, reason in [("thread_id", LAST, "cycle"), ("thread_id", ARCHIVE, "unrelated"),
                                   ("thread_id", str(UUID(int=9999)), "missing"),
                                   ("end_byte_offset", 7, "boundary")]:
            with self.subTest(reason=reason):
                header = json.loads(lines[0][0])
                header["payload"]["history_base"][key] = value
                path.write_bytes(json.dumps(header).encode() + b"\n" + b"".join(l for l, _ in lines[1:]))
                with self.assertRaisesRegex(ValueError, reason):
                    Snapshot(self.dest)

    def test_first_segment_is_a_file_but_is_incomplete(self):
        snap = Snapshot(self.source)
        relocate(snap, self.dest, self.dest)
        with database(self.dest / cli.DATABASES[0]) as db:
            db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(self.dest / rollout(LIVE)), LIVE))
        cli.validate_copy(self.dest)
        with self.assertRaisesRegex(ValueError, "unreachable"):
            Snapshot(self.dest)
        expected = [(t, "same item") for t in snap.threads[LIVE].recorded_turns()]
        with self.assertRaisesRegex(ValueError, "expected 3 visible turns, read 1"):
            compare_history({LIVE: ("title", False, expected)}, {LIVE: ("title", False, expected[:1])})


@unittest.skipUnless(SERVER, "set CODEX_TEST_APP_SERVER to the desktop's Codex CLI")
class RealClient(unittest.TestCase):
    def test_wrong_entry_truncates_client_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "snapshot"
            fixture(home, binary=SERVER)
            model = Snapshot(home)
            relocate(model, home, home)
            full = read_probe(model, SERVER, root / "baseline", size=1)
            with database(home / cli.DATABASES[0]) as db:
                db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(home / rollout(LIVE)), LIVE))
            cli.validate_copy(home)
            with app_server(SERVER, home, root) as call:
                short = call("thread/turns/list", {"threadId": LIVE, "sortDirection": "asc", "itemsView": "full"})
            turns = [(t["id"], digest(t["items"])) for t in short["data"]]
            self.assertEqual(len(turns), 1)
            with self.assertRaisesRegex(ValueError, "expected 3 visible turns, read 1"):
                compare_history(full, {**full, LIVE: (*full[LIVE][:2], turns)})

    def test_native_override_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home, copied = root / "source", root / "target"
            fixture(home, binary=SERVER)
            model = Snapshot(home)
            relocate(model, home, home)
            project = root / "project"
            project.mkdir()
            with app_server(SERVER, home, root) as call:
                call("thread/resume", {"threadId": LIVE, "excludeTurns": True,
                                       "cwd": str(project), "runtimeWorkspaceRoots": [str(project)]})
            model = Snapshot(home)
            session_copy(home, copied)
            raw = (home / model.threads[LIVE].path).read_bytes()
            new_project = root / "new-project"
            new_project.mkdir()
            maps = Workspaces(model, {WINDOWS: str(new_project), str(project): str(new_project)}, root / "view")
            used = rewrite_logs(model, copied, maps, prefer_override=True)
            self.assertEqual(used, set() if os.name == "nt" else {LIVE})
            if used:
                self.assertTrue((copied / model.threads[LIVE].path).read_bytes().startswith(raw))
            update_indexes(model, copied)
            relocate(model, copied, copied, maps)
            target = Snapshot(copied)
            required = {tid: {"cwd": str(new_project), "roots": [str(new_project)]} for tid in target.threads}
            before = read_probe(model, SERVER, root / "before", size=1)
            after = read_probe(target, SERVER, root / "after", size=1, resume=True, workspaces=required)
            compare_history(before, after)

    def test_prepare_repair_restart_and_check(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            remote = root / "remote"
            remote.mkdir()
            name = "codex-git-20260101-000000"
            source = remote / name
            fixture(source, binary=SERVER)
            def git(*args):
                subprocess.run(["git", "-c", "core.autocrlf=false", "-C", str(remote), *args], check=True, capture_output=True)
            git("init", "-q", "-b", "main")
            (remote / ".gitattributes").write_text("*.zst export-ignore\n", encoding="ascii")
            git("add", "--", ".gitattributes")
            git("add", "--", *(str(Path(name) / p) for p in (*cli.LOG_DIRECTORIES, *cli.SESSION_FILES)))
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
            config = root / "toolkit.toml"
            config.write_text("[paths]\nformal_home='formal'\ntest_home='testing/home'\nbackup_root='rollback'\n"
                              "checkout='checkout'\n[repository]\nurl=" + json.dumps(str(remote)), encoding="utf-8")
            (root / "formal").mkdir()
            (root / "formal/config.toml").touch()
            args = ["--config", str(config)]
            cli.main([*args, "prepare"])
            # A subsequent branch commit must never become the baseline for this prepared home.
            git("rm", "-qr", "--cached", "--", name)
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "-qm", "expire snapshot on the branch")
            subprocess.run(["git", "-C", str(root / "checkout"), "fetch", "origin"], check=True, capture_output=True)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                cli.main([*args, "repair", "--plan"])
            self.assertTrue(json.loads(output.getvalue())["workspaces"])
            mapping = root / "map.json"
            repair = [*args, "repair", "--map", str(mapping), "--app-server", SERVER]
            prepared = root / "testing/home"
            receipt = (prepared / ".session-restore.json").read_bytes()
            mapping.write_text(json.dumps({WINDOWS: "unconfirmed/relative/path"}))
            with self.assertRaisesRegex(SystemExit, "existing absolute local directory"):
                cli.main(repair)
            self.assertEqual((prepared / ".session-restore.json").read_bytes(), receipt)
            self.assertTrue(list(prepared.parent.glob(".session-repair-*")))
            mapping.write_text(json.dumps({WINDOWS: None}))
            cli.main(repair)
            cli.main(repair)
            cli.main([*args, "test", "--check-only"])
            with patch.object(cli.Desktop, "stop") as stop, patch.object(cli.Desktop, "command", return_value=["gui"]), \
                    patch.object(cli.Desktop, "launch_test") as launch:
                cli.main([*args, "test"])
                stop.assert_called_once()
                launch.assert_called_once_with(["gui"])
            model = Snapshot(root / "testing/home")
            result = read_probe(model, SERVER, root / "paged", size=1, resume=True)
            self.assertEqual([r[0] for r in result[LIVE][2]], model.threads[LIVE].recorded_turns())
            self.assertEqual(len(result[LIVE][2]), 3)
            self.assertTrue(result[ARCHIVE][1])
            last = next(l for l in records(model.home / rollout(LIVE, LAST))
                        if l[1]["payload"].get("item", {}).get("type") == "UserMessage")
            self.assertEqual(last[1]["payload"]["item"]["content"][0]["text"], f"Fixture turn 402: {WINDOWS}\nline two")
            # Change the visible projection and the associated log without changing byte lengths.
            log = model.home / rollout(LIVE, LAST)
            log.write_bytes(log.read_bytes().replace(b"Fixture turn 402", b"Altered turn 402"))
            with database(model.home / cli.DATABASES[1]) as db:
                db.execute("UPDATE thread_items SET item_json=replace(item_json,?,?) WHERE thread_id=?",
                           ("Fixture turn 402", "Altered turn 402", LAST))
            with patch.object(cli.Desktop, "stop") as stop, patch.object(cli.Desktop, "launch_test") as launch:
                with self.assertRaisesRegex(SystemExit, "history mismatch"):
                    cli.main([*args, "test"])
                stop.assert_not_called()
                launch.assert_not_called()
