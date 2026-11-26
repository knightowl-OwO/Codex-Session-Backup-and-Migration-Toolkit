"""Desktop process operations for the host running the migration."""

import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


WINDOWS_SANDBOX = "CodexSandboxService.OpenAI.Codex"
WINDOWS_PROMOTION_NAMES = frozenset({
    "chatgpt.exe", "codex.exe", "codex-code-mode.exe",
    "codex-code-mode-host.exe", "codex-computer-use-swift.exe",
})


def windows_admin():
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def windows_sandbox():
    import psutil

    try:
        service = psutil.win_service_get(WINDOWS_SANDBOX)
        service.status()
        return service
    except psutil.NoSuchProcess:
        return None


@contextmanager
def suspend_windows_sandbox():
    service = windows_sandbox() if sys.platform == "win32" else None
    if service is None or service.status() == "stopped":
        yield
        return
    if not windows_admin():
        raise ValueError("Stopping the Windows Codex sandbox service requires administrator access.")

    sc = Path(os.environ["SystemRoot"]) / "System32" / "sc.exe"

    def wait(target):
        deadline = time.monotonic() + 20
        while service.status() != target:
            if time.monotonic() >= deadline:
                raise ValueError(f"Timed out waiting for {WINDOWS_SANDBOX} to reach {target}.")
            time.sleep(0.2)

    def change(action, target):
        status = service.status()
        if status == target:
            return
        if status != f"{action}_pending":
            result = subprocess.run(
                [str(sc), action, WINDOWS_SANDBOX], capture_output=True, text=True, errors="replace", timeout=20,
            )
            if result.returncode and service.status() not in (target, f"{action}_pending"):
                raise ValueError(f"Could not {action} {WINDOWS_SANDBOX}: {result.stdout.strip()}")
        wait(target)

    if service.status() == "start_pending":
        wait("running")
    try:
        change("stop", "stopped")
        print(f"Stopped Windows sandbox service {WINDOWS_SANDBOX}.", flush=True)
        yield
    finally:
        if service.status() == "stop_pending":
            wait("stopped")
        change("start", "running")


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
        names = ("codex", "codex-code-mode", "codex-code-mode-host", "chatgpt")
        suffix = ".exe" if sys.platform == "win32" else ""
        defaults = tuple(name + suffix for name in names)
        self.names = {name.casefold() for name in (config.process_names or defaults)}

    def processes(self):
        if sys.platform.startswith("linux"):
            return list(_linux_processes(self.names))
        return list(_desktop_processes(self.names))

    def require_test_idle(self):
        homes = (self.config.test, self.config.test_runtime)
        for process in self.processes():
            process_home = Path(process.home).resolve() if process.home else None
            database_homes = [Path(path).resolve() for path in process.databases]
            if any(process_home == home or any(path.is_relative_to(home) for path in database_homes)
                   for home in homes):
                raise ValueError("Exit the desktop test instance before replacing its test or runtime copy.")
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

    def windows_promotion_targets(self):
        import psutil

        local_app_data = os.environ.get("LOCALAPPDATA")
        runtime = Path(local_app_data) / "OpenAI" / "Codex" if local_app_data else None
        owner = psutil.Process().username().casefold()
        roots = []
        for process in psutil.process_iter(["name", "username"]):
            try:
                name = (process.info["name"] or "").casefold()
                if (process.info["username"] or "").casefold() != owner:
                    continue
                if name in self.names | WINDOWS_PROMOTION_NAMES:
                    roots.append(process)
                elif name == "node_repl.exe" and runtime and Path(process.exe()).is_relative_to(runtime):
                    roots.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        targets = set(roots)
        for process in roots:
            try:
                children = process.children(recursive=True)
            except psutil.NoSuchProcess:
                continue
            for child in children:
                try:
                    if child.username().casefold() == owner:
                        targets.add(child)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        return targets

    def require_external_windows_promotion(self, targets=None):
        import psutil

        if targets is None:
            targets = self.windows_promotion_targets()
        caller = psutil.Process()
        if targets.intersection([caller, *caller.parents()]):
            raise ValueError("Run this command from an external PowerShell window. It closes Codex and its terminals.")

    def stop_for_promotion(self):
        self.stop_for_restart()

    def stop_for_restart(self):
        if sys.platform != "win32":
            self.stop()
            return

        import psutil

        for _ in range(3):
            targets = self.windows_promotion_targets()
            self.require_external_windows_promotion(targets)
            if not targets:
                return
            for process in targets:
                try:
                    process.kill()
                except psutil.NoSuchProcess:
                    continue
                except psutil.AccessDenied as error:
                    raise ValueError(f"Could not stop Codex process PID {process.pid}.") from error
            psutil.wait_procs(targets, timeout=4)
        if targets := self.windows_promotion_targets():
            pids = ", ".join(str(process.pid) for process in targets)
            raise ValueError(f"Codex processes are still running (PIDs: {pids}). Exit them before retrying.")

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
        log = self.config.test.parent / f"{self.config.test_runtime.name}-desktop.log"
        self.launch(command, self.config.test_runtime, log)

    def launch(self, command, home, log):
        env = {**os.environ, "CODEX_HOME": str(home)}
        options = (
            {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
            if sys.platform == "win32" else {"start_new_session": True}
        )
        with log.open("wb") as output:
            process = subprocess.Popen(
                command, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, **options,
            )
        print(f"Started desktop launcher PID {process.pid} with CODEX_HOME={home}")
        print(f"Launch log: {log}")

    def status(self):
        if self.config.test_runtime.exists():
            print(f"Test runtime path: {self.config.test_runtime}")
        processes = self.processes()
        if not processes:
            print("No matching Codex processes found.")
        for process in processes:
            home = (process.home or "<unset>") if process.environment_readable else "<unreadable>"
            databases = ", ".join(sorted(process.databases)) or "<not detected>"
            print(f"{process.name} PID={process.pid} CODEX_HOME={home}  state database={databases}")
