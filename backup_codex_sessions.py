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


def prepare_checkout(repo):
    repo.fetch()
    git = repo.git
    branch = repo.config.branch
    if repo.output("remote", "get-url", "--push", "origin") != repo.config.remote:
        raise RuntimeError("The backup checkout has a different push URL.")
    if repo.output("status", "--porcelain"):
        raise RuntimeError(f"The backup checkout has local changes: {repo.path}")

    remote_exists = repo.has_ref(repo.ref)
    current = git("symbolic-ref", "--quiet", "HEAD", capture=True, check=False).stdout.strip()
    new_branch = False
    if current != f"refs/heads/{branch}":
        if repo.has_ref(f"refs/heads/{branch}"):
            git("switch", branch)
        elif remote_exists:
            git("switch", "-c", branch, "--track", repo.ref)
        elif git("rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode:
            git("symbolic-ref", "HEAD", f"refs/heads/{branch}")
        else:
            git("switch", "-c", branch)
            new_branch = True
    if remote_exists:
        git("merge", "--ff-only", repo.ref)
    head = git("rev-parse", "--verify", "--quiet", "HEAD", capture=True, check=False)
    # Re-running after a failed push should publish the existing snapshot first.
    pending = not new_branch and head.returncode == 0 and (
        not remote_exists
        or head.stdout.strip() != repo.output("rev-parse", repo.ref)
    )
    return branch, pending


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


def validate_rollouts(source, snapshot):
    database = snapshot / DATABASES[0]
    with connect(database) as db:
        rows = db.execute("SELECT id, rollout_path FROM threads WHERE rollout_path IS NOT NULL")
        for thread_id, rollout in rows:
            relative = rollout_relative(rollout, source)
            if relative is None or not (snapshot / relative).is_file():
                raise RuntimeError(f"Thread {thread_id} has no log in the snapshot.")


def copy_snapshot(source, snapshot):
    before = source_files(source)
    for database in DATABASES:
        backup_database(source / database, snapshot / database)
    for directory in LOG_DIRECTORIES:
        (snapshot / directory).mkdir(exist_ok=True)
    for relative in before:
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    if source_files(source) != before:
        return False
    validate_rollouts(source, snapshot)
    for path in snapshot.rglob("*"):
        if path.is_file() and path.stat().st_size > GITHUB_FILE_LIMIT:
            raise RuntimeError(f"GitHub's 100 MiB file limit would be exceeded by {path.relative_to(snapshot)}")
    return True


def commit_snapshot(repo, snapshot):
    existing = sorted(
        entry.name for entry in repo.path.iterdir()
        if SNAPSHOT_NAME.fullmatch(entry.name) and entry.is_dir() and not entry.is_symlink()
    )
    expired = sorted([*existing, snapshot.name])[:-repo.config.keep]
    if snapshot.name in expired:
        raise ValueError("The snapshot timestamp precedes the retained backups. Check the clock and UTC offset.")
    destination = repo.path / snapshot.name
    if destination.exists():
        raise RuntimeError(f"Snapshot already exists: {destination}")

    # Keep the old directories available until the commit succeeds.
    moved = []
    paths = [snapshot.name, *expired]
    snapshot.rename(destination)
    try:
        for name in expired:
            (repo.path / name).rename(snapshot.parent / name)
            moved.append(name)
        repo.git("add", "-A", "-f", "--", *paths)
        repo.git("commit", "-m", f"Back up Codex sessions {snapshot.name}")
    except (OSError, CalledProcessError, KeyboardInterrupt):
        for name in moved:
            (snapshot.parent / name).rename(repo.path / name)
        destination.rename(snapshot)
        if repo.git("rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode == 0:
            repo.git("reset", "--quiet", "--", *paths)
        else:
            repo.git("rm", "--quiet", "-r", "-f", "--cached", "--ignore-unmatch", "--", *paths)
        raise


def backup(config):
    repo = Repository(config)
    with directory_lock(config.formal), directory_lock(repo.path):
        branch, pending = prepare_checkout(repo)
        if pending:
            print("Retrying the push of an existing local backup commit.", flush=True)
        else:
            name = f"codex-git-{config.timestamp()}"
            print(f"Taking a snapshot of {config.formal}", flush=True)
            for attempt in range(3):
                with TemporaryDirectory(prefix=".codex-backup-", dir=repo.path.parent) as staging:
                    snapshot = Path(staging) / name
                    try:
                        stable = copy_snapshot(config.formal, snapshot)
                    except FileNotFoundError:
                        stable = False
                    if stable:
                        commit_snapshot(repo, snapshot)
                        break
                if attempt < 2:
                    print("Session files changed during the copy. Retrying.", flush=True)
            else:
                raise RuntimeError("Session files were missing or changing. Check the Codex home and retry.")
        repo.git("push", "-u", "origin", f"HEAD:refs/heads/{branch}")
