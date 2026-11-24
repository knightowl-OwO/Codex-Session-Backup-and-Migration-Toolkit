"""Desktop process operations for the host running the migration."""

import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Process:
    pid: int
    name: str
    home: str | None
    databases: tuple[str, ...]
    environment_readable: bool


def _linux_processes(names):
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            name = (entry / "comm").read_text().strip()
            if name.casefold() not in names:
                continue
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                continue
            try:
                environment = (entry / "environ").read_bytes().split(b"\0")
                home = next((os.fsdecode(item[11:]) for item in environment if item.startswith(b"CODEX_HOME=")), None)
                readable = True
            except PermissionError:
                home, readable = None, False
            databases = []
            try:
                for fd in (entry / "fd").iterdir():
                    try:
                        path = os.readlink(fd)
                        if Path(path).name == "state_5.sqlite":
                            databases.append(path)
                    except (FileNotFoundError, PermissionError):
                        continue
            except PermissionError:
                pass
            yield Process(int(entry.name), name, home, tuple(set(databases)), readable)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue


def _desktop_processes(names):
    try:
        import psutil
    except ImportError as error:
        raise ValueError("Desktop process inspection on Windows and macOS requires psutil. "
                         "Install it with the same Python using -m pip install psutil.") from error

    username = psutil.Process().username().casefold()
    for process in psutil.process_iter(["name", "username", "status"]):
        try:
            info = process.info
            if (info["name"] or "").casefold() not in names:
                continue
            if (not info["username"] or info["username"].casefold() != username
                    or info["status"] == psutil.STATUS_ZOMBIE):
                continue
            try:
                home, readable = process.environ().get("CODEX_HOME"), True
            except (psutil.AccessDenied, NotImplementedError):
                home, readable = None, False
            try:
                databases = tuple(f.path for f in process.open_files() if Path(f.path).name == "state_5.sqlite")
            except (psutil.AccessDenied, NotImplementedError):
                databases = ()
            yield Process(process.pid, info["name"], home, databases, readable)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue


class Desktop:
    def __init__(self, config):
        self.config = config
        base = ("Codex", "codex-code-mode", "codex-code-mode-host")
        if sys.platform.startswith("linux"):
            defaults = (*base, "ChatGPT")
        elif sys.platform == "win32":
            defaults = tuple(f"{name}.exe" for name in (*base, "ChatGPT"))
        else:
            defaults = (*base, "ChatGPT")
        self.names = {name.casefold() for name in (config.process_names or defaults)}

    def processes(self):
        if sys.platform.startswith("linux"):
            return list(_linux_processes(self.names))
        return list(_desktop_processes(self.names))

    def require_test_idle(self):
        if not self.config.test.exists():
            return
        for process in self.processes():
            if (process.home and Path(process.home).resolve() == self.config.test
                    or any(Path(path).resolve().parent == self.config.test for path in process.databases)):
                raise ValueError("Exit the desktop test instance before replacing the test copy.")
            if not process.environment_readable and not process.databases:
                raise ValueError(f"Could not verify the data directory of {process.name} PID {process.pid}. Exit it and retry.")

    def stop(self):
        processes = self.processes()
        if not processes:
            return
        if not sys.platform.startswith("linux"):
            names = ", ".join(f"{proc.name} ({proc.pid})" for proc in processes)
            raise ValueError(f"Fully quit Codex and retry from an external terminal. Still running: {names}")

        pids = {proc.pid for proc in processes}
        parent = os.getppid()
        while parent > 1:
            if parent in pids:
                raise ValueError("Run this command from an external terminal. It closes the Codex desktop.")
            try:
                parent = int(Path(f"/proc/{parent}/stat").read_text().rsplit(")", 1)[1].split()[1])
            except (FileNotFoundError, PermissionError):
                break

        for sig, seconds in ((signal.SIGTERM, 30), (signal.SIGKILL, 5)):
            running = {proc.pid for proc in self.processes()}
            for pid in pids & running:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if not self.processes():
                    return
                time.sleep(0.25)
        raise ValueError("Codex processes are still running. Exit them before retrying.")

    def command(self):
        if self.config.desktop_command:
            executable, *args = self.config.desktop_command
            executable = os.path.expanduser(os.path.expandvars(executable))
        elif sys.platform.startswith("linux"):
            executable, args = "chatgpt", []
        elif sys.platform == "darwin":
            for bundle in (Path("/Applications/Codex.app"), Path.home() / "Applications/Codex.app"):
                manifest = bundle / "Contents/Info.plist"
                if manifest.is_file():
                    with manifest.open("rb") as source:
                        executable = str(bundle / "Contents/MacOS" / plistlib.load(source)["CFBundleExecutable"])
                    args = []
                    break
            else:
                raise ValueError("Set desktop.command to your Codex installation's executable.")
        else:
            raise ValueError("Set desktop.command to the installed Codex desktop executable on Windows.")
        executable = shutil.which(executable)
        if executable is None:
            raise ValueError("Codex desktop executable not found. Check desktop.command in your configuration.")
        return [executable, *args]

    def launch_test(self, command):
        env = {**os.environ, "CODEX_HOME": str(self.config.test)}
        log = self.config.test.parent / f"{self.config.test.name}-desktop.log"
        options = (
            {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
            if sys.platform == "win32" else {"start_new_session": True}
        )
        with log.open("wb") as output:
            process = subprocess.Popen(
                command, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, **options,
            )
        print(f"Started desktop launcher PID {process.pid} with CODEX_HOME={self.config.test}")
        print(f"Launch log: {log}")

    def status(self):
        processes = self.processes()
        if not processes:
            print("No matching Codex processes found.")
        for process in processes:
            home = (process.home or "<unset>") if process.environment_readable else "<unreadable>"
            databases = ", ".join(sorted(process.databases)) or "<not detected>"
            print(f"{process.name} PID={process.pid} CODEX_HOME={home}  state database={databases}")
