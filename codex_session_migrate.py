#!/usr/bin/env python3
"""Back up, prepare, validate and promote a Codex session snapshot."""

import argparse
import base64
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory, mkdtemp

from backup_codex_sessions import backup
from codex_desktop import Desktop, suspend_windows_sandbox, windows_admin, windows_sandbox
from codex_session_common import (
    DATABASES, DEFAULT_CONFIG, LOG_DIRECTORIES, PROJECT, SESSION_FILES, SNAPSHOT_NAME,
    Repository, check_database, connect, directory_lock, is_redirected, load_config, rollout_relative,
)
from prepare_codex_copy import stage_copy
from restore_snapshot import pin, repair, verify


COMMAND_ERRORS = (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError, EOFError)


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
    with directory_lock(config.formal), directory_lock(config.test), directory_lock(repo.path):
        desktop = Desktop(config)
        desktop.require_test_idle()
        repo.fetch()
        revision, name = select_snapshot(repo, version)
        print(f"Preparing {name} from {revision}", flush=True)
        staging = Path(mkdtemp(prefix=".preparing-", dir=config.test.parent))
        candidate, previous = staging / "home", staging / "previous-home"
        try:
            snapshot = extract_snapshot(repo, revision, name, staging)
            stage_copy(config.formal, snapshot, candidate)
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
    print("After repair and test --check-only pass, run test from an external terminal to open the copy.")


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


def promote(config, desktop=None):
    require_formal(config)
    if not (config.test / "config.toml").is_file():
        raise ValueError(f"Missing Codex configuration in {config.test}. Run prepare first.")
    desktop = desktop or Desktop(config)
    with directory_lock(config.formal), directory_lock(config.test):
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
            backup_dir = config.backups / f"codex-{config.timestamp()}"
            backup_dir.mkdir()
            previous = backup_dir / "codex"
            try:
                config.formal.rename(previous)
                config.test.rename(config.formal)
                rewrite_rollouts(config.test, config.formal)
            except BaseException as failure:
                if previous.exists():
                    try:
                        if config.formal.exists():
                            config.formal.rename(backup_dir / "promoted-failed-codex")
                        previous.rename(config.formal)
                    except OSError as error:
                        raise ValueError(f"Automatic rollback failed. Check the directories under {backup_dir}.") from error
                    print(f"Restored the formal directory. Check {backup_dir} for the failed copy.", file=sys.stderr)
                else:
                    try:
                        backup_dir.rmdir()
                    except OSError:
                        pass
                if getattr(failure, "winerror", None) == 32:
                    raise ValueError(f"A Windows process still holds a promotion directory open. Close its file "
                                     f"handles and retry. Details: {failure}") from failure
                raise
            print(f"Promoted {config.test} to {config.formal}")
            print(f"Previous formal data: {previous}")
        prune_promotion_backups(backup_dir)
        test_log = config.test.parent / f"{config.test.name}-desktop.log"
        try:
            if test_log.is_file() and test_log.resolve() == test_log and not is_redirected(test_log.lstat()):
                test_log.unlink()
        except OSError as error:
            print(f"WARNING: Could not remove the test desktop log {test_log}: {error}", file=sys.stderr)


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
        if log.is_file():
            print(log.read_text(encoding="utf-8"), end="", flush=True)
        if result.returncode:
            raise ValueError("Elevated promotion failed. Approve the prompt using the current Windows account and "
                             "check any error above.")


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
            repair(args, config, extract_snapshot)
        case "status":
            Desktop(config).status()
        case "test":
            with directory_lock(config.formal), directory_lock(config.test):
                validate_copy(config.test)
                Desktop(config).require_test_idle()
                verify(config, args, extract_snapshot)
                if not args.check_only:
                    desktop = Desktop(config)
                    command = desktop.command()
                    desktop.stop()
                    validate_copy(config.test)
                    desktop.launch_test(command)
        case "promote":
            promote_and_launch(args, config, config_file)
        case "purge-backup":
            purge_backup(config, args.directory)
        case action:
            with directory_lock(config.formal), directory_lock(config.test):
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
    repair_parser.add_argument("--app-server", type=Path, help="Codex CLI executable used by the target desktop.")
    test = commands.add_parser("test", parents=[config_option], help="Verify pinned history and open the test desktop.")
    test.add_argument("--app-server", type=Path, help="Override the app-server executable recorded during repair.")
    test.add_argument("--check-only", action="store_true", help="Compare client history without launching the GUI.")
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
