# Codex Session Backup and Migration Toolkit

A command-line toolkit for backing up and migrating local Codex sessions. The source machine pushes a session snapshot to a private Git repository. The destination machine repairs and tests the snapshot in an isolated copy before making it the active data directory. The toolkit code can live in a separate public repository.

## How it works

- `backup` creates a `codex-git-YYYYMMDD-HHMMSS` snapshot from the source machine's active Codex data directory. It includes `sessions/`, `archived_sessions/`, `session_index.jsonl`, `state_5.sqlite`, `thread_history_1.sqlite`, and `sqlite/codex.db`. The toolkit uses SQLite's backup API to copy the databases, then commits the snapshot and pushes it with Git.
- `prepare` builds a test copy from the destination machine's active data directory and imports the selected snapshot. It records the snapshot name and full commit ID in the test home and pins the commit in the local backup clone. `repair` and `test` use that revision even if the branch advances.
- `repair` follows each source SQLite entry through its rollout segments, applies confirmed workspace mappings in staging and compares the result with the pinned snapshot using the destination desktop's app-server. `test` repeats the comparison before opening Codex with `CODEX_HOME` set to the verified copy. After you verify the restored chats, `promote` makes the copy active, updates its session log paths, and keeps one local rollback backup.

**Each migration restores one snapshot.** Sessions and newer messages found only on the destination machine remain in the local backup created during promotion. The verifier expects the threads and history of the selected snapshot. Set up project source code and workspace files separately on the destination machine.

## Requirements and configuration

Install Python 3.11 or later and Git on both machines. The destination machine needs the Codex desktop app and an existing Codex data directory. On Windows and macOS, install `psutil` with the Python interpreter you use to run the toolkit, for example `python -m pip install psutil`. The toolkit uses it to check which data directory Codex is using and whether it has exited before replacing data.

Create your own **private Git repository** on GitHub to store your session snapshots. Set up Git access to it on each machine using SSH or HTTPS credentials. In the toolkit's project root, create a `config.toml` on each machine with its own settings. For available settings and example values, see [config.example.toml](config.example.toml).

Paths can use `~`. Use absolute paths or paths relative to the directory that contains `config.toml`.

For snapshots with `.jsonl.zst` logs, use Python's Zstandard module, install `zstandard` with `python -m pip install zstandard`, or put the `zstd` executable on `PATH`. The restore commands need the Codex CLI distributed with the destination desktop app. They look for it beside the configured GUI launcher in the application's resources. You can supply its exact path with `repair --app-server`.

On Windows, use single-quoted TOML strings to keep backslashes literal. For example, set `formal_home` and `checkout` under `[paths]` as shown below.

~~~toml
[paths]
formal_home = 'C:\Users\username\.codex'
checkout = 'F:\individual-codex-session-backups'
~~~

| Setting | What to configure                                                                                                                                                                                                                                              |
| --- |----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `paths.formal_home` | Active Codex data directory, defaulting to `~/.codex`. The `backup` command reads from this directory.                                                                                                                                                         |
| `paths.test_home` | Isolated test copy, set to `~/.codex_copy/.codex` in the template. Each successful `prepare` replaces this copy.                                                                                                                                               |
| `paths.backup_root` | Location for the local rollback backup, set to `~/.codex-backups` in the template. `promote` requires `formal_home`, `test_home`, and `backup_root` to be on the same filesystem. `checkout` can be on a different filesystem. |
| `paths.checkout` | Local directory for the clone of your private backup repository. Keep it outside the toolkit directory to avoid nesting the private clone inside the public project. The toolkit creates the clone at this path if needed. After a successful backup, this directory contains one visible snapshot folder. |
| `repository.url` and `repository.branch` | Clone URL of the remote private backup repository and the branch to use. Use the same URL and branch on the machines you migrate between.                                                                                                                      |
| `repository.keep` | Maximum number of snapshot directories in the latest commit on the backup branch. The default is 5. Set any positive integer.                                                                                                                                  |
| `repository.timestamp_utc_offset_minutes` | UTC offset in minutes for snapshot timestamps. Use `480` for Beijing time (UTC+8) or `0` for UTC. Use the same offset on every device that shares the backup branch. |

When `backup` creates a new snapshot, it fetches the remote branch, then keeps the newest `keep` snapshot directories by their name timestamps. With `keep = 5`, adding a sixth snapshot removes the oldest directory in the new commit. If you lower `keep`, the next new snapshot removes as many older directories as needed. The remote branch reflects these deletions after a successful push. Older snapshots remain in Git history and can still be selected by name with `prepare`.

After a successful push, `backup` replaces the visible snapshot in `paths.checkout` with the one it just uploaded. Open that `codex-git-*` folder directly in your file manager. A failed push leaves the previous local snapshot available. Older snapshots and Git history still take up space in the clone's `.git` directory. `backup` and `prepare` reuse the clone and fetch updates as needed.

Keep session snapshots in the private repository because they contain chat messages, tool output, and local paths. Before publishing the toolkit, check the staged files. The ignore rules cover the local `config.toml` and the default clone directory. [Ignore rules](.gitignore)

### Desktop launch

`[desktop]` tells the toolkit how to start and identify the Codex desktop app. `desktop.command` lists the executable followed by any arguments. The toolkit sets `CODEX_HOME` for the new process and its children. It uses `paths.test_home` for `test` and `paths.formal_home` for `promote`.

On Linux, the default is `chatgpt` on `PATH`. On macOS, the toolkit looks for `Codex.app` in `/Applications` and `~/Applications`.

On Windows, set `command` to the path of the GUI launcher for your installation. For Microsoft Store or MSIX installations, this may be `ChatGPT.exe`. A `codex.exe` process path reported by PowerShell may belong to a backend process, so verify which executable opens the desktop app. Replace the example path below with the one on your machine. Windows app updates can change the versioned directory under `WindowsApps`, so check the path again after an update.

~~~toml
[desktop]
command = ['C:\Program Files\WindowsApps\OpenAI.Codex_26.1120.5083.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe']
~~~

Leave `process_names` at its defaults unless process detection misses your installation. If you set it, list the actual process names shown by your OS, including any Codex backend processes. The toolkit uses those names for `status`, to check whether the test copy is in use, and to check that Codex has exited before replacing data.

## Migration workflow

Run the commands from the project directory in a terminal outside Codex. The examples use `python`. Check its version with `python --version` and use an interpreter running Python 3.11 or later. Depending on your installation, that command may be `python3`, `python3.12`, or `py -3.12`.

1. **Back up sessions on the source machine.**

   ~~~bash
   python codex_session_migrate.py backup
   ~~~

2. **Prepare a test copy on the destination machine.** This fetches the newest snapshot on the configured branch. To select an older one, pass its `codex-git-YYYYMMDD-HHMMSS` directory name after `prepare`.

   ~~~bash
   python codex_session_migrate.py prepare
   ~~~

3. **Give Codex the repair skill and the test path printed by `prepare`.** The skill runs the commands below. The plan lists source directories, affected threads and fields, and whether each directory already exists locally. Confirm the missing project's local directory or choose history-only viewing. [Session repair skill](.agents/skills/codex-session-restore/SKILL.md)

   ~~~bash
   python codex_session_migrate.py repair --plan --test-home "<prepared path>"
   python codex_session_migrate.py repair --test-home "<prepared path>" --map "<mapping file>"
   python codex_session_migrate.py test --check-only
   ~~~

   The mapping file is a private UTF-8 JSON object. Keep it outside the test copy if you want to reuse it. For example, `{"/work/example": "D:/projects/example", "/work/retired": null}` maps one project and preserves history-only access to another. A null mapping uses a stable, empty directory beside the formal home (normally `~/.codex-history-view`) and removes that root from the runtime workspace list. Choose persistent project directories outside the test home. To continue in a history-only project later, rerun repair with its real directory. Omit `--map` when the existing paths are usable. With a custom configuration, pass the same `--config` to each command.

4. **Launch the verified test instance.** From an external terminal, run `test`. On Windows and macOS, fully quit Codex before running the command.

   ~~~bash
   python codex_session_migrate.py test
   ~~~

   Once Codex opens, check a title in the sidebar, page through an ordinary multi-turn chat to its oldest messages, and open an archived chat. Use `status` to inspect the database path when the OS exposes it.

5. **Promote the verified copy.** Run `promote` from a terminal outside Codex after saving any ongoing work. On Windows, the command force-closes the current user's ChatGPT and Codex processes and their children. When the Windows Codex sandbox service is running, approve the administrator prompt using the same Windows account. The command stops the service during the move, restarts it, and launches the desktop with the active data directory. On Linux the command closes Codex before the move. On macOS, quit Codex before running it. Use `promote --no-launch` to leave the desktop closed. If the directory move or log-path update fails, `promote` attempts to restore the original directory.

   ~~~bash
   python codex_session_migrate.py promote
   ~~~

### Local rollback backup

After a successful `promote`, the previous data is saved under `backup_root/codex-YYYYMMDD-HHMMSS/codex`. This holds either the original active home or the files replaced during a Windows file-by-file promotion. Once the new data is in place and its log paths are updated, the toolkit permanently deletes older promotion backups in `backup_root` and keeps the one it just made. Git snapshot retention is controlled separately by `repository.keep`.

Promotion normally moves the test home into the active location. If Windows blocks the directory move or a session still uses a workspace in the test copy, the toolkit moves session logs, indexes, desktop project state and attachments individually. If a file move fails, it attempts to reverse the completed moves and leaves the test data available for retry.

After a successful promotion the toolkit removes the test runtime, launch log and migration staging directories, including earlier failed repairs. It removes the default `~/.codex_copy` directory once empty. The next `prepare` recreates it. A custom test path can share its parent with other data, so cleanup only removes its own files there. Files outside the tool's recognized layout and workspaces referenced by the promoted sessions or rollback backup are kept. An older migration may still use `.codex_copy/.codex-history-view`. A private `workspace-map-codex-git-YYYYMMDD-HHMMSS.json` in the default copy is saved beside it when the name is available.

### History verification

The tool reads the original file contents directly from the pinned Git commit. The source state database selects the entry segment for each thread. For the supported rollout layout, the metadata ID identifies its owner and the filename suffix identifies the physical segment referenced by `history_base.thread_id`. The tool checks the parent chain, its ordinal and byte boundaries, and the SQLite pagination offsets. It preserves unchanged logs and compressed frames byte for byte. Where possible it appends a copy of the client's own saved settings with the confirmed runtime paths. It verifies the effective settings on a fresh server. Where the override is unavailable, it edits only structured JSON values and translates both segment links and SQLite indexes using the offsets from that rewrite.

Acceptance runs independent app-servers on disposable copies of the source and destination data. It enumerates active and archived threads, checks titles and archive flags, and reads every page of turns with full items in order. It compares turn IDs and per-turn digests of the returned items, including message bodies. It also resumes ordinary threads by ID with no workspace overrides and repeats the reads after restarting the server. `test --check-only` performs this comparison without launching the GUI. An unsupported schema, missing segment, invalid offset or history mismatch stops the operation. A failed repair retains the prepared home and prints the location of its staging copy. Retry against the same commit after resolving the reported cause.

Run the synthetic regression suite with `python -m unittest discover -s tests -v`. Set `CODEX_TEST_APP_SERVER` to the desktop's bundled Codex CLI to include the real-reader tests. On Ubuntu 24.04.2, the tests exercised the app-server in desktop package `26.1117.41037` (Codex CLI `0.171.0-alpha.13`), including paginated multi-segment JSONL, Zstandard archives, CRLF, restart, and a deliberately truncated entry. Windows path round trips use synthetic data and simulated destination path availability. Native Windows 11 and macOS desktop launches, sidebar behavior and reopening after promotion still need verification on those systems.
