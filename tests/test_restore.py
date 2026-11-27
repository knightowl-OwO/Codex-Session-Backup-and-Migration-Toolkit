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
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import codex_session_migrate as cli
import codex_desktop
from codex_session_common import Config
from restore_client import app_server, compare_history, digest
from restore_rollouts import Snapshot, Workspaces, log_bytes, records, relocate, rewrite_logs, update_indexes
from restore_snapshot import (
    RECEIPT, read_probe, session_copy, swap_candidate, validate_workspace_targets, write_map_draft,
)

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

    def test_prepare_installs_runtime_links_after_publishing_final_home(self):
        config = SimpleNamespace(
            formal=self.source, test=self.dest, test_lock=self.root / "lock",
            scratch_prefix=lambda operation: f".{operation}-",
        )
        repo = SimpleNamespace(path=self.root / "repo", fetch=lambda: None)
        relative = Path("packages/app-server-daemon/current")
        target = Path("packages/app-server-daemon/releases/v1")

        def stage(_formal, _snapshot, candidate):
            (candidate / target).mkdir(parents=True)
            return {relative: target}

        desktop = SimpleNamespace(require_test_idle=lambda: None)
        with (
            patch.object(cli, "Repository", return_value=repo),
            patch.object(cli, "directory_lock", side_effect=lambda _: contextlib.nullcontext()),
            patch.object(cli, "Desktop", return_value=desktop),
            patch.object(cli, "select_snapshot", return_value=("revision", "snapshot")),
            patch.object(cli, "extract_snapshot", return_value=self.root / "snapshot"),
            patch.object(cli, "stage_copy", side_effect=stage),
            patch.object(cli, "pin"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cli.prepare(config, None)

        self.assertEqual((self.dest / relative).resolve(strict=True), self.dest / target)

    def test_prepare_restores_previous_home_when_runtime_link_install_fails(self):
        config = SimpleNamespace(
            formal=self.source, test=self.dest, test_lock=self.root / "lock",
            scratch_prefix=lambda operation: f".{operation}-",
        )
        repo = SimpleNamespace(path=self.root / "repo", fetch=lambda: None)
        original = self.dest.stat().st_ino

        def stage(_formal, _snapshot, candidate):
            candidate.mkdir()
            (candidate / "new-copy").touch()
            return {}

        desktop = SimpleNamespace(require_test_idle=lambda: None)
        with (
            patch.object(cli, "Repository", return_value=repo),
            patch.object(cli, "directory_lock", side_effect=lambda _: contextlib.nullcontext()),
            patch.object(cli, "Desktop", return_value=desktop),
            patch.object(cli, "select_snapshot", return_value=("revision", "snapshot")),
            patch.object(cli, "extract_snapshot", return_value=self.root / "snapshot"),
            patch.object(cli, "stage_copy", side_effect=stage),
            patch.object(cli, "pin"),
            patch.object(cli, "install_runtime_links", side_effect=ValueError("link failure")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(ValueError, "link failure"):
                cli.prepare(config, None)

        self.assertEqual(self.dest.stat().st_ino, original)
        self.assertFalse((self.dest / "new-copy").exists())
        self.assertEqual(list(self.root.glob(".preparing-*")), [])

    def test_prepare_restores_previous_home_when_publication_fails(self):
        config = SimpleNamespace(
            formal=self.source, test=self.dest, test_lock=self.root / "lock",
            scratch_prefix=lambda operation: f".{operation}-",
        )
        repo = SimpleNamespace(path=self.root / "repo", fetch=lambda: None)
        original = self.dest.stat().st_ino

        def stage(_formal, _snapshot, candidate):
            candidate.mkdir()
            return {}

        original_rename = Path.rename

        def rename(path, target):
            if path.name == "home" and target == self.dest:
                raise OSError("publication failure")
            return original_rename(path, target)

        desktop = SimpleNamespace(require_test_idle=lambda: None)
        with (
            patch.object(cli, "Repository", return_value=repo),
            patch.object(cli, "directory_lock", side_effect=lambda _: contextlib.nullcontext()),
            patch.object(cli, "Desktop", return_value=desktop),
            patch.object(cli, "select_snapshot", return_value=("revision", "snapshot")),
            patch.object(cli, "extract_snapshot", return_value=self.root / "snapshot"),
            patch.object(cli, "stage_copy", side_effect=stage),
            patch.object(cli, "pin"),
            patch.object(Path, "rename", rename),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(OSError, "publication failure"):
                cli.prepare(config, None)

        self.assertEqual(self.dest.stat().st_ino, original)
        self.assertEqual(list(self.root.glob(".preparing-*")), [])

    def test_test_stops_windows_desktop_before_verifying(self):
        events = []
        marker = {"threads": [], "launched": False}
        desktop = SimpleNamespace(
            command=lambda: events.append("command") or ["desktop"],
            stop_for_restart=lambda: events.append("stop"),
            require_test_idle=lambda: events.append("idle"),
            launch_test=lambda command: events.append(("launch", command)),
        )
        config = SimpleNamespace(formal=self.root / "formal", test_lock=self.root / "lock", test=self.dest,
                                 test_runtime=self.dest.with_name(self.dest.name + "-runtime"),
                                 scratch_prefix=lambda operation: f".{operation}-")
        args = SimpleNamespace(action="test", check_only=False, reopen=False)
        with (
            patch.object(cli, "directory_lock", side_effect=lambda _: contextlib.nullcontext()),
            patch.object(cli, "Desktop", return_value=desktop),
            patch.object(cli, "validate_copy", side_effect=lambda _: events.append("validate")),
            patch.object(cli, "verify", side_effect=lambda *_: events.append("verify")),
            patch.object(cli, "write_test_marker",
                         side_effect=lambda _: events.append("write marker") or marker),
            patch.object(cli, "validate_test_marker",
                         side_effect=lambda _: events.append("check marker") or marker),
            patch.object(cli, "mark_test_launched",
                         side_effect=lambda *_args, **_kwargs: events.append("mark launched")),
            patch.object(cli.sys, "platform", "win32"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cli.run(args, config, self.root / "config.toml")
        self.assertEqual(events, [
            "command", "stop", "validate", "idle", "verify", "write marker", "stop", "validate",
            "check marker", "mark launched",
            ("launch", ["desktop"]),
        ])

    def test_check_only_keeps_windows_desktop_open(self):
        events = []
        desktop = SimpleNamespace(
            command=lambda: self.fail("check-only must not resolve a launcher"),
            stop_for_restart=lambda: events.append("stop"),
            require_test_idle=lambda: events.append("idle"),
            launch_test=lambda _: events.append("launch"),
        )
        config = SimpleNamespace(formal=self.root / "formal", test_lock=self.root / "lock", test=self.dest)
        args = SimpleNamespace(action="test", check_only=True, reopen=False)
        with (
            patch.object(cli, "directory_lock", side_effect=lambda _: contextlib.nullcontext()),
            patch.object(cli, "Desktop", return_value=desktop),
            patch.object(cli, "validate_copy", side_effect=lambda _: events.append("validate")),
            patch.object(cli, "verify", side_effect=lambda *_: events.append("verify")),
            patch.object(cli, "write_test_marker", side_effect=lambda _: events.append("write marker")),
            patch.object(cli.sys, "platform", "win32"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cli.run(args, config, self.root / "config.toml")
        self.assertEqual(events, ["validate", "idle", "verify", "write marker"])

    @unittest.skipUnless(os.name == "nt", "extended-length paths are Windows-only")
    def test_validate_and_rewrite_extended_windows_rollout_paths(self):
        prefix = "\\\\?\\"
        rollouts = {
            LIVE: rollout(LIVE, LAST),
            ARCHIVE: rollout(ARCHIVE, archived=True, compressed=True),
        }
        with database(self.dest / cli.DATABASES[0]) as db:
            db.executemany(
                "UPDATE threads SET rollout_path=? WHERE id=?",
                [(prefix + str(self.dest / relative), tid) for tid, relative in rollouts.items()],
            )
            outside = prefix + str(self.source / rollouts[LIVE])
            db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (outside, LIVE))

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "1 rollout paths are missing or outside"):
                cli.validate_copy(self.dest)

        with database(self.dest / cli.DATABASES[0]) as db:
            db.execute("UPDATE threads SET rollout_path=? WHERE id=?",
                       (prefix + str(self.dest / rollouts[LIVE]), LIVE))

        cli.validate_copy(self.dest)
        self.dest.rename(self.root / "promoted")
        promoted = self.root / "promoted"
        cli.rewrite_rollouts(self.dest, promoted)
        cli.validate_copy(promoted)
        with database(promoted / cli.DATABASES[0]) as db:
            self.assertEqual(dict(db.execute("SELECT id, rollout_path FROM threads")),
                             {tid: str(promoted / relative) for tid, relative in rollouts.items()})

    def test_promote_uses_active_home_and_keeps_history_view(self):
        config = Config(self.source, self.dest, self.root / "backups", self.root / "git", "unused",
                        "main", 5, 0)
        (self.dest / "config.toml").touch()
        test_log = self.root / "dest-desktop.log"
        test_log.touch()
        viewing = self.root / ".codex-history-view"
        viewing.mkdir()
        with database(self.dest / cli.DATABASES[0]) as db:
            for tid, relative in ((LIVE, rollout(LIVE, LAST)), (ARCHIVE, rollout(ARCHIVE, archived=True, compressed=True))):
                db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(self.dest / relative), tid))

        events = []

        @contextlib.contextmanager
        def suspend():
            events.append("stop service")
            try:
                yield
            finally:
                events.append("start service")

        def launch(command, home, log):
            events.append("launch")
            self.assertEqual(command, ["desktop"])
            self.assertEqual(home, config.formal)
            cli.validate_copy(home)

        with patch.object(cli, "suspend_windows_sandbox", suspend), patch.object(cli.Desktop, "command",
                 return_value=["desktop"]), patch.object(cli.Desktop, "require_external_windows_promotion"), \
                patch.object(cli, "windows_sandbox", return_value=None), patch.object(
                    cli.Desktop, "stop_for_promotion", side_effect=lambda: events.append("close desktop")
                ), patch.object(cli.Desktop, "launch", side_effect=launch):
            cli.promote_and_launch(SimpleNamespace(no_launch=False), config, self.root / "config.toml")

        self.assertEqual(events, ["close desktop", "stop service", "start service", "launch"])
        self.assertFalse(self.dest.exists())
        self.assertFalse(test_log.exists())
        self.assertTrue(viewing.is_dir())
        (previous,) = config.backups.glob("codex-*/codex")
        self.assertTrue((previous / cli.DATABASES[0]).is_file())

    @unittest.skipUnless(os.name == "nt", "Windows rejects directory moves with open descendants")
    def test_promote_rolls_back_when_a_session_file_is_locked(self):
        config = Config(self.source, self.dest, self.root / "backups", self.root / "git", "unused",
                        "main", 5, 0)
        (self.dest / "config.toml").touch()
        with database(self.dest / cli.DATABASES[0]) as db:
            for tid, relative in ((LIVE, rollout(LIVE, LAST)), (ARCHIVE, rollout(ARCHIVE, archived=True, compressed=True))):
                db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(self.dest / relative), tid))

        original_id = self.source.stat().st_ino
        test_id = self.dest.stat().st_ino
        stderr = io.StringIO()
        with (
            patch.object(cli, "suspend_windows_sandbox", contextlib.nullcontext),
            patch.object(cli.Desktop, "stop_for_promotion"),
            patch.object(cli.time, "sleep") as retry_wait,
            patch.object(cli.shutil, "copytree", side_effect=AssertionError("Promotion must move existing data")),
            contextlib.redirect_stderr(stderr),
        ):
            with (self.dest / rollout(LIVE, LAST)).open("rb"):
                with self.assertRaisesRegex(ValueError, "blocked a promotion file operation after retries"):
                    cli.promote(config)
            self.assertGreaterEqual(retry_wait.call_count, 3)
            self.assertIn("Restored the formal and test session files", stderr.getvalue())
            self.assertEqual(self.source.stat().st_ino, original_id)
            self.assertEqual(self.dest.stat().st_ino, test_id)
            self.assertEqual(list(config.backups.iterdir()), [])
            cli.validate_copy(self.dest)
            cli.promote(config)

        self.assertFalse(self.dest.exists())
        self.assertEqual(self.source.stat().st_ino, test_id)
        cli.validate_copy(config.formal)
        (previous,) = config.backups.glob("codex-*/codex")
        self.assertEqual(previous.stat().st_ino, original_id)

    @unittest.skipUnless(os.name == "nt", "Windows rejects directory moves with open descendants")
    def test_promote_moves_session_files_when_runtime_file_is_locked(self):
        config = Config(self.source, self.dest, self.root / "backups", self.root / "git", "unused",
                        "main", 5, 0)
        for home in (self.source, self.dest):
            (home / "packages").mkdir()
            (home / "packages" / "runtime").write_text(home.name, encoding="utf-8")
            (home / "attachments").mkdir()
            (home / "attachments" / "fixture").write_text(home.name, encoding="utf-8")
            (home / "config.toml").write_text(home.name, encoding="utf-8")
            (home / ".codex-global-state.json").write_text(home.name, encoding="utf-8")
        with database(self.dest / cli.DATABASES[0]) as db:
            for tid, relative in ((LIVE, rollout(LIVE, LAST)), (ARCHIVE, rollout(ARCHIVE, archived=True, compressed=True))):
                db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(self.dest / relative), tid))

        active_id = self.source.stat().st_ino
        candidate_log_id = (self.dest / rollout(LIVE, LAST)).stat().st_ino
        with (
            (self.dest / "packages" / "runtime").open("rb"),
            patch.object(cli, "suspend_windows_sandbox", contextlib.nullcontext),
            patch.object(cli.Desktop, "stop_for_promotion"),
            patch.object(cli.time, "sleep"),
            patch.object(cli.shutil, "copytree", side_effect=AssertionError("Promotion must move existing data")),
        ):
            cli.promote(config)

        self.assertEqual(self.source.stat().st_ino, active_id)
        self.assertEqual((self.source / rollout(LIVE, LAST)).stat().st_ino, candidate_log_id)
        self.assertFalse((self.dest / rollout(LIVE, LAST)).exists())
        cli.validate_copy(config.formal)
        (previous,) = config.backups.glob("codex-*/codex")
        for name in ("packages/runtime", "config.toml"):
            self.assertEqual((self.source / name).read_text(encoding="utf-8"), "source")
            self.assertFalse((previous / name).exists())
        for name in ("attachments/fixture", ".codex-global-state.json"):
            self.assertEqual((self.source / name).read_text(encoding="utf-8"), "dest")
            self.assertEqual((previous / name).read_text(encoding="utf-8"), "source")

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

    def promotion_fixture(self):
        copy = self.root / ".codex_copy"
        copy.mkdir()
        config = Config(self.source, copy / ".codex", self.root / "backups", self.root / "git",
                        "unused", "main", 5, 0)
        self.dest.rename(config.test)
        (config.test / "config.toml").touch()
        (config.test / cli.RECEIPT).write_text(json.dumps({"format": 1, "workspaces": {}}))
        model = Snapshot(config.test)
        maps = Workspaces(model, {WINDOWS: None}, config.history_view)
        rewrite_logs(model, config.test, maps)
        update_indexes(model, config.test)
        relocate(model, config.test, config.test, maps)
        return config

    def test_promote_removes_dedicated_copy_and_keeps_stable_history_workspace(self):
        config = self.promotion_fixture()
        copy = config.test.parent
        runtime = config.test_runtime
        runtime.mkdir()
        (runtime / cli.LEGACY_RUNTIME_MARKER).write_text(json.dumps({
            "format": 1, "test_home": str(config.test),
        }))
        (copy / ".session-repair-abcdefgh" / "repaired").mkdir(parents=True)
        (copy / ".history-check-12345678").mkdir()
        (copy / ".codex-desktop.log").touch()
        (copy / ".codex.codex-migration.lock").touch()
        mapping = copy / "workspace-map-codex-git-20260101-000000.json"
        mapping.write_text('{"old": null}')
        with patch.object(cli, "suspend_windows_sandbox", contextlib.nullcontext):
            cli.promote(config, SimpleNamespace(stop_for_promotion=lambda: None))
        self.assertFalse(copy.exists())
        self.assertFalse(runtime.exists())
        self.assertTrue(config.history_view.is_dir())
        self.assertEqual((copy.parent / mapping.name).read_text(), '{"old": null}')
        current = Snapshot(config.formal)
        self.assertEqual(current.threads[LIVE].cwd, str(config.history_view))
        self.assertEqual(len(current.threads[LIVE].recorded_turns()), 3)
        self.assertTrue(current.threads[ARCHIVE].archived)
        self.assertEqual(len(list(config.backups.glob("*/codex"))), 1)

    def test_reopen_marker_allows_extra_threads_and_detects_baseline_change(self):
        config = Config(self.source, self.dest, self.root / "backups", self.root / "git", "unused",
                        "main", 5, 0)
        (self.dest / RECEIPT).write_text(json.dumps({
            "format": 1,
            "commit": "a" * 40,
            "snapshot": "codex-git-20260101-000000",
            "client_version": "codex-cli fixture",
            "workspaces": {},
            "overrides": [],
            "history_only": [],
        }))
        marker = cli.write_test_marker(config)
        # Treat ARCHIVE as a later GUI-created thread by keeping only LIVE in the accepted baseline.
        marker.update(cli.semantic_fingerprints(self.dest, [LIVE]))
        (self.dest / cli.TEST_MARKER).write_text(json.dumps(marker))
        self.assertEqual(cli.validate_test_marker(config)["threads"], [LIVE])

        with database(self.dest / cli.DATABASES[0]) as db:
            db.execute("UPDATE threads SET title='changed during test' WHERE id=?", (LIVE,))
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            cli.validate_test_marker(config)

    def test_desktop_test_lifecycle_uses_and_protects_test_home(self):
        config = Config(self.source, self.dest, self.root / "backups", self.root / "git", "unused",
                        "main", 5, 0)
        desktop = codex_desktop.Desktop(config)
        process = SimpleNamespace(pid=17, name="Codex.exe", home=str(config.test),
                                  databases=(), environment_readable=True)
        with patch.object(desktop, "processes", return_value=[process]):
            with self.assertRaisesRegex(ValueError, "test copy"):
                desktop.require_test_idle()
        with patch.object(desktop, "launch") as launch:
            desktop.launch_test(["desktop"])
        launch.assert_called_once_with(
            ["desktop"], config.test,
            config.test.parent / f"{config.test.name}-desktop.log",
        )

    def test_workspace_mappings_cannot_target_either_disposable_home(self):
        runtime = self.dest.with_name(self.dest.name + "-runtime")
        outside = self.root / "persistent-project"
        for home in (self.dest, runtime):
            with self.subTest(home=home), self.assertRaisesRegex(ValueError, "disposable test home"):
                validate_workspace_targets([str(home / "project")], (self.dest, runtime))
        validate_workspace_targets([str(outside)], (self.dest, runtime))

    def test_map_draft_reuses_valid_decisions_and_reports_unresolved_paths(self):
        available = self.root / "available"
        mapped = self.root / "mapped"
        available.mkdir()
        mapped.mkdir()
        missing_mapped = "/source/mapped"
        missing_history = "/source/history-only"
        missing_new = "/source/needs-decision"
        draft_path = self.root / "workspace-map.json"
        draft_path.write_text(json.dumps({missing_mapped: str(mapped), missing_history: None, "stale": None}))
        plan = [
            {"source": str(available), "available": True, "uses": []},
            {"source": missing_mapped, "available": False, "uses": []},
            {"source": missing_history, "available": False, "uses": []},
            {"source": missing_new, "available": False, "uses": []},
        ]

        path, unresolved, history_only = write_map_draft(draft_path, plan)

        self.assertEqual(path, draft_path)
        self.assertEqual(unresolved, [missing_new])
        self.assertEqual(history_only, [missing_history])
        self.assertEqual(json.loads(draft_path.read_text()), {
            str(available): str(available),
            missing_mapped: str(mapped),
            missing_history: None,
            missing_new: None,
        })

    def test_windows_rename_retries_sharing_violations(self):
        source, destination = self.root / "before", self.root / "after"
        source.write_text("runtime", encoding="utf-8")
        original_rename = Path.rename
        attempts = 0

        def rename(path, target):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                error = OSError("sharing violation")
                error.winerror = 32
                raise error
            return original_rename(path, target)

        with patch.object(cli.sys, "platform", "win32"), patch.object(Path, "rename", rename), \
                patch.object(cli.time, "sleep"):
            cli.rename_for_promotion(source, destination)
        self.assertEqual(attempts, 2)
        self.assertEqual(destination.read_text(encoding="utf-8"), "runtime")

    def test_session_swap_preserves_non_session_profile_and_clears_marker(self):
        candidate = self.root / "candidate"
        work = self.root / "work"
        work.mkdir()
        session_copy(self.source, candidate)
        (candidate / RECEIPT).write_text('{"version":"new"}')
        (candidate / "session_index.jsonl").write_text('{"new":true}\n')
        (self.dest / RECEIPT).write_text('{"version":"old"}')
        (self.dest / cli.TEST_MARKER).write_text('{"format":1}')
        runtime = self.dest / "packages" / "app-server" / "binary"
        runtime.parent.mkdir(parents=True)
        runtime.write_bytes(b"local runtime")
        inode = runtime.stat().st_ino

        swap_candidate(candidate, self.dest, work)

        self.assertEqual(runtime.read_bytes(), b"local runtime")
        self.assertEqual(runtime.stat().st_ino, inode)
        self.assertEqual((self.dest / RECEIPT).read_text(), '{"version":"new"}')
        self.assertEqual((self.dest / "session_index.jsonl").read_text(), '{"new":true}\n')
        self.assertFalse((self.dest / cli.TEST_MARKER).exists())
        self.assertFalse((candidate / "packages").exists())

    def test_failed_session_swap_rolls_back_without_touching_profile(self):
        candidate = self.root / "candidate"
        work = self.root / "work"
        work.mkdir()
        session_copy(self.source, candidate)
        (candidate / RECEIPT).write_text('{"version":"new"}')
        (self.dest / RECEIPT).write_text('{"version":"old"}')
        (self.dest / cli.TEST_MARKER).write_text('{"format":1}')
        package = self.dest / "plugins" / "cache" / "entry"
        package.parent.mkdir(parents=True)
        package.write_text("local")
        original_rename = Path.rename

        def fail_install(path, destination):
            if path == candidate / "session_index.jsonl" and destination == self.dest / "session_index.jsonl":
                raise OSError("simulated session install failure")
            return original_rename(path, destination)

        with patch.object(Path, "rename", fail_install):
            with self.assertRaisesRegex(OSError, "simulated session install failure"):
                swap_candidate(candidate, self.dest, work)

        self.assertEqual((self.dest / RECEIPT).read_text(), '{"version":"old"}')
        self.assertTrue((self.dest / cli.TEST_MARKER).is_file())
        self.assertEqual(package.read_text(), "local")

    def test_legacy_workspaces_and_user_files_survive_cleanup(self):
        config = self.promotion_fixture()
        copy = config.test.parent
        view = copy / ".codex-history-view"
        view.mkdir()
        visual = config.test / "visualizations" / "fixture"
        visual.mkdir(parents=True)
        (visual / "plot").write_bytes(b"plot")
        (config.test / "packages").mkdir()
        (config.test / "packages" / "large").touch()
        unknown = copy / "notes.txt"
        unknown.write_text("mine")
        mapping = copy / "workspace-map-codex-git-20260101-000000.json"
        mapping.write_text("new")
        (copy.parent / mapping.name).write_text("existing")
        # The rollback uses the old viewing directory. Only a structured log field refers to the plot.
        with database(config.formal / cli.DATABASES[0]) as db:
            db.execute("UPDATE threads SET cwd=?", (str(view),))
        path = config.test / rollout(LIVE, LAST)
        with path.open("ab") as f:
            f.write(json.dumps({"type": "turn_context", "payload": {"workspace_roots": [str(visual)]}}).encode() + b"\n")
        with patch.object(cli, "suspend_windows_sandbox", contextlib.nullcontext):
            cli.promote(config, SimpleNamespace(stop_for_promotion=lambda: None))
        self.assertTrue(view.is_dir())
        self.assertEqual((visual / "plot").read_bytes(), b"plot")
        self.assertFalse((config.test / "packages").exists())
        self.assertEqual(unknown.read_text(), "mine")
        self.assertEqual(mapping.read_text(), "new")
        self.assertEqual((copy.parent / mapping.name).read_text(), "existing")
        self.assertEqual(len(list(config.backups.glob("*/codex"))), 1)
        remapped = Workspaces(SimpleNamespace(places={str(view): {(LIVE, "cwd")}}), {},
                              config.history_view, legacy_view=view)
        self.assertEqual(remapped.cwd(str(view)), str(config.history_view))

    def test_failed_promotion_keeps_copy_and_repair_staging(self):
        config = self.promotion_fixture()
        stage = config.test.parent / ".session-repair-aaaaaaaa"
        stage.mkdir()
        with cli.directory_lock(config.test_lock), self.assertRaisesRegex(ValueError, "Could not lock"):
            cli.promote(config)
        self.assertTrue(stage.is_dir())
        self.assertTrue((config.test / cli.RECEIPT).is_file())

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


@unittest.skipUnless(os.name == "nt", "tests Windows service control")
class WindowsPromotion(unittest.TestCase):
    def test_restart_sandbox_service_after_promotion_failure(self):
        state = "running"
        calls = []

        def control(command, **options):
            nonlocal state
            action = command[1]
            calls.append(action)
            self.assertEqual(command[2], codex_desktop.WINDOWS_SANDBOX)
            state = {"stop": "stopped", "start": "running"}[action]
            return subprocess.CompletedProcess(command, 0)

        with (
            patch.object(codex_desktop, "windows_sandbox", return_value=SimpleNamespace(status=lambda: state)),
            patch.object(codex_desktop, "windows_admin", return_value=True),
            patch.object(codex_desktop.subprocess, "run", side_effect=control),
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture error"):
                with codex_desktop.suspend_windows_sandbox():
                    self.assertEqual(state, "stopped")
                    raise RuntimeError("fixture error")
        self.assertEqual(state, "running")
        self.assertEqual(calls, ["stop", "start"])


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
            config.write_text("[paths]\nformal_home='formal'\ntest_home='.codex_copy/.codex'\nbackup_root='rollback'\n"
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
            prepared = root / ".codex_copy/.codex"
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
            model = Snapshot(prepared)
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
            # Repair from the pin again, promote, reopen the history after cleanup, then prepare afresh.
            cli.main(repair)
            formal = root / "formal"
            for relative in cli.SESSION_FILES:
                target = formal / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((source / relative).read_bytes())
            with patch.object(cli, "suspend_windows_sandbox", contextlib.nullcontext):
                cli.promote(cli.load_config(config), SimpleNamespace(stop_for_promotion=lambda: None))
            self.assertFalse(prepared.parent.exists())
            after = read_probe(Snapshot(formal), SERVER, root / "promoted-reader", size=1, resume=True)
            compare_history(result, after)
            cli.main([*args, "prepare", name])
            self.assertTrue((prepared / cli.RECEIPT).is_file())
