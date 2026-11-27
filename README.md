# Codex Session Backup and Migration Toolkit

A command-line toolkit for backing up and migrating local Codex sessions. The source machine pushes a session snapshot to a private Git repository. The destination machine repairs and tests the snapshot in an isolated copy before making it the active data directory. The toolkit code can live in a separate public repository.

## How it works

- `backup` creates a `codex-git-YYYYMMDD-HHMMSS` snapshot from the source machine's active Codex data directory. It includes `sessions/`, `archived_sessions/`, `session_index.jsonl`, `state_5.sqlite`, `thread_history_1.sqlite`, and `sqlite/codex.db`. The toolkit uses SQLite's backup API to copy the databases, then commits the snapshot and pushes it with Git.
- `prepare` builds a test copy from the destination machine's active data directory and imports the selected snapshot. It records the snapshot name and full commit ID in the test home and pins the commit in the local backup clone. `repair` and `test` use that revision even if the branch advances.
- `repair` follows each source SQLite entry through its rollout segments, applies confirmed workspace mappings to a session-only candidate, and compares it with the pinned snapshot using the destination desktop's app-server. After acceptance, it transactionally installs only the session layer into the complete prepared home. `test` verifies and opens that same promotable home as `CODEX_HOME`. After you inspect the restored chats, `promote` makes it active, updates its session log paths, and keeps one local rollback backup.

**Each migration restores one snapshot.** Sessions and newer messages found only on the destination machine remain in the local backup created during promotion. The verifier expects the threads and history of the selected snapshot. Set up project source code and workspace files separately on the destination machine.

## Requirements and configuration

Install Python 3.11 or later and Git on both machines. The destination machine needs the Codex desktop app and an existing Codex data directory. On Windows and macOS, install `psutil` with the Python interpreter you use to run the toolkit, for example `python -m pip install psutil`. The toolkit uses it to check which data directory Codex is using and whether it has exited before replacing data.

Create your own **private Git repository** to store your session snapshots. GitHub is one option; any reachable Git remote is suitable. Set up Git access to it on each machine using SSH or HTTPS credentials. In the toolkit's project root, create a `config.toml` on each machine with its own settings. For available settings and example values, see [config.example.toml](config.example.toml).

Paths can use `~`. Use absolute paths or paths relative to the directory that contains `config.toml`.

For snapshots with `.jsonl.zst` logs, use Python's Zstandard module, install `zstandard` with `python -m pip install zstandard`, or put the `zstd` executable on `PATH`. The restore commands need the Codex CLI distributed with the destination desktop app. They look for it beside the configured GUI launcher in the application's resources. You can supply its exact path with `repair --app-server` or `test --app-server`; a successful repair records the selected client for later checks.

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

`[desktop]` tells the toolkit how to start and identify the Codex desktop app. `desktop.command` lists the executable followed by any arguments. The toolkit sets `CODEX_HOME` for the new process and its children. `test --check-only` verifies `paths.test_home`; a full `test` launches that same complete home. The directory you inspect is therefore the directory later eligible for promotion. Mapped project workspaces must be persistent directories outside the prepared home.

On Linux, the default is `chatgpt` on `PATH`. On macOS, the toolkit looks for `Codex.app` in `/Applications` and `~/Applications`.

On Windows, set `command` to the path of the GUI launcher for your installation. For Microsoft Store or MSIX installations, this may be `ChatGPT.exe`. A `codex.exe` process path reported by PowerShell may belong to a backend process, so verify which executable opens the desktop app. Replace the example path below with the one on your machine. Windows app updates can change the versioned directory under `WindowsApps`, so check the path again after an update.

~~~toml
[desktop]
command = ['C:\Path\To\Codex\ChatGPT.exe']
~~~

Leave `process_names` at its defaults unless process detection misses your installation. If you set it, list the actual process names shown by your OS, including any Codex backend processes. The toolkit uses those names for `status`, to check whether the test copy is in use, and to check that Codex has exited before replacing data.

### Storage and cross-platform model

The private snapshot contains sessions and their indexes/databases, not the complete Codex installation. `prepare` copies the destination machine's existing non-session profile once, then replaces its session layer from the pinned snapshot. The resulting home is intentionally about the same size as the destination's active home and remains independently runnable and promotable.

Large `packages/` and `plugins/` directories contain platform-native backends, plugin hosts, cached versions, and other local runtime data. They stay from the destination machine and are never restored from another OS. `repair` stages only `sessions/`, `archived_sessions/`, `session_index.jsonl`, the supported session databases, and the restore receipt; it does not duplicate `packages/` or `plugins/`. Temporary disk use therefore scales with session history rather than with the whole local installation, and ordinary failed staging is removed unless `--keep-staging` is explicitly requested.

The JSONL/Zstandard/SQLite repair and app-server acceptance logic is shared across Windows, Linux, and macOS. A thin platform layer handles process control, native path forms, junction/symlink behavior, permissions, and desktop launch. Workspace mappings express each destination machine's drive and mount layout; do not hard-code one source machine's paths as universal rules.

## Migration workflow

Run commands from the toolkit root. Codex can perform the restore work through `test --check-only`; run the full desktop `test` and `promote` from an external terminal because they may close Codex, ChatGPT, and their terminals. The examples use `python`. Check its version with `python --version` and use an interpreter running Python 3.11 or later. Depending on your installation, that command may be `python3`, `python3.12`, or `py -3.12`.

1. **Back up sessions on the source machine.**

   ~~~bash
   python codex_session_migrate.py backup
   ~~~

2. **Prepare a test copy on the destination machine.** This fetches the newest snapshot on the configured branch. To select an older one, pass its `codex-git-YYYYMMDD-HHMMSS` directory name after `prepare`.

   ~~~bash
   python codex_session_migrate.py prepare
   ~~~

3. **Give Codex the repair skill and the test path printed by `prepare`.** The skill runs the commands below. The plan lists source directories, affected threads and fields, and whether each directory already exists locally. Confirm the missing project's local directory or choose history-only viewing. [Session repair skill](.agents/skills/codex-session-restore/SKILL.md)

   Replace the two placeholders and paste this prompt into Codex with the skill attached or accessible at the stated path:

   ~~~text
   Use the `codex-session-restore` skill at `<TOOLKIT_ROOT>/.agents/skills/codex-session-restore/SKILL.md` to repair the prepared Codex home at `<PREPARED_TEST_HOME>` until `python codex_session_migrate.py test --check-only` passes.

   Work from `<TOOLKIT_ROOT>` and use its destination-machine configuration. Verify the prepared receipt and pinned commit first. Generate or reuse a private workspace-map file outside the toolkit checkout, formal home, and prepared home. Reuse only mappings whose target directories still exist. In a newly generated draft, `null` for an entry listed under `unresolved` is only a placeholder, not permission for history-only access: ask me once for all genuinely ambiguous paths, and let me choose an existing absolute project directory or explicitly approve history-only access.

   Modify only the prepared home, the private mapping file, the configured history-view directory, and toolkit-owned temporary staging beside the prepared home; remove ordinary staging when each command finishes. Do not rerun `prepare`, modify the formal Codex home, edit toolkit source code for a data-specific failure, hand-edit SQLite or rollout files, retain large staging by default, launch the full desktop `test`, or promote the copy.

   After check-only succeeds, report the pinned commit, snapshot, client version, verified thread and visible-turn counts, history-only workspaces, whether repair staging was removed, and the exact external-terminal command for the full test.
   ~~~

   ~~~bash
   python codex_session_migrate.py repair --plan --test-home "<prepared path>" --write-map-draft "<private mapping file>"
   python codex_session_migrate.py repair --test-home "<prepared path>" --map "<mapping file>"
   python codex_session_migrate.py test --check-only
   ~~~

   The plan writes a private UTF-8 JSON draft and reports the paths that still need a decision. Keep it outside the toolkit checkout, formal home, and prepared home so it can be reused after the copy is deleted. For example, `{"/work/example": "D:/projects/example", "/work/retired": null}` maps one project and preserves history-only access to another. In a new draft, unresolved paths are temporarily written as `null`; they become history-only only after the user explicitly approves that choice. A confirmed null mapping uses the stable, empty directory beside the formal home (normally `~/.codex-history-view`). Accept an automatic replacement only when the destination is an existing, unique project directory; decide the remaining ambiguous projects once, or mark them history-only. To continue in a history-only project later, rerun repair with its real directory. Pass the draft with `--map` whenever it contains reused cross-platform mappings or confirmed null values, even if the plan reports `unresolved: []`; omit it only when every source path is already usable as written. Use `repair --keep-staging` only when deliberately debugging a failed candidate. With a custom configuration, pass the same `--config` to each command.

4. **Launch the verified test instance.** Run `test` from an external terminal. On Windows, save active work first; the command closes the current user's Codex and ChatGPT processes before final validation and launch. On macOS and Linux, fully quit an existing test instance before rerunning. After the workflow above, `test` validates and reuses the unlaunched acceptance marker written by `test --check-only`, so it normally does not repeat the expensive full-history comparison. The GUI opens `test_home` itself, so new chats, settings, caches, and normal SQLite/log updates become part of the exact copy eligible for promotion.

   ~~~bash
   python codex_session_migrate.py test
   ~~~

   Once Codex opens, check a title in the sidebar, page through an ordinary multi-turn chat to its oldest messages, and open an archived chat. Use `status` to inspect the database path when the OS exposes it.

   To reopen a copy that has already passed strict acceptance and then been used by the GUI, run `python codex_session_migrate.py test --reopen`. This requires its `.session-test.json` marker, checks the current home against the accepted baseline while allowing additional GUI-created threads, and launches the same home again. To reset it to the pinned snapshot instead, rerun the session-only `repair` with the same map and then run strict `test` again.

5. **Promote the verified copy.** Run `promote` from a terminal outside Codex after saving any ongoing work. On Windows, the command force-closes the current user's ChatGPT and Codex processes and their children. When the Windows Codex sandbox service is running, approve the administrator prompt using the same Windows account. The command stops the service during the move, restarts it, and launches the desktop with the active data directory. On Linux the command closes Codex before the move. On macOS, quit Codex before running it. Use `promote --no-launch` to leave the desktop closed. If the directory move or log-path update fails, `promote` attempts to restore the original directory.

   ~~~bash
   python codex_session_migrate.py promote
   ~~~

### Local rollback backup

After a successful `promote`, the previous data is saved under `backup_root/codex-YYYYMMDD-HHMMSS/codex`. This holds either the original active home or the files replaced during a Windows file-by-file promotion. Once the new data is in place and its log paths are updated, the toolkit permanently deletes older promotion backups in `backup_root` and keeps the one it just made. Git snapshot retention is controlled separately by `repository.keep`.

Promotion normally moves the test home into the active location. If Windows blocks the directory move or a session still uses a workspace in the test copy, the toolkit moves session logs, indexes, desktop project state and attachments individually. If a file move fails, it attempts to reverse the completed moves and leaves the test data available for retry.

After a successful promotion the toolkit removes launch logs and recognized migration staging directories, including earlier failed repairs. It removes the default `~/.codex_copy` directory once empty. The next `prepare` recreates it. A custom test path can share its parent with other data, so cleanup only removes tool-owned files there. Files outside the recognized layout and workspaces referenced by the promoted sessions or rollback backup are kept. An older migration may still use `.codex_copy/.codex-history-view`. Keep reusable private mapping files outside the copy so cleanup cannot remove them.

### History verification

The tool reads the original file contents directly from the pinned Git commit. The source state database selects the entry segment for each thread. For the supported rollout layout, the metadata ID identifies its owner and the filename suffix identifies the physical segment referenced by `history_base.thread_id`. The tool checks the parent chain, its ordinal and byte boundaries, and the SQLite pagination offsets. It preserves unchanged logs and compressed frames byte for byte. Where possible it appends a copy of the client's own saved settings with the confirmed runtime paths. It verifies the effective settings on a fresh server. Where the override is unavailable, it edits only structured JSON values and translates both segment links and SQLite indexes using the offsets from that rewrite.

Acceptance runs independent app-servers on disposable session candidates for the source and destination data. It enumerates active and archived threads, checks titles and archive flags, and reads every page of turns with full items in order. It compares turn IDs and per-turn digests of the returned items, including message bodies. It also resumes ordinary threads by ID with no workspace overrides and repeats the reads after restarting the server. `test --check-only` compares the pinned snapshot with the repaired home without launching the GUI, then records semantic acceptance in `.session-test.json`. A full `test` validates and reuses that marker before launching the same promotable home; if no marker exists, it performs strict acceptance first. Later `test --reopen` verifies the accepted thread, title, archive, turn, and message fingerprints while allowing additional GUI-created threads. An unsupported schema, missing segment, invalid offset, or history mismatch stops the operation. Ordinary failed session staging is deleted by default; the prepared home and receipt remain unchanged. Do not re-prepare or hand-edit SQLite/logs to clear a data error. Retry against the same commit after resolving its concrete cause, and request `--keep-staging` only when the candidate itself is needed for diagnosis.

Run the regression suite with `python -m unittest discover -s tests -v`. Set `CODEX_TEST_APP_SERVER` to the destination desktop's bundled Codex CLI to include the real app-server reader tests. The suite covers multi-segment and compressed rollouts, byte offsets, workspace remapping, session-only installation and rollback, test markers, direct test-home launch, reopen semantics, and promotion fallbacks. Automated tests complement, but do not replace, `test --check-only` and the user's visual desktop inspection on the destination machine.
