# Usage and maintenance

The examples below assume the app is installed in `C:\OfflineGitSync`. Use your actual installation path and the same Python interpreter used for `install.py`.

## Command line

Open PowerShell in the installation folder:

```powershell
Set-Location C:\OfflineGitSync
```

Open the management dashboard:

```powershell
python .\github_sync.py dashboard --open
```

Run one configured repository using its saved settings:

```powershell
python .\github_sync.py run-saved 'owner/repository'
```

Run every configured, active sync in sequence:

```powershell
python .\github_sync.py run-all
```

`run-all` continues after individual failures, reports failed repositories at the end, and skips archived entries. Overlapping runs for the same repository are skipped. Separate repositories can run concurrently through their individual scheduled tasks.

Create a sync with an immediate first run, then register a daily task:

```powershell
python .\github_sync.py setup 'owner/repository' --asset-pattern '*windows*x64*.zip' --asset-pattern '*.exe' --last 5 --every-minutes 1440 --start-time '19:00'
```

The dashboard's **Add sync** flow saves the schedule before starting its optional background first run. The CLI `setup` command performs the initial sync before saving its schedule; a failed initial sync can therefore leave downloaded files without a new scheduled task.

Save or update a schedule without syncing immediately:

```powershell
python .\github_sync.py schedule 'owner/repository' --asset-pattern '*.zip' --last 5 --every-minutes 240
```

Perform an immediate sync with explicit options:

```powershell
python .\github_sync.py run 'owner/repository' --asset-pattern '*.zip' --last 5
```

`run` uses the options on that command line rather than loading all saved settings. Prefer `run-saved` for an already configured sync. Archived entries are skipped even when invoked manually; resume them first.

Choose a source branch and skip releases:

```powershell
python .\github_sync.py setup 'owner/repository' --branch 'main' --source-only --every-minutes 1440
```

For compatible Forgejo/Gitea repositories, replace `owner/repository` with the repository's full `https://host/owner/repository` URL. Saved identifiers include the host. Each host is checked for its supported API; a similar-looking website alone is not enough.

Generate a static local status snapshot without opening the helper:

```powershell
python .\github_sync.py dashboard
```

`dashboard.html` is a static snapshot. Open **Open dashboard.vbs** or use `dashboard --open` for management controls and first-sync progress.

## Asset patterns and releases

Patterns may contain letters, numbers, dots, dashes, underscores, `*`, and `?`. Matching is case-insensitive; `*` matches any sequence and `?` matches one character. Repeat `--asset-pattern` at the command line to select the union of multiple patterns. The default is `*windows*x64*.zip`.

Release selection is by publication date, newest first, including published prereleases. The branch setting affects the source checkout, not which repository-wide releases are selected. Each selected release includes a source ZIP regardless of asset patterns. **Source only** skips all release assets and release source ZIPs.

The app skips unchanged assets based on saved asset identity and size. It records SHA-256 checksums for new downloads in `sync.log`; this is a record of downloaded bytes, not a publisher signature or malware check. Release source ZIPs are refreshed when the corresponding upstream tag changes.

Removing a pattern preserves old downloads. Use **Refresh Assets** to preview and remove recognized assets that no longer match saved patterns. It uses local metadata and, for older rolling-release assets, verified download history. Unrecognized files are kept. Newly matching files download on a later sync for releases within the configured limit.

For both cleanup operations, a changed configuration or file can invalidate the preview. Generate a new preview before confirming. Cleanup remains available on archived syncs.

## Storage and size

Everything below is relative to the installation folder:

```text
offline-git-sync/
  github_*.py                       application modules
  Open dashboard.vbs               generated local launcher
  sync-config.json                 your saved syncs and settings
  sync.log                         timestamped downloads, summaries, errors
  dashboard.html                   generated static snapshot
  .tools/python/                   local Truststore dependency
  .sync-state/                     status, locks, progress, and caches
  owner/repository/                GitHub source checkout and .git
  owner/repository-releases/tag/   GitHub release files and metadata
  host/owner/repository/           Forgejo/Gitea source checkout
  host/owner/repository-releases/tag/
```

**Sync Size** sums file lengths in the source checkout—including `.git` and LFS objects—and the entire release folder. It includes all local releases, extra files, and partial downloads, not just the configured release count. Source/Git and releases are shown separately. Binary units are used: 1 GiB is 1,073,741,824 bytes.

This is not filesystem “size on disk”; compression and hard links can change actual allocated space. Junctions, symbolic links, and other reparse points are not followed. A partial count displays `≥`. Size scans are cached for up to 60 seconds and invalidated after syncs and cleanup.

Local release counts include recognized release folders that exist on disk. Unrecognized folders are reported separately and left alone. The latest result, last completed sync, and last content change are separate values; an unchanged successful run retains the prior change summary.

## Scheduling

Schedules use Windows Task Scheduler under the account that created them. New tasks invoke `run-saved`, so they use the latest saved settings. GitHub task names use `Git Sync - owner-repository`; other hosts include the hostname.

The PC must be running with network access for downloads. This is not a Windows service and it does not wake the PC, provide credentials for a logged-out session, or promise catch-up runs after sleep or shutdown. Actual execution also depends on the task's Windows conditions. Use Task Scheduler to inspect its triggers, account, and execution history. The dashboard describes the saved sync configuration; it is not a complete audit of externally edited or disabled Windows tasks.

Intervals are 5–1440 minutes; 1440 creates a daily trigger. Optional start times are `HH:MM` in the PC's local Windows time. Dashboard timestamps use your browser's local time, while log timestamps are UTC.

Do not configure the same repository in two separate installations under the same Windows account: task names identify the repository, not the installation folder. Management checks task ownership before modifying actions. Wait for a running sync to finish before changing its settings or deleting its downloaded data.

## Back up, update, or uninstall

**Back up:** retain the installation folder, especially `sync-config.json`, `.sync-state`, `sync.log`, source checkouts, and release folders. `release.json` and source ZIP `.tag` files are part of the release records; preserve them. A filesystem backup is separate from Windows Task Scheduler registrations. Pause/archive the syncs or wait until they are idle before taking a consistent copy.

**Update:** back up first, wait for syncs to finish, and replace the application files with a newer release of this project. Preserve your generated configuration, state, logs, dependencies, source checkouts, and release folders. Run `python .\install.py` again if setup requirements changed, then reopen the dashboard launcher to load the current helper. The app does not automatically update itself.

**Move folders or Python:** scheduled tasks contain absolute script and interpreter paths. Moving the installation or removing its Python interpreter breaks those tasks. Prefer keeping both paths stable. To relocate, archive the syncs using the old installation first, move the complete folder, run setup with the chosen Python in the new folder, then resume the syncs there. Do not leave duplicate active installations.

**Uninstall:** use **Archive** or **Remove** for each active sync first, and confirm the corresponding `Git Sync - …` tasks are gone in Windows Task Scheduler. Close the dashboard and allow its helper to exit. Then delete the application folder only if you also intend to delete its stored source and release downloads; otherwise move or back up the data you want to keep. The tool does not uninstall Python, Git, or Git LFS.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `python` or `git` is not found | Install the prerequisite, open a new PowerShell window, and verify its version. If multiple Pythons are installed, use the full path to the one used for setup. |
| Truststore is missing | Run `python .\install.py` with the same interpreter used by the launcher and scheduled tasks. The dependency belongs in `.tools\python` beside the scripts. |
| The launcher does not open | Use `python .\github_sync.py dashboard --open` in PowerShell to see the error. Check Windows Script Host/VBScript policy and whether the chosen Python path still exists. |
| A task cannot be created or modified | Run the dashboard under the Windows account that owns the tasks. Check Windows policy and Task Scheduler access. The dashboard reports failed task changes rather than treating them as success. |
| “Scheduled task action is not uniquely owned by this sync” | Expand **Scheduled task details** in the pattern editor. Compare the actual action with the expected script, interpreter, and repository in Task Scheduler. Unsaved pattern edits are retained. Do not overwrite a task belonging to something else. |
| A network or rate-limit error | Check the host and connection, use a less frequent schedule, and wait for the host's rate-limit window. Browser sign-in does not raise this app's API allowance. Temporary Git fetch interruptions receive two retries; clone failures and interrupted download bodies may require the next sync. |
| A certificate error | Check the PC's time, trusted certificates, and any network interception. Do not disable TLS verification as a workaround. |
| LFS is unavailable or incomplete | Install current Git LFS and check `git lfs version`. A missing server object must be restored by its publisher; normal source/release downloads can still succeed with a warning. |
| Source update refuses local changes or the branch differs | Preserve your work and inspect the checkout. The tool does not automatically discard conflicting local edits or switch an existing checkout to a different branch. |
| Cleanup skips files | The files could not be verified as this sync's release data, or changed after preview. Inspect them and make a fresh preview; do not assume every folder is safe to delete. |

Read recent log entries with:

```powershell
Get-Content .\sync.log -Tail 30
```

Logs and configuration can reveal local paths and the repositories you follow. Review them before sharing. The management helper binds only to this PC, checks request origins, and uses a per-process session token; it is not designed for remote hosting or access by untrusted users.
