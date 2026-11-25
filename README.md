# Codex Session Backup and Migration Toolkit

A command-line toolkit for backing up and migrating local Codex sessions. The source machine pushes a session snapshot to a private Git repository. The destination machine repairs and tests the snapshot in an isolated copy before making it the active data directory. The toolkit code can live in a separate public repository.

## How it works

- `backup` creates a `codex-git-YYYYMMDD-HHMMSS` snapshot from the source machine's active Codex data directory. It includes `sessions/`, `archived_sessions/`, `session_index.jsonl`, `state_5.sqlite`, `thread_history_1.sqlite`, and `sqlite/codex.db`. The toolkit uses SQLite's backup API to copy the databases, then commits the snapshot and pushes it with Git.
- `prepare` builds a test copy from the destination machine's active data directory and replaces the session data in that copy with the selected snapshot. The Codex repair skill then inspects the session files and databases and updates log references and workspace paths for the destination machine.
- `test` checks the test copy's SQLite databases and session log paths, then starts Codex with `CODEX_HOME` set to the copy. After you verify the restored chats, `promote` makes the copy active, updates its session log paths, and keeps one local rollback backup.

**Each migration restores one snapshot.** Sessions and newer messages found only on the destination machine remain in the local backup created during promotion. To keep additions from both machines in the active directory, merge their session data in the test copy before promoting it. Set up project source code and workspace files separately on the destination machine.

## Requirements and configuration

Install Python 3.11 or later and Git on both machines. The destination machine needs the Codex desktop app and an existing Codex data directory. On Windows and macOS, install `psutil` with the Python interpreter you use to run the toolkit, for example `python -m pip install psutil`. The toolkit uses it to check which data directory Codex is using and whether it has exited before replacing data.

Create your own **private Git repository** on GitHub to store your session snapshots. Set up Git access to it on each machine using SSH or HTTPS credentials. In the toolkit's project root, create a `config.toml` on each machine with its own settings. For available settings and example values, see [config.example.toml](config.example.toml).

Paths can use `~`. Use absolute paths or paths relative to the directory that contains `config.toml`.

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

`[desktop]` tells the toolkit how to start and identify the Codex desktop app. `desktop.command` specifies the **program to run**. The first item is the executable and any remaining items are its arguments. `paths.test_home` specifies the **data to open**. When you run `test`, the toolkit passes that path as `CODEX_HOME` only to the new Codex process and its children. Your terminal keeps its existing environment.

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

3. **Repair the test copy in Codex on the destination machine.** Reference the project's `SKILL.md` in a Codex chat and give Codex your configured `test_home`. Map workspace paths from the source machine to directories that exist on the destination. [Session repair skill](.agents/skills/codex-session-restore/SKILL.md)

4. **Validate the files and launch the test instance.** From an external terminal, run `test`. It checks the SQLite databases and session log paths before opening Codex with the test copy. On Windows and macOS, fully quit Codex before running the command.

   ~~~bash
   python codex_session_migrate.py test
   ~~~

   Use `test --check-only` to run the file checks alone. Once Codex opens, run `python codex_session_migrate.py status` to check the database path when the OS exposes it, then verify the sidebar, titles, and messages of a few restored chats.

5. **Promote the verified copy.** If testing reveals a problem, quit the test instance and reopen Codex with the active data directory. The test copy stays available for repair. To make the verified copy active, quit Codex, run `promote`, and reopen Codex normally. If the directory move or log-path update fails, `promote` attempts to put the original directory back and retains earlier rollback backups.

   ~~~bash
   python codex_session_migrate.py promote
   ~~~

### Local rollback backup

After a successful `promote`, the previous active directory is saved under `backup_root/codex-YYYYMMDD-HHMMSS/codex`. Once the new directory is in place and its log paths are updated, the toolkit permanently deletes older promotion backups in `backup_root` and keeps the one it just made. Git snapshot retention is controlled separately by `repository.keep`.

The workflow has been tested on Ubuntu with temporary data and a mock desktop launcher. Desktop switching on Windows and macOS still needs testing on those systems.
