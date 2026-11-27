"""Assemble a test home from local settings and an extracted snapshot."""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from codex_session_common import DATABASES, LOG_DIRECTORIES, SESSION_FILES, backup_database, is_redirected


RUNTIME_LINKS = (
    Path("packages/app-server-daemon/current"),
    Path("packages/standalone/current"),
)


def capture_runtime_links(home):
    """Return runtime links whose targets are contained in this Codex home."""
    links = {}
    for relative in RUNTIME_LINKS:
        link = home / relative
        try:
            metadata = link.lstat()
        except FileNotFoundError:
            continue
        if not is_redirected(metadata):
            continue
        try:
            destination = link.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError(f"Broken runtime link in the Codex home: {link}") from error
        if not destination.is_dir() or not destination.is_relative_to(home):
            raise ValueError(f"Runtime link points outside the Codex home: {link} -> {destination}")
        links[relative] = destination.relative_to(home)
    return links


def install_runtime_links(home, links):
    """Create platform-native links that remain inside one physical Codex home."""
    for relative, target_relative in links.items():
        link = home / relative
        destination = home / target_relative
        if not destination.is_dir():
            raise ValueError(f"Missing runtime release for {link}: {destination}")
        try:
            metadata = link.lstat()
        except FileNotFoundError:
            metadata = None
        if metadata is not None:
            if not is_redirected(metadata):
                raise ValueError(f"Refusing to replace a physical runtime directory: {link}")
            if stat.S_ISLNK(metadata.st_mode):
                link.unlink()
            else:
                link.rmdir()
        link.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            command = os.environ.get("COMSPEC") or str(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe")
            result = subprocess.run(
                [command, "/d", "/c", "mklink", "/J", str(link), str(destination)],
                capture_output=True, text=True, errors="replace",
            )
            if result.returncode:
                raise ValueError(f"Could not create runtime junction {link}: {result.stdout.strip()}")
        else:
            link.symlink_to(os.path.relpath(destination, link.parent), target_is_directory=True)


def stage_copy(formal, snapshot, target):
    local_databases = {
        path.name for path in formal.glob("*.sqlite")
        if path.is_file() and path.name not in DATABASES
    }
    excluded = {
        *LOG_DIRECTORIES, *SESSION_FILES, *local_databases,
        "session_index.jsonl.lock", ".session-restore.json", ".session-test.json",
        "app-server-control", "app-server-daemon", "thread-writer-locks",
        ".sqlite-maintenance.lock", "tmp", ".tmp",
    }
    for database in (*DATABASES, *local_databases):
        excluded.update(database + suffix for suffix in ("-wal", "-shm", "-journal"))
    sqlite_sidecars = {Path(item).name for item in excluded if item.startswith("sqlite/")}
    windows = sys.platform == "win32"
    runtime_links = capture_runtime_links(formal)
    runtime_entries = {formal / relative for relative in runtime_links}

    def ignore(folder, names):
        path = Path(folder)
        skipped = excluded if path == formal else sqlite_sidecars if path == formal / "sqlite" else set()
        ignored = []
        for name in names:
            if name in skipped:
                ignored.append(name)
                continue
            entry = path / name
            try:
                metadata = entry.lstat()
            except FileNotFoundError:
                ignored.append(name)
                continue
            if is_redirected(metadata):
                if entry in runtime_entries:
                    ignored.append(name)
                    continue
                if not stat.S_ISLNK(metadata.st_mode):
                    raise ValueError(f"Redirected directory in the formal home: {entry}")
            if windows and path == formal and stat.S_ISREG(metadata.st_mode) and entry.match(".codex-provisioning-*.guard"):
                ignored.append(name)
                continue
            if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)):
                ignored.append(name)
        return ignored

    # SQLite's backup API includes committed WAL pages in the copied local databases.
    shutil.copytree(formal, target, symlinks=True, ignore=ignore)
    install_runtime_links(target, runtime_links)
    for name in sorted(local_databases):
        backup_database(formal / name, target / name)

    for directory in LOG_DIRECTORIES:
        source = snapshot / directory
        destination = target / directory
        if source.is_dir():
            source.rename(destination)
        else:
            destination.mkdir()
    for relative in SESSION_FILES:
        source = snapshot / relative
        if not source.is_file():
            raise ValueError(f"Missing snapshot file: {relative}")
        destination = target / relative
        if destination.parent.resolve() != destination.parent:
            raise ValueError(f"Redirected snapshot destination: {destination.parent}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
