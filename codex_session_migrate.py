#!/usr/bin/env python3
"""Back up, prepare, validate and promote a Codex session snapshot."""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory, mkdtemp

from backup_codex_sessions import backup
from codex_desktop import Desktop, suspend_windows_sandbox, windows_admin, windows_sandbox
from codex_session_common import (
    DATABASES, DEFAULT_CONFIG, LOG_DIRECTORIES, PROJECT, SESSION_FILES, SNAPSHOT_NAME,
    Repository, check_database, connect, directory_lock, is_redirected, load_config,
    rollout_relative,
)
from prepare_codex_copy import capture_runtime_links, install_runtime_links, stage_copy
from restore_rollouts import Snapshot, catalog_rows, records, workspace_fields
from restore_snapshot import RECEIPT, pin, remove_scratch, repair, verify


COMMAND_ERRORS = (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError, EOFError)
TEST_MARKER = ".session-test.json"
LEGACY_RUNTIME_MARKER = ".session-test-runtime.json"


def require_formal(config):
    if not config.formal.is_dir():
        raise ValueError(f"Formal Codex data directory is missing: {config.formal}")


def select_snapshot(repo, name):
    if not repo.has_ref(repo.ref):
        raise ValueError(f"No remote branch {repo.config.branch!r} in the backup repository.")
    revision = repo.output("rev-parse", repo.ref)
    choices = [
        item for item in repo.output("ls-tree", "-d", "--name-only", revision).splitlines()
        if SNAPSHOT_NAME.fullmatch(item)
    ]
    if name is None:
        if not choices:
            raise ValueError(f"No snapshot directories on {repo.config.branch}.")
        return revision, max(choices)
    if not SNAPSHOT_NAME.fullmatch(name):
        raise ValueError("Specify a snapshot named codex-git-YYYYMMDD-HHMMSS.")
    if name in choices:
        return revision, name
    for candidate in repo.output("log", "--format=%H", repo.ref, "--", name).splitlines():
        if repo.output("ls-tree", "-d", "--name-only", candidate, "--", name) == name:
            return candidate, name
    raise ValueError(f"Snapshot not found in {repo.config.branch}: {name}")


def extract_snapshot(repo, revision, name, staging):
    # Read committed bytes, including files subject to export-ignore or checkout EOL conversion.
    git = ["git", "-C", str(repo.path)]
    tree = subprocess.check_output([*git, "ls-tree", "-rz", "--full-tree", revision, "--", name])
    with subprocess.Popen([*git, "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
        try:
            for entry in tree.split(b"\0"):
                if not entry:
                    continue
                header, raw_path = entry.split(b"\t", 1)
                mode, kind, oid = header.split()
                relative = PurePosixPath(raw_path.decode("utf-8"))
                if (len(relative.parts) < 2 or relative.parts[0] != name or ".." in relative.parts
                        or any("\\" in part or ":" in part for part in relative.parts)):
                    raise ValueError(f"Invalid snapshot path: {relative}")
                remainder = relative.parts[1:]
                if remainder[0] not in (*LOG_DIRECTORIES, "sqlite", *SESSION_FILES):
                    continue
                if os.name == "nt" and any(Path(part).is_reserved() or part.endswith((".", " "))
                                           for part in relative.parts):
                    raise ValueError(f"Snapshot path is incompatible with Windows: {relative}")
                if (mode not in (b"100644", b"100755") or kind != b"blob" or
                        not ("/".join(remainder) in SESSION_FILES or
                             (remainder[0] in LOG_DIRECTORIES and len(remainder) > 1))):
                    raise ValueError(f"Unexpected snapshot entry: {relative}")
                process.stdin.write(oid + b"\n")
                process.stdin.flush()
                reply = process.stdout.readline().split()
                if len(reply) != 3 or reply[:2] != [oid, b"blob"]:
                    raise ValueError(f"Missing committed blob for {relative}")
                left = int(reply[2])
                path = staging.joinpath(*relative.parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as target:
                    while left:
                        chunk = process.stdout.read(min(left, 1024 * 1024))
                        if not chunk:
                            raise ValueError(f"Incomplete committed blob for {relative}")
                        target.write(chunk)
                        left -= len(chunk)
                if process.stdout.read(1) != b"\n":
                    raise ValueError(f"Invalid Git blob boundary for {relative}")
            process.stdin.close()
            if process.wait():
                raise subprocess.CalledProcessError(process.returncode, process.args)
        except BaseException:
            if process.poll() is None:
                process.kill()
            raise
    return staging / name


def prepare(config, version):
    require_formal(config)
    repo = Repository(config)
    with directory_lock(config.formal), directory_lock(config.test_lock), directory_lock(repo.path):
        desktop = Desktop(config)
        desktop.require_test_idle()
        repo.fetch()
        revision, name = select_snapshot(repo, version)
        print(f"Preparing {name} from {revision}", flush=True)
        config.test.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(mkdtemp(prefix=config.scratch_prefix("preparing"), dir=config.test.parent))
        candidate, previous = staging / "home", staging / "previous-home"
        try:
            snapshot = extract_snapshot(repo, revision, name, staging)
            stage_copy(config.formal, snapshot, candidate)
            (candidate / TEST_MARKER).unlink(missing_ok=True)
            pin(repo, revision, name, candidate)
            desktop.require_test_idle()
            if config.test.is_dir():
                config.test.rename(previous)
            candidate.rename(config.test)
        finally:
            if previous.exists() and candidate.exists():
                try:
                    previous.rename(config.test)
                except OSError as error:
                    raise ValueError(f"Previous test copy is available at {previous}") from error
            shutil.rmtree(staging)
    skill = PROJECT / ".agents/skills/codex-session-restore/SKILL.md"
    print(f"Prepared test copy: {config.test}")
    print(f"Ask Codex to repair this copy using {skill}")
    print("After repair and test --check-only pass, run test from an external terminal to open this copy directly.")


def validate_copy(home):
    for filename in DATABASES:
        database = home / filename
        with connect(database) as db:
            check_database(db, database)
    with connect(home / DATABASES[0]) as db:
        rows = db.execute("SELECT id, rollout_path FROM threads").fetchall()
    invalid = []
    for thread, path in rows:
        relative = rollout_relative(path, home)
        log = home / relative if relative is not None else None
        if log is None or log.resolve() != log or not log.is_file():
            invalid.append((thread, path))
    if invalid:
        for thread, path in invalid[:10]:
            print(f"  {thread}: {path}", file=sys.stderr)
        raise ValueError(f"{len(invalid)} rollout paths are missing or outside {home}. Repair the copy first.")
    print(f"Checked {len(DATABASES)} databases and {len(rows)} rollout paths in {home}")


def rewrite_rollouts(old, new):
    with connect(new / DATABASES[0], "rw") as db, db:
        db.execute("BEGIN IMMEDIATE")
        thread_paths = []
        for thread, path in db.execute("SELECT id, rollout_path FROM threads"):
            relative = rollout_relative(path, old) or rollout_relative(path, new)
            target = new / relative if relative is not None else None
            if target is None or target.resolve() != target or not target.is_file():
                raise ValueError(f"Could not locate the rollout for {thread} under {new}: {path}")
            thread_paths.append((str(target), thread))
        db.executemany("UPDATE threads SET rollout_path=? WHERE id=?", thread_paths)
        skipped_table = db.execute("""
            SELECT name FROM sqlite_schema WHERE type='table' AND name='rollout_migration_skipped_rollouts'
        """).fetchone()
        if skipped_table:
            paths = db.execute("SELECT DISTINCT rollout_path FROM rollout_migration_skipped_rollouts").fetchall()
            for (path,) in paths:
                relative = rollout_relative(path, old)
                if relative is not None:
                    db.execute("UPDATE rollout_migration_skipped_rollouts SET rollout_path=? WHERE rollout_path=?",
                               (str(new / relative), path))
    print(f"Verified rollout paths in {new}")


def stable_digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def receipt_for_test(home):
    path = home / RECEIPT
    if not path.is_file() or path.resolve() != path:
        raise ValueError(f"Missing prepared snapshot receipt: {path}")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict):
        raise ValueError(f"Invalid prepared snapshot receipt: {path}")
    return receipt


def semantic_fingerprints(home, baseline_threads=None):
    snapshot = Snapshot(home)
    current = snapshot.threads
    threads = sorted(current) if baseline_threads is None else list(baseline_threads)
    if any(not isinstance(thread, str) for thread in threads) or len(threads) != len(set(threads)):
        raise ValueError("Invalid thread list in the test marker")
    missing = sorted(set(threads) - current.keys())
    if missing:
        raise ValueError(f"Restored threads are missing after the desktop test: {missing[:10]}")

    metadata, turns, items = [], [], []
    for thread_id in threads:
        thread = current[thread_id]
        metadata.append([thread_id, thread.title, thread.archived])
        turns.append([thread_id, thread.recorded_turns()])
        messages = []
        for (turn_id, item_id), digest in thread.visible_records("messages"):
            messages.append([[turn_id, item_id], digest.hex()])
        items.append([thread_id, messages])
    return {
        "threads": threads,
        "thread_ids_digest": stable_digest(threads),
        "title_archive_digest": stable_digest(metadata),
        "turn_ids_digest": stable_digest(turns),
        "item_messages_digest": stable_digest(items),
    }


def mapping_digest(receipt):
    return stable_digest({
        "workspaces": receipt.get("workspaces"),
        "overrides": receipt.get("overrides"),
        "history_only": receipt.get("history_only"),
    })


def write_test_marker(config):
    receipt = receipt_for_test(config.test)
    marker = {
        "format": 1,
        "test_home": str(config.test),
        "commit": receipt.get("commit"),
        "snapshot": receipt.get("snapshot"),
        "client_version": receipt.get("client_version"),
        "mapping_digest": mapping_digest(receipt),
        "verified_at": int(time.time()),
        "launched": False,
        **semantic_fingerprints(config.test),
    }
    path = config.test / TEST_MARKER
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(marker, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return marker


def validate_test_marker(config):
    path = config.test / TEST_MARKER
    if not path.is_file() or path.resolve() != path:
        raise ValueError("No verified desktop-test marker. Run test --check-only first.")
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
        receipt = receipt_for_test(config.test)
        required = {
            "threads", "thread_ids_digest", "title_archive_digest", "turn_ids_digest", "item_messages_digest",
        }
        if (not isinstance(marker, dict) or marker.get("format") != 1 or not required <= marker.keys()
                or Path(marker.get("test_home", "")).resolve() != config.test
                or marker.get("commit") != receipt.get("commit")
                or marker.get("snapshot") != receipt.get("snapshot")
                or marker.get("client_version") != receipt.get("client_version")
                or marker.get("mapping_digest") != mapping_digest(receipt)):
            raise ValueError
        current = semantic_fingerprints(config.test, marker["threads"])
        if any(marker.get(key) != current[key] for key in required - {"threads"}):
            raise ValueError
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("The verified desktop-test marker no longer matches the restored sessions. "
                         "Run repair, then test --check-only again.") from error
    return marker


def mark_test_launched(config, marker, *, reopened=False):
    marker = dict(marker)
    marker["launched"] = True
    marker["last_launched_at"] = int(time.time())
    marker["launch_count"] = int(marker.get("launch_count", 0)) + 1
    if reopened:
        marker["last_launch_was_reopen"] = True
    path = config.test / TEST_MARKER
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(marker, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def report_formal_changes(config):
    with connect(config.formal / DATABASES[0]) as formal, connect(config.test / DATABASES[0]) as test:
        versions = []
        for db in (formal, test):
            columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
            timestamps = []
            if "updated_at_ms" in columns:
                timestamps.append("updated_at_ms")
            if "updated_at" in columns:
                timestamps.append("updated_at * 1000")
            expression = f"COALESCE({', '.join([*timestamps, '0'])})" if timestamps else "0"
            versions.append(dict(db.execute(f"SELECT id, {expression} FROM threads")))
    previous, current = versions
    absent = len(previous.keys() - current.keys())
    newer = sum(previous[key] > current[key] for key in previous.keys() & current.keys())
    if absent or newer:
        print(f"The pre-promotion backup will retain {absent} formal-only sessions and {newer} newer formal versions.")


def prune_promotion_backups(current):
    try:
        candidates = list(current.parent.iterdir())
    except OSError as error:
        print(f"WARNING: Promotion succeeded, but backup cleanup could not list {current.parent}: {error}",
              file=sys.stderr)
        return
    for candidate in candidates:
        if candidate == current or not re.fullmatch(r"codex-[0-9]{8}-[0-9]{6}", candidate.name):
            continue
        previous = candidate / "codex"
        try:
            if (is_redirected(candidate.lstat()) or not previous.is_dir()
                    or is_redirected(previous.lstat()) or list(candidate.iterdir()) != [previous]):
                continue
            shutil.rmtree(candidate)
            print(f"Removed old promotion backup: {candidate}")
        except OSError as error:
            print(f"WARNING: Promotion succeeded, but backup cleanup failed for {candidate}: {error}",
                  file=sys.stderr)


def copy_workspace_paths(home):
    """Include log-only roots, even for archived threads and chats added during the desktop test."""
    with connect(home / DATABASES[0]) as db:
        paths = {cwd for cwd, in db.execute("SELECT DISTINCT cwd FROM threads") if cwd}
    with connect(home / DATABASES[2]) as db:
        paths.update(cwd for (host, _), (_, cwd) in catalog_rows(db).items() if host == "local" and cwd)
    for directory in LOG_DIRECTORIES:
        for log in (home / directory).rglob("*"):
            if log.resolve() != log or is_redirected(log.lstat()):
                raise ValueError(f"Redirected rollout in {home}: {log}")
            if log.is_file():
                for _, record in records(log):
                    for _, values in workspace_fields(record):
                        paths.update(values)
    return paths


def referenced_copy_paths(root, *homes):
    paths = set()
    for home in homes:
        for value in copy_workspace_paths(home):
            path = Path(value)
            if path.is_absolute() and path.is_dir():
                paths.update(p for p in (path, path.resolve()) if p.is_relative_to(root))
    return paths


def remove_test_runtime(path, keep):
    if path in keep:
        print(f"Kept referenced path: {path}")
    elif any(p.is_relative_to(path) for p in keep):
        if path.is_dir() and path.resolve() == path:
            for child in path.iterdir():
                remove_test_runtime(child, keep)
    elif path.is_symlink() or not path.is_dir():
        path.unlink()
    elif is_redirected(path.lstat()):
        path.rmdir()  # Unlink the Windows junction itself.
    else:
        remove_scratch(path)


def owned_legacy_runtime(config, runtime):
    """Recognize old disposable runtimes only so promotion can reclaim their storage."""
    marker = runtime / LEGACY_RUNTIME_MARKER
    try:
        metadata = runtime.lstat()
        owner = json.loads(marker.read_text(encoding="utf-8"))
        return (runtime.resolve() == runtime and not is_redirected(metadata) and runtime.is_dir()
                and marker.resolve() == marker and owner.get("format") == 1
                and Path(owner.get("test_home", "")).resolve() == config.test)
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def cleanup_after_promotion(config, references):
    root = config.test.parent
    if not root.exists():
        return
    try:
        if root.resolve() != root or is_redirected(root.lstat()):
            raise ValueError(f"Redirected copy directory: {root}")
        protected = references | {config.formal, config.backups, config.checkout, PROJECT, Path.cwd(), config.history_view}
        if root in references:
            print(f"Kept the copy directory because it is a session workspace: {root}")
            return

        scratch = "|".join(re.escape(config.scratch_prefix(op)) for op in (
            "session-repair", "history-check", "preparing", "test-runtime",
        ))
        legacy_lock = f"{config.test.name}.codex-migration.lock"
        test_logs = {f"{config.test.name}-desktop.log", f"{config.test_runtime.name}-desktop.log"}
        for entry in root.iterdir():
            if entry.resolve() != entry or is_redirected(entry.lstat()):
                continue
            if entry != config.test and any(path.is_relative_to(entry) for path in protected):
                if config.dedicated_copy:
                    print(f"Kept referenced path: {entry}")
                continue
            try:
                if entry == config.test:
                    # A completed file-by-file promotion has moved every session file out.
                    if any((entry / rel).exists() for rel in promotion_session_paths(entry)):
                        raise ValueError(f"The test home still contains session data: {entry}")
                    remove_test_runtime(entry, protected)
                elif entry == config.test_runtime:
                    if owned_legacy_runtime(config, entry):
                        remove_test_runtime(entry, protected)
                elif entry.is_dir() and re.fullmatch(f"(?:{scratch})[a-z0-9_]{{8}}", entry.name):
                    children = {child.name for child in entry.iterdir()}
                    # A failed prepare can leave the only copy of a previous test home here.
                    if "previous-home" in children:
                        print(f"Kept previous test home in {entry}")
                    elif "previous-runtime" in children:
                        print(f"Kept previous desktop runtime in {entry}")
                    elif all(SNAPSHOT_NAME.fullmatch(name) or name in {
                            "home", "input", "repaired", "previous", "baseline", "candidate", "without-override",
                            "previous-runtime",
                    } for name in children):
                        remove_scratch(entry)
                elif entry.is_file() and (entry.name in test_logs or
                        config.dedicated_copy and entry.name == legacy_lock and entry.stat().st_size == 0):
                    entry.unlink()
                elif (config.dedicated_copy and entry.is_file()
                      and re.fullmatch(r"workspace-map-codex-git-[0-9]{8}-[0-9]{6}\.json", entry.name)):
                    destination = root.parent / entry.name
                    if not destination.exists():
                        os.link(entry, destination)  # Same filesystem, fail rather than overwrite.
                    if entry.samefile(destination):
                        entry.unlink()
                        print(f"Saved workspace mapping at {destination}")
                elif config.dedicated_copy and entry.name == ".codex-history-view" and entry.is_dir():
                    if not any(entry.iterdir()):
                        entry.rmdir()
            except OSError as error:
                print(f"WARNING: Could not clean {entry}: {error}", file=sys.stderr)
        if config.dedicated_copy and root not in protected:
            if not any(root.iterdir()):
                root.rmdir()
                print(f"Removed test copy directory: {root}")
            else:
                print(f"Remaining entries in {root}: " + ", ".join(sorted(p.name for p in root.iterdir())))
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"WARNING: Promotion data is in place. Copy cleanup stopped: {error}", file=sys.stderr)


def windows_move_blocked(error):
    return sys.platform == "win32" and getattr(error, "winerror", None) in (5, 32)


def rename_for_promotion(source, destination):
    for delay in (0.15, 0.35, 0.75, None):
        try:
            source.rename(destination)
            return
        except OSError as error:
            if delay is None or not windows_move_blocked(error):
                raise
            time.sleep(delay)


def promotion_session_paths(home):
    def walk_error(error):
        raise error

    paths = {
        *(Path(name) for name in (*SESSION_FILES, RECEIPT, ".codex-global-state.json",
                                  ".codex-global-state.json.bak", ".codex-global-state.sqlite")),
        *(Path(name + suffix) for name in (*DATABASES, ".codex-global-state.sqlite")
          for suffix in ("-wal", "-shm", "-journal")),
    }
    for directory in (*LOG_DIRECTORIES, "attachments"):
        base = home / directory
        if base.is_symlink() or base.resolve() != base:
            raise ValueError(f"Redirected session directory: {base}")
        if not base.exists():
            continue
        if not base.is_dir():
            raise ValueError(f"Expected a session directory: {base}")
        for folder, directories, files in os.walk(base, onerror=walk_error):
            root = Path(folder)
            for name in directories + files:
                entry = root / name
                if is_redirected(entry.lstat()):
                    raise ValueError(f"Redirected session path: {entry}")
            paths.update((root / name).relative_to(home) for name in files)
    return paths


def promote_session_files(config, previous):
    formal, test = config.formal, config.test
    state = test / DATABASES[0]
    # The path rewrite opens this database after moving it. Finish its WAL
    # first so that opening it cannot consume sidecars needed for rollback.
    try:
        with connect(state, "rw") as db:
            if db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise ValueError(f"Could not finish the SQLite journal for {state}")
    except sqlite3.Error as error:
        raise ValueError(f"Could not finish the SQLite journal for {state}: {error}") from error

    paths = sorted(promotion_session_paths(formal) | promotion_session_paths(test))
    files = {}
    for home in (formal, test):
        files[home] = []
        for relative in paths:
            entry = home / relative
            if entry.resolve() != entry or entry.is_symlink():
                raise ValueError(f"Redirected session path: {entry}")
            if entry.exists():
                if not entry.is_file():
                    raise ValueError(f"Expected a session file: {entry}")
                files[home].append(relative)

    saved, installed = [], []
    try:
        for source, target, moved in ((formal, previous, saved), (test, formal, installed)):
            for relative in files[source]:
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                rename_for_promotion(source / relative, destination)
                moved.append(relative)
        rewrite_rollouts(test, formal)
    except BaseException:
        try:
            for source, target, moved in ((formal, test, installed), (previous, formal, saved)):
                for relative in reversed(moved):
                    rename_for_promotion(source / relative, target / relative)
        except OSError as error:
            raise ValueError(f"Session-file rollback stopped. Check {formal}, {test}, and {previous}.") from error
        for directory, _, _ in os.walk(previous, topdown=False):
            folder = Path(directory)
            if not any(folder.iterdir()):
                folder.rmdir()
        print(f"Restored the formal and test session files: {formal} and {test}", file=sys.stderr)
        raise


def promote(config, desktop=None):
    require_formal(config)
    if not (config.test / "config.toml").is_file():
        raise ValueError(f"Missing Codex configuration in {config.test}. Run prepare first.")
    desktop = desktop or Desktop(config)
    with directory_lock(config.formal), directory_lock(config.test_lock):
        validate_copy(config.test)
        config.backups.mkdir(parents=True, exist_ok=True)
        parents = (config.formal.parent, config.formal, config.test.parent, config.test, config.backups)
        if len({path.stat().st_dev for path in parents}) != 1:
            raise ValueError("Promotion requires formal, test and backup directories on the same filesystem.")
        desktop.stop_for_promotion()
        with suspend_windows_sandbox():
            validate_copy(config.test)
            report_formal_changes(config)
            for home in (config.formal, config.test, config.backups):
                if home.resolve() != home or is_redirected(home.lstat()):
                    raise ValueError(f"Use a physical directory for promotion: {home}")
            formal_runtime_links = capture_runtime_links(config.formal)
            test_runtime_links = capture_runtime_links(config.test)
            try:
                references = referenced_copy_paths(config.test.parent, config.formal, config.test)
            except (OSError, ValueError, sqlite3.Error) as error:
                print(f"WARNING: Could not check workspace references. Keeping the test-copy directory: {error}",
                      file=sys.stderr)
                references = None
            backup_dir = config.backups / f"codex-{config.timestamp()}"
            backup_dir.mkdir()
            previous = backup_dir / "codex"
            session_files_only = references is None or any(
                path.is_relative_to(config.test) or path == config.test.parent for path in references)
            if session_files_only and references is not None:
                print("Moving session files individually to preserve a workspace in the test copy.")
            try:
                formal_moved = False
                try:
                    if not session_files_only:
                        rename_for_promotion(config.formal, previous)
                        formal_moved = True
                        rename_for_promotion(config.test, config.formal)
                        install_runtime_links(previous, formal_runtime_links)
                        install_runtime_links(config.formal, test_runtime_links)
                except OSError as error:
                    if not windows_move_blocked(error):
                        raise
                    if formal_moved:
                        if config.formal.exists():
                            raise
                        rename_for_promotion(previous, config.formal)
                    if not config.formal.is_dir() or previous.exists():
                        raise
                    print(f"Windows blocked a directory move. Moving session files individually. Details: {error}",
                          flush=True)
                    session_files_only = True
                if session_files_only:
                    promote_session_files(config, previous)
                else:
                    rewrite_rollouts(config.test, config.formal)
            except BaseException as failure:
                if not session_files_only and previous.exists():
                    try:
                        if config.formal.exists():
                            rename_for_promotion(config.formal, backup_dir / "promoted-failed-codex")
                        rename_for_promotion(previous, config.formal)
                        install_runtime_links(config.formal, formal_runtime_links)
                    except (OSError, ValueError) as error:
                        raise ValueError(f"Automatic rollback failed. Check the directories under {backup_dir}.") from error
                    print(f"Restored the formal directory: {config.formal}", file=sys.stderr)
                    failed_copy = backup_dir / "promoted-failed-codex"
                    if failed_copy.exists():
                        print(f"Test home from the failed move is available at {failed_copy}", file=sys.stderr)
                try:
                    backup_dir.rmdir()
                except OSError:
                    pass
                if windows_move_blocked(failure):
                    raise ValueError(f"Windows blocked a promotion file operation after retries. Close applications "
                                     f"using the reported path and retry. Details: {failure}") from failure
                raise
            print(f"Promoted {config.test} to {config.formal}")
            print(f"Previous formal data: {previous}")
            if session_files_only:
                print("The backup contains the replaced session files and desktop metadata.")
            for home in (config.formal, config.test):
                try:
                    (home / TEST_MARKER).unlink(missing_ok=True)
                except OSError as error:
                    print(f"WARNING: Could not remove the completed test marker from {home}: {error}", file=sys.stderr)
        prune_promotion_backups(backup_dir)
        if references is not None:
            cleanup_after_promotion(config, references)


def elevated_promotion(config_file):
    import psutil

    # The original terminal starts the GUI with its original user token after the worker exits.
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    with TemporaryDirectory(prefix="codex-promotion-") as directory:
        log = Path(directory) / "output.log"
        arguments = subprocess.list2cmdline([
            str(Path(__file__).resolve()), "promote", "--config", str(config_file),
            "--windows-worker", psutil.Process().username(), str(log),
        ])

        def quoted(value):
            return "'" + str(value).replace("'", "''") + "'"

        command = (
            "$ErrorActionPreference = 'Stop'\n"
            f"$worker = Start-Process -FilePath {quoted(sys.executable)} "
            f"-ArgumentList {quoted(arguments)} -Verb RunAs -WindowStyle Hidden -Wait -PassThru\n"
            "exit $worker.ExitCode"
        )
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        print("Approve the Windows prompt to stop the Codex sandbox service and promote the copy.", flush=True)
        result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded])
        worker_started = log.is_file()
        if worker_started:
            print(log.read_text(encoding="utf-8"), end="", flush=True)
        if result.returncode:
            if worker_started:
                raise SystemExit(result.returncode)
            raise ValueError("Could not start the administrator process. Check the Windows prompt and use the "
                             "current Windows account.")


def promote_and_launch(args, config, config_file):
    desktop = Desktop(config)
    if sys.platform == "win32":
        desktop.require_external_windows_promotion()
    command = None if args.no_launch else desktop.command()
    service = windows_sandbox() if sys.platform == "win32" else None
    if service is not None and service.status() != "stopped" and not windows_admin():
        elevated_promotion(config_file)
    else:
        promote(config, desktop)
    if command is not None:
        try:
            desktop.launch(command, config.formal, config.backups / "promoted-desktop.log")
        except OSError as error:
            raise ValueError(f"Promotion succeeded. Open Codex manually for {config.formal}. "
                             f"Automatic launch failed: {error}") from error


def purge_backup(config, requested):
    backup_dir = requested.absolute()
    if (backup_dir.parent.resolve() != config.backups or backup_dir.resolve() != backup_dir
            or not re.fullmatch(r"codex-(?:repair-)?[0-9]{8}-[0-9]{6}", backup_dir.name)
            or not (backup_dir / "codex").is_dir()):
        raise ValueError(f"Choose a backup directly inside {config.backups}.")
    with directory_lock(config.formal):
        if input(f"Type DELETE to permanently remove {backup_dir}: ") != "DELETE":
            raise ValueError("Backup kept.")
        shutil.rmtree(backup_dir)


def run(args, config, config_file):
    match args.action:
        case "backup":
            require_formal(config)
            backup(config)
        case "prepare":
            prepare(config, args.version)
        case "repair":
            if getattr(args, "write_map_draft", None) and not args.plan:
                raise ValueError("--write-map-draft requires --plan")
            repair(args, config, extract_snapshot)
        case "status":
            Desktop(config).status()
        case "test":
            with directory_lock(config.formal), directory_lock(config.test_lock):
                desktop = Desktop(config)
                command = None if args.check_only else desktop.command()
                if command and sys.platform == "win32":
                    print("Closing Codex and ChatGPT before testing the copy...", flush=True)
                    desktop.stop_for_restart()
                validate_copy(config.test)
                desktop.require_test_idle()
                reopening = bool(getattr(args, "reopen", False))
                if reopening:
                    marker = validate_test_marker(config)
                    print(f"Verified {len(marker['threads'])} restored threads after the desktop test.")
                else:
                    marker_path = config.test / TEST_MARKER
                    marker = None
                    if marker_path.is_file():
                        marker = validate_test_marker(config)
                        if marker.get("launched"):
                            raise ValueError("This copy has already been opened by the desktop. Use test --reopen, "
                                             "or run repair to restore the pinned state before a new strict test.")
                        print("Using the current test --check-only validation marker.")
                    if marker is None:
                        print("Comparing chat history with the pinned snapshot. This can take a few minutes...", flush=True)
                        verify(config, args, extract_snapshot)
                        marker = write_test_marker(config)
                if command:
                    desktop.stop_for_restart()
                    validate_copy(config.test)
                    marker = validate_test_marker(config)
                    mark_test_launched(config, marker, reopened=reopening)
                    desktop.launch_test(command)
        case "promote":
            promote_and_launch(args, config, config_file)
        case "purge-backup":
            purge_backup(config, args.directory)
        case action:
            with directory_lock(config.formal), directory_lock(config.test_lock):
                desktop = Desktop(config)
                if action == "restore":
                    desktop.stop()
                    print(f"Test copy kept at {config.test}")
                    print(f"Reopen Codex for {config.formal}. Set CODEX_HOME explicitly when using a custom formal home.")
                elif action == "repair-formal":
                    require_formal(config)
                    desktop.stop()
                    rewrite_rollouts(config.test, config.formal)


def main(argv=None):
    config_option = argparse.ArgumentParser(add_help=False)
    config_option.add_argument("--config", type=Path, default=argparse.SUPPRESS,
                               help=f"TOML configuration path (default: {DEFAULT_CONFIG})")
    parser = argparse.ArgumentParser(description=__doc__, parents=[config_option])
    commands = parser.add_subparsers(dest="action")
    commands.add_parser("backup", parents=[config_option], help="Create and push a formal session snapshot.")
    prepare_parser = commands.add_parser("prepare", parents=[config_option], help="Fetch a snapshot into the test copy.")
    prepare_parser.add_argument("version", nargs="?")
    prepare_parser.add_argument("--replace", action="store_true", help=argparse.SUPPRESS)
    repair_parser = commands.add_parser("repair", parents=[config_option], help="Repair and compare a prepared snapshot.")
    repair_parser.add_argument("--test-home", type=Path, help="Confirm the configured test directory.")
    repair_parser.add_argument("--plan", action="store_true", help="List source workspaces and their local availability.")
    repair_parser.add_argument("--map", type=Path, help="JSON object mapping exact source paths to local paths or null.")
    repair_parser.add_argument("--write-map-draft", type=Path,
                               help="With --plan, write a reusable UTF-8 workspace mapping draft.")
    repair_parser.add_argument("--keep-staging", action="store_true",
                               help="Retain failed session-only repair staging for diagnosis.")
    repair_parser.add_argument("--app-server", type=Path, help="Codex CLI executable used by the target desktop.")
    test = commands.add_parser("test", parents=[config_option], help="Verify pinned history and open the test desktop.")
    test.add_argument("--app-server", type=Path, help="Override the app-server executable recorded during repair.")
    test_mode = test.add_mutually_exclusive_group()
    test_mode.add_argument("--check-only", action="store_true", help="Compare client history without launching the GUI.")
    test_mode.add_argument("--reopen", action="store_true",
                           help="Reopen a tested copy after checking its restored-thread fingerprints.")
    commands.add_parser("status", parents=[config_option], help="List desktop processes and known data directories.")
    commands.add_parser("restore", parents=[config_option], help="Exit testing and keep the copy.")
    promotion_parser = commands.add_parser("promote", parents=[config_option],
                                           help="Make the test copy active and restart Codex.")
    promotion_parser.add_argument("--no-launch", action="store_true", help="Leave Codex closed after promotion.")
    promotion_parser.add_argument("--windows-worker", nargs=2, help=argparse.SUPPRESS)
    commands.add_parser("repair-formal", parents=[config_option], help="Fix stale rollout paths in the formal directory.")
    purge = commands.add_parser("purge-backup", parents=[config_option], help="Remove one local promotion backup.")
    purge.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    if args.action is None:
        parser.print_help()
        return
    os.umask(0o077)
    try:
        config_file = getattr(args, "config", DEFAULT_CONFIG).resolve()
        if args.action == "promote" and args.windows_worker:
            if sys.platform != "win32" or not windows_admin():
                raise ValueError("The Windows promotion worker must run with administrator access.")
            import psutil

            owner, log = args.windows_worker
            if psutil.Process().username().casefold() != owner.casefold():
                raise ValueError("Use the same Windows account to approve the administrator prompt.")
            with Path(log).open("x", encoding="utf-8") as output, redirect_stdout(output), redirect_stderr(output):
                try:
                    promote(load_config(config_file))
                except (*COMMAND_ERRORS, KeyboardInterrupt) as error:
                    print(f"ERROR: {error}", file=sys.stderr)
                    sys.exit(1)
        else:
            run(args, load_config(config_file), config_file)
    except COMMAND_ERRORS as error:
        sys.exit(f"ERROR: {error}")
    except KeyboardInterrupt:
        sys.exit("Interrupted.")


if __name__ == "__main__":
    main()
