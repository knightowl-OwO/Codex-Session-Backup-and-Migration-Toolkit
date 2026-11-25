#!/usr/bin/env python3
"""Back up Codex session files and push them to the configured repository."""

import os
import shutil
from pathlib import Path
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory

from codex_session_common import (
    DATABASES, LOG_DIRECTORIES, SNAPSHOT_NAME, Repository, backup_database,
    connect, directory_lock, is_redirected, rollout_relative,
)

GITHUB_FILE_LIMIT = 100 * 1024**2


def source_files(source):
    """Record the session files and index so changes during the copy can be detected."""
    def walk_error(error):
        raise error

    files = {}
    for directory in LOG_DIRECTORIES:
        root = source / directory
        if not root.exists() and not root.is_symlink():
            continue
        if is_redirected(root.lstat()) or not root.is_dir():
            raise RuntimeError(f"Expected a physical session directory: {root}")
        for folder, dirs, names in os.walk(root, onerror=walk_error):
            for name in (*dirs, *names):
                path = Path(folder) / name
                metadata = path.lstat()
                if is_redirected(metadata):
                    raise RuntimeError(f"Unexpected redirected path in session files: {path}")
                if path.is_file():
                    files[path.relative_to(source)] = (
                        metadata.st_ino, metadata.st_size, metadata.st_mtime_ns,
                    )
    index = source / "session_index.jsonl"
    if index.is_symlink():
        raise RuntimeError(f"Unexpected symlink: {index}")
    stat = index.stat()
    files[index.relative_to(source)] = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    return files


def validate_and_relink_rollouts(source, snapshot):
    database = snapshot / DATABASES[0]
    with connect(database, "rw") as db, db:
        relinked = []
        rows = db.execute("SELECT id, rollout_path FROM threads WHERE rollout_path IS NOT NULL")
        for thread_id, rollout in rows:
            relative = rollout_relative(rollout, source)
            if relative is not None:
                if (snapshot / relative).is_file():
                    continue
                if relative.suffix == ".jsonl":
                    compressed = relative.with_suffix(".jsonl.zst")
                    if (snapshot / compressed).is_file():
                        relinked.append((str(source / compressed), thread_id))
                        continue
            raise RuntimeError(f"Thread {thread_id} has no matching rollout in the snapshot: {rollout}")
        db.executemany("UPDATE threads SET rollout_path = ? WHERE id = ?", relinked)
    if relinked:
        print(f"Updated {len(relinked)} rollout references to compressed logs in the snapshot.")


def copy_snapshot(source, snapshot):
    before = source_files(source)
    for database in DATABASES:
        backup_database(source / database, snapshot / database)
    for relative in before:
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    if source_files(source) != before:
        return False
    validate_and_relink_rollouts(source, snapshot)
    for path in snapshot.rglob("*"):
        if path.is_file() and path.stat().st_size > GITHUB_FILE_LIMIT:
            raise RuntimeError(f"GitHub's 100 MiB file limit would be exceeded by {path.relative_to(snapshot)}")
    return True


def commit_snapshot(repo, snapshot):
    parent = repo.output("rev-parse", repo.ref) if repo.has_ref(repo.ref) else None
    existing = []
    if parent:
        existing = [
            name for name in repo.output("ls-tree", "-d", "--name-only", parent).splitlines()
            if SNAPSHOT_NAME.fullmatch(name)
        ]
    if snapshot.name in existing:
        raise RuntimeError(f"Snapshot already exists: {snapshot.name}")
    expired = sorted([*existing, snapshot.name])[:-repo.config.keep]
    if snapshot.name in expired:
        raise ValueError("The snapshot timestamp precedes the retained backups. Check the clock and UTC offset.")
    # Prepare the new commit without changing the visible local snapshot.
    work_tree = snapshot.parent
    env = {**os.environ, "GIT_INDEX_FILE": str(work_tree / "index")}
    options = {"env": env, "work_tree": work_tree}
    repo.git("read-tree", parent or "--empty", **options)
    repo.git("add", "-f", "--", snapshot.name, **options)
    if expired:
        repo.git("rm", "--quiet", "-r", "--cached", "--", *expired, **options)
    tree = repo.git("write-tree", capture=True, **options).stdout.strip()
    parents = ("-p", parent) if parent else ()
    return repo.output("commit-tree", tree, *parents, "-m", f"Back up Codex sessions {snapshot.name}")


def backup(config):
    repo = Repository(config)
    with directory_lock(config.formal), directory_lock(repo.path):
        repo.fetch()
        repo.require_clean_checkout()
        if repo.output("remote", "get-url", "--push", "origin") != config.remote:
            raise ValueError("The backup checkout has a different push URL.")
        name = f"codex-git-{config.timestamp()}"
        print(f"Taking a snapshot of {config.formal}", flush=True)
        for attempt in range(3):
            with TemporaryDirectory(prefix=".snapshot-", dir=repo.path.parent) as staging:
                snapshot = Path(staging).resolve() / name
                try:
                    stable = copy_snapshot(config.formal, snapshot)
                except FileNotFoundError:
                    stable = False
                if stable:
                    commit = commit_snapshot(repo, snapshot)
                    break
            if attempt < 2:
                print("Session files changed during the copy. Retrying.", flush=True)
        else:
            raise RuntimeError("Session files were missing or changing. Check the Codex home and retry.")
        repo.git("push", "origin", f"{commit}:refs/heads/{config.branch}")
        try:
            repo.require_clean_checkout()
            repo.show_snapshot(commit, name)
        except (CalledProcessError, ValueError) as error:
            raise RuntimeError(f"Uploaded {name}, but could not refresh its local copy at {repo.path}.") from error
        print(f"Latest local snapshot: {repo.path / name}")
