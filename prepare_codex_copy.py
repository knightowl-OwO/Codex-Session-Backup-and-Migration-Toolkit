"""Assemble a test home from local settings and an extracted snapshot."""

import shutil
import stat
from pathlib import Path

from codex_session_common import DATABASES, LOG_DIRECTORIES, SESSION_FILES, backup_database, is_redirected


def stage_copy(formal, snapshot, target):
    local_databases = {
        path.name for path in formal.glob("*.sqlite")
        if path.is_file() and path.name not in DATABASES
    }
    excluded = {
        *LOG_DIRECTORIES, *SESSION_FILES, *local_databases,
        "session_index.jsonl.lock", ".session-restore.json",
        "app-server-control", "app-server-daemon", "thread-writer-locks",
        ".sqlite-maintenance.lock", "tmp", ".tmp",
    }
    for database in (*DATABASES, *local_databases):
        excluded.update(database + suffix for suffix in ("-wal", "-shm", "-journal"))
    sqlite_sidecars = {Path(item).name for item in excluded if item.startswith("sqlite/")}

    def ignore(folder, names):
        path = Path(folder)
        skipped = excluded if path == formal else sqlite_sidecars if path == formal / "sqlite" else set()
        ignored = []
        for name in names:
            if name in skipped:
                ignored.append(name)
                continue
            try:
                metadata = (path / name).lstat()
            except FileNotFoundError:
                ignored.append(name)
                continue
            if is_redirected(metadata) and not stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"Redirected directory in the formal home: {path / name}")
            if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)):
                ignored.append(name)
        return ignored

    # SQLite's backup API includes committed WAL pages in the copied local databases.
    shutil.copytree(formal, target, symlinks=True, ignore=ignore)
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
