# Codex Session Backup and Migration Toolkit

A command-line toolkit for backing up and migrating local Codex sessions. The source machine pushes a session snapshot to a private Git repository. The destination machine repairs and tests the snapshot in an isolated copy before making it the active data directory. The toolkit code can live in a separate public repository.

## How it works

- `backup` creates a `codex-git-YYYYMMDD-HHMMSS` snapshot from the source machine's active Codex data directory. It includes `sessions/`, `archived_sessions/`, `session_index.jsonl`, `state_5.sqlite`, `thread_history_1.sqlite`, and `sqlite/codex.db`. The toolkit uses SQLite's backup API to copy the databases, then commits the snapshot and pushes it with Git.
- `prepare` builds a test copy from the destination machine's active data directory and replaces the session data in that copy with the selected snapshot. The Codex repair skill then inspects the session files and databases and updates log references and workspace paths for the destination machine.
- `test` launches the desktop app with `CODEX_HOME` pointing to the test copy. After you verify the restored sessions, `promote` makes the copy active, updates its session log paths, and backs up the previous active directory.

**Each migration restores one snapshot.** Sessions and newer messages found only on the destination machine remain in the local backup created during promotion. To keep additions from both machines in the active directory, merge their session data in the test copy before promoting it. Set up project source code and workspace files separately on the destination machine.

## Requirements and configuration

Install Python 3.11 or later and Git on both machines, and set up access to the private backup repository. The destination machine needs the Codex desktop app and an existing Codex data directory. On Windows and macOS, install `psutil` with the Python interpreter you use to run the toolkit, for example `python -m pip install psutil`. The toolkit uses it to inspect desktop processes when switching data directories.

On each machine, use [config.example.toml](config.example.toml) as the starting point for `config.toml`. Paths can use `~`. Relative paths are resolved from the configuration file's directory.

On Windows, you can use a single-quoted TOML string to keep backslashes literal. For example, set `formal_home` under `[paths]` as shown below.

~~~toml
formal_home = 'C:\Users\your-name\.codex'
~~~

| Setting | What to configure |
| --- | --- |
| `paths.formal_home` | Active Codex data directory, defaulting to `~/.codex`. The `backup` command reads from this directory. |
| `paths.test_home` | Isolated test copy, set to `~/.codex_copy/.codex` in the template. Each successful `prepare` replaces this copy. |
| `paths.backup_root` | Location for backups created during promotion, set to `~/.codex-backups` in the template. Promotion requires the active, test, and backup directories to be on the same filesystem. |
| `paths.checkout` | Local clone of the private backup repository, set to `session-backup-checkout` inside the project in the template. |
| `repository.url` and `repository.branch` | Private backup repository URL and branch. Use the same values on the machines you migrate between. |
| `repository.keep` and `repository.timestamp_utc_offset_minutes` | The branch keeps the five newest snapshot directories by default. Older snapshots remain in Git history. Use the same timestamp offset on machines that push to the same branch. |
| `desktop.command` | On Windows, specify the installed Codex executable. Linux looks for `chatgpt` on `PATH`. macOS looks for `Codex.app`. Override this setting for a custom installation. |

Keep session snapshots in the private repository because they contain chat messages, tool output, and local paths. Before publishing the toolkit, check the staged files, especially if you changed the checkout directory. The ignore rules cover the local `config.toml` and the default checkout directory. [Ignore rules](.gitignore)

## Migration workflow

Run the commands from the project directory in a terminal outside Codex. Use `python`, `python3`, or `py -3`, depending on which command runs Python 3.11 or later on your machine. On Linux, you can also use `bash ./codex-session-migrate.sh` in place of `python codex_session_migrate.py`.

1. **Back up sessions on the source machine.**

   ~~~bash
   python codex_session_migrate.py backup
   ~~~

2. **Prepare a test copy on the destination machine.** This fetches the newest snapshot on the configured branch. To select an older one, pass its `codex-git-YYYYMMDD-HHMMSS` directory name after `prepare`.

   ~~~bash
   python codex_session_migrate.py prepare
   ~~~

3. **Repair the test copy in Codex on the destination machine.** Reference the project's `SKILL.md` in a Codex chat and give Codex your configured `test_home`. Map workspace paths from the source machine to directories that exist on the destination. [Session repair skill](.agents/skills/codex-session-restore/SKILL.md)

4. **Verify the restored sessions.** Run `test --check-only` to check the SQLite databases and session log paths. Once it passes, run `test` from your terminal. On Windows and macOS, fully quit Codex before running `test`. After the app starts, use `status` to check the running processes and any database paths it can detect. Open a few recent, imported, and archived chats to verify their titles and messages.

   ~~~bash
   python codex_session_migrate.py test --check-only
   python codex_session_migrate.py test
   python codex_session_migrate.py status
   ~~~

5. **Promote the verified copy.** If testing reveals a problem, run `python codex_session_migrate.py restore` and reopen Codex with the active data directory. The test copy stays available for repair. Once the restored chats look right, run `promote` and reopen Codex with the active directory. On Windows and macOS, fully quit Codex before running either `restore` or `promote`. The previous active directory is saved under `backup_root/codex-YYYYMMDD-HHMMSS/codex`. If the directory move or log-path update fails, `promote` attempts to put the original directory back.

   ~~~bash
   python codex_session_migrate.py promote
   ~~~

The current version has been tested on Ubuntu with a temporary repository and a mock launcher for backup, historical snapshot import, log-path validation, test launch, and rollback after a failed promotion. Desktop switching on Windows and macOS still needs testing on those systems.
