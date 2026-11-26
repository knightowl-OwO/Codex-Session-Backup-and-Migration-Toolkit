"""Read synthetic or disposable Codex homes with the desktop app-server protocol."""

import hashlib
import json
import os
import queue
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from restore_rollouts import text_digest

SOURCES = ["cli", "vscode", "exec", "appServer", "subAgent", "subAgentReview",
           "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown"]


@contextmanager
def app_server(binary, home, scratch):
    # A probe never inherits the user's desktop profile, active daemon or API tokens.
    profile = scratch / "user"
    profile.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "PATHEXT", "LANG", "LC_ALL",
    }}
    env.update(HOME=str(profile), USERPROFILE=str(profile), CODEX_HOME=str(home),
               XDG_CONFIG_HOME=str(profile / "config"), XDG_DATA_HOME=str(profile / "data"),
               LOCALAPPDATA=str(profile / "local"), APPDATA=str(profile / "roaming"))
    with subprocess.Popen([str(binary), "app-server", "--listen", "stdio://"],
                          env=env, cwd=scratch, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, encoding="utf-8", bufsize=1) as process:
        messages = queue.Queue()

        def receive():
            try:
                for line in process.stdout:
                    messages.put(json.loads(line))
            except (OSError, ValueError):
                pass
            finally:
                messages.put(None)

        reader = threading.Thread(target=receive, daemon=True)
        reader.start()
        seq = 0

        def send(data):
            process.stdin.write(json.dumps(data) + "\n")
            process.stdin.flush()

        def call(method, params):
            nonlocal seq
            seq += 1
            send({"id": seq, "method": method, "params": params})
            deadline = time.monotonic() + 60
            while True:
                try:
                    msg = messages.get(timeout=max(0, deadline - time.monotonic()))
                except queue.Empty as exc:
                    raise ValueError(f"{method}: client timed out") from exc
                if msg is None:
                    raise ValueError(f"{method}: app-server closed its output")
                if "method" in msg and "id" in msg:
                    send({"id": msg["id"], "error": {"code": -32601, "message": "Offline history probe"}})
                elif msg.get("id") == seq:
                    if "error" in msg:
                        raise ValueError(f"{method}: {msg['error'].get('message')}")
                    return msg["result"]

        try:
            call("initialize", {"clientInfo": {"name": "session_migration", "version": "1"},
                                "capabilities": {"experimentalApi": True}})
            send({"method": "initialized"})
            yield call
        finally:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            reader.join(timeout=2)


def pages(call, method, params, size):
    cursor = None
    seen = set()
    while True:
        result = call(method, {**params, "cursor": cursor, "limit": size})
        yield from result["data"]
        cursor = result["nextCursor"]
        if cursor is None:
            return
        if not isinstance(cursor, str) or cursor in seen:
            raise ValueError(f"{method}: repeated or invalid cursor")
        seen.add(cursor)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                                     sort_keys=True).encode()).hexdigest()


def same_directories(actual, expected):
    if not isinstance(actual, list) or len(actual) != len(expected):
        return False
    for left, right in zip(actual, expected):
        if not isinstance(left, str) or not Path(left).is_absolute() or not Path(left).is_dir():
            return False
        if not Path(left).samefile(right):
            return False
    return True


def history(call, threads, size=32, *, resume=False, workspaces=None):
    """Keep per-turn digests only. Neither the receipt nor diagnostics contain messages."""
    catalog = {}
    for archived in (False, True):
        parents = sorted({t.attached_to for t in threads.values() if t.attached_to})
        for scope in [{}, *({"attachedToThreadId": pid} for pid in parents)]:
            for t in pages(call, "thread/list", {"archived": archived, "modelProviders": [],
                                                 "sourceKinds": SOURCES, "useStateDbOnly": True,
                                                 "currentAccountOnly": False, **scope}, size):
                tid = t["id"]
                if tid in catalog:
                    raise ValueError(f"{tid}: duplicate catalog entry")
                catalog[tid] = (t["name"] or t["preview"], archived)
    if set(catalog) != set(threads):
        raise ValueError(f"Client catalog ID mismatch: {sorted(set(catalog) ^ set(threads))}")
    result = {}
    for tid, (_, archived) in sorted(catalog.items()):
        try:
            if threads[tid].archived != archived:
                raise ValueError("archive state differs from the snapshot")
            t = call("thread/read", {"threadId": tid, "includeTurns": False})["thread"]
            title = t["name"] or t["preview"]
            if t["id"] != tid or title != catalog[tid][0]:
                raise ValueError("thread/read disagrees with the catalog")
            turns = {}
            messages = dict(threads[tid].visible_records("messages"))
            for turn in pages(call, "thread/turns/list", {"threadId": tid, "sortDirection": "asc",
                                                         "itemsView": "full"}, size):
                turn_id = turn["id"]
                if turn_id in turns or turn.get("itemsView") != "full":
                    raise ValueError(f"{turn_id}: duplicate turn or incomplete items")
                turns[turn_id] = digest(turn["items"])
                for item in turn["items"]:
                    key = (turn_id, item["id"])
                    if key in messages:
                        if item["type"] == "userMessage":
                            body = [part["text"] for part in item["content"] if part["type"] == "text"]
                        elif item["type"] == "agentMessage":
                            body = item["text"]
                        else:
                            raise ValueError(f"{turn_id}: message type changed for {item['id']}")
                        if messages.pop(key) != text_digest(body):
                            raise ValueError(f"{turn_id}: client message body differs for {item['id']}")
            if messages:
                raise ValueError(f"client omitted logged messages: {list(messages)[:3]}")
            logged = threads[tid].recorded_turns()
            if logged and logged != list(turns):
                raise ValueError(f"log chain has {len(logged)} turns, client returned {len(turns)} in a different history")
            if resume and not archived:
                opened = call("thread/resume", {"threadId": tid, "excludeTurns": True})
                if opened["thread"]["id"] != tid:
                    raise ValueError("resume opened a different thread")
                if workspaces is not None:
                    expected = workspaces[tid]
                    if (not same_directories([opened["cwd"]], [expected["cwd"]]) or expected["roots"] is not None
                            and not same_directories(opened["runtimeWorkspaceRoots"], expected["roots"])):
                        raise ValueError(f"resumed workspace differs: cwd={opened['cwd']!r}, "
                                         f"roots={opened['runtimeWorkspaceRoots']!r}")
                call("thread/unsubscribe", {"threadId": tid})
            result[tid] = (title, archived, list(turns.items()))
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"{tid}: {exc}") from exc
    return result


def compare_history(expected, actual):
    for tid in sorted(expected.keys() | actual.keys()):
        if tid not in actual or tid not in expected:
            raise ValueError(f"{tid}: client catalog changed")
        left, right = expected[tid], actual[tid]
        if left[:2] != right[:2]:
            raise ValueError(f"{tid}: client title or archive state changed")
        if left[2] != right[2]:
            for number, (a, b) in enumerate(zip(left[2], right[2])):
                if a != b:
                    raise ValueError(f"{tid}: history mismatch at turn {number + 1} ({a[0]} / {b[0]})")
            raise ValueError(f"{tid}: expected {len(left[2])} visible turns, read {len(right[2])}")
