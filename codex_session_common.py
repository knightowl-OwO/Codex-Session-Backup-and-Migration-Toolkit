"""Configuration, locking, Git and SQLite for session migration."""

import os
import re
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("Codex session migration requires Python 3.11 or newer.")

import tomllib


PROJECT = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT / "config.toml"
SNAPSHOT_NAME = re.compile(r"codex-git-[0-9]{8}-[0-9]{6}")
LOG_DIRECTORIES = ("sessions", "archived_sessions")
DATABASES = ("state_5.sqlite", "thread_history_1.sqlite", "sqlite/codex.db")
SESSION_FILES = ("session_index.jsonl", *DATABASES)


@dataclass(frozen=True)
class Config:
    formal: Path
    test: Path
    backups: Path
    checkout: Path
    remote: str
    branch: str
    keep: int
    utc_offset_minutes: int
    desktop_command: tuple[str, ...] = ()
    process_names: tuple[str, ...] = ()

    @property
    def dedicated_copy(self):
        return self.test == self.test.parent.parent / ".codex_copy" / ".codex"

    @property
    def test_runtime(self):
        return self.test.with_name(self.test.name + "-runtime")

    @property
    def test_lock(self):
        # Keep the lock outside a removable copy root, including between migrations.
        return self.test.parent if self.dedicated_copy else self.test

    @property
    def history_view(self):
        return self.formal.with_name(self.formal.name + "-history-view")

    def scratch_prefix(self, operation):
        return f".{operation}-" if self.dedicated_copy else f"{self.test.name}.{operation}-"

    def timestamp(self):
        zone = timezone(timedelta(minutes=self.utc_offset_minutes))
        return datetime.now(zone).strftime("%Y%m%d-%H%M%S")


def load_config(file):
    file = file.resolve()
    if not file.is_file():
        raise ValueError(f"Missing configuration: {file}. See {PROJECT / 'config.example.toml'}.")
    with file.open("rb") as source:
        data = tomllib.load(source)
    paths = data.get("paths", {})
    repo = data.get("repository", {})
    desktop = data.get("desktop", {})
    if not all(isinstance(section, dict) for section in (paths, repo, desktop)):
        raise ValueError("Use TOML tables for paths, repository and desktop.")

    def path(key, default):
        value = paths.get(key, str(default))
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"paths.{key} must be a nonempty path.")
        candidate = file.parent / Path(os.path.expandvars(value)).expanduser()
        try:
            if is_redirected(candidate.lstat()):
                raise ValueError(f"Use a physical directory for paths.{key}: {candidate}")
        except FileNotFoundError:
            pass
        return candidate.resolve()

    formal = path("formal_home", Path.home() / ".codex")
    test = path("test_home", formal.parent / ".codex_copy" / ".codex")
    backups = path("backup_root", formal.parent / ".codex-backups")
    checkout = path("checkout", file.parent / "session-backup-checkout")
    test_runtime = test.with_name(test.name + "-runtime")
    history_view = formal.with_name(formal.name + "-history-view")
    directories = (formal, test, test_runtime, history_view, backups, checkout)
    for index, left in enumerate(directories):
        if left == left.parent or file.is_relative_to(left) or PROJECT.is_relative_to(left):
            raise ValueError(f"Use a dedicated data directory: {left}")
        for right in directories[index + 1:]:
            if left.is_relative_to(right) or right.is_relative_to(left):
                raise ValueError(f"Data directories must be separate: {left} and {right}")

    remote = repo.get("url")
    branch = repo.get("branch", "main")
    keep = repo.get("keep", 5)
    offset = repo.get("timestamp_utc_offset_minutes", 0)
    if not isinstance(remote, str) or not remote.strip():
        raise ValueError("Set repository.url to the private backup repository.")
    if not isinstance(branch, str) or not branch or branch.startswith("-") or branch == "HEAD":
        raise ValueError("Set repository.branch to a Git branch name.")
    if type(keep) is not int or keep < 1:
        raise ValueError("repository.keep must be a positive integer.")
    if type(offset) is not int or not -1439 <= offset <= 1439:
        raise ValueError("repository.timestamp_utc_offset_minutes must be between -1439 and 1439.")
    command = desktop.get("command", [])
    names = desktop.get("process_names", [])
    for key, value in (("command", command), ("process_names", names)):
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError(f"desktop.{key} must be an array of nonempty strings.")
    return Config(formal, test, backups, checkout, remote, branch, keep, offset, tuple(command), tuple(names))


def is_redirected(metadata):
    return (stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_reparse_tag", 0) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1))


@contextmanager
def directory_lock(directory):
    directory.parent.mkdir(parents=True, exist_ok=True)
    lock = directory.parent / f"{directory.name}.codex-migration.lock"
    with lock.open("a+b", buffering=0) as handle:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError(f"Could not lock {directory}: {error}") from error
        yield


class Repository:
    def __init__(self, config):
        self.config = config
        self.path = config.checkout
        self.ref = f"refs/remotes/origin/{config.branch}"

    def git(self, *args, check=True, capture=False, env=None, work_tree=None):
        command = ["git", "-C", str(self.path), "-c", "core.autocrlf=false"]
        if work_tree is not None:
            command.extend(["-c", "core.sparseCheckout=false", f"--work-tree={work_tree}"])
        return subprocess.run(
            [*command, *args],
            check=check, stdout=subprocess.PIPE if capture else None, env=env,
            text=capture, encoding="utf-8" if capture else None,
        )

    def output(self, *args):
        return self.git(*args, capture=True).stdout.strip()

    def has_ref(self, ref):
        return self.git("show-ref", "--verify", "--quiet", ref, check=False).returncode == 0

    def fetch(self):
        branch = f"refs/heads/{self.config.branch}"
        subprocess.run(["git", "check-ref-format", branch], check=True)
        if not self.path.exists():
            subprocess.run(
                ["git", "-c", "core.autocrlf=false", "clone", "--sparse", "--", self.config.remote, str(self.path)],
                check=True,
            )
        if not (self.path / ".git").is_dir():
            raise ValueError(f"Expected a dedicated backup checkout: {self.path}")
        if self.output("remote", "get-url", "origin") != self.config.remote:
            raise ValueError("The backup checkout has a different origin.")
        self.git("fetch", "--no-tags", "--prune", "origin")

    def require_clean_checkout(self):
        if self.output("status", "--porcelain=v1", "--untracked-files=all") or self.output(
            "status", "--porcelain=v1", "--ignored=matching", "--",
            ":(top,glob)codex-git-*/**",
        ):
            raise ValueError(f"Review the local changes in the backup repository first: {self.path}")
        local_ref = f"refs/heads/{self.config.branch}"
        if self.has_ref(local_ref):
            if not self.has_ref(self.ref) or self.git(
                "merge-base", "--is-ancestor", local_ref, self.ref, check=False,
            ).returncode != 0:
                raise ValueError(f"The local backup branch has unpublished or divergent commits: {local_ref}")

    def show_snapshot(self, commit, name):
        self.git("sparse-checkout", "set", "--no-cone", "--", "/*", "!/codex-git-*/", f"/{name}/")
        self.git("switch", "-C", self.config.branch, commit)
        self.git("branch", "--set-upstream-to", self.ref, self.config.branch)


def connect(path, mode="ro"):
    if path.resolve() != path or not path.is_file():
        raise ValueError(f"Missing or redirected database: {path}")
    return closing(sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True))


def check_database(db, path):
    if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise ValueError(f"SQLite integrity check failed: {path}")


def backup_database(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with connect(source) as live, closing(sqlite3.connect(target)) as snapshot:
        live.backup(snapshot)
        snapshot.execute("PRAGMA journal_mode=DELETE").fetchone()
        check_database(snapshot, target)


def rollout_relative(value, home):
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        return None
    prefix = "\\\\?\\"
    if os.name == "nt" and path.drive.startswith(prefix) and not home.drive.startswith(prefix):
        # Codex can store an extended-length spelling of the same Windows path.
        home_name = str(home)
        if home_name.startswith("\\\\"):
            home_name = "UNC\\" + home_name[2:]
        home = Path(prefix + home_name)
    if not path.is_relative_to(home):
        return None
    relative = path.relative_to(home)
    if len(relative.parts) < 2 or relative.parts[0] not in LOG_DIRECTORIES:
        return None
    return relative
