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

The dashboard's **Add sync** flow first shows a read-only review. Discovery, Asset Preview, and **Review sync** read metadata without creating tasks, saving a sync, or downloading files. Only the final **Add sync** button saves the schedule and starts the optional background first run. Editing the draft invalidates that review; reviews also expire after 15 minutes. The CLI `setup` command performs the initial sync before saving its schedule; a failed initial sync can therefore leave downloaded files without a new scheduled task.

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

New dashboard syncs include all release types with one shared limit of five releases total. **Discover release types**, or **Check for new types** on an existing sync, reads the actual repository metadata. The available choices are not a fixed list shared by every repository. Types use the publisher’s stable/prerelease flag and qualifiers in release tags; example tags show why releases were grouped together. A recognizable tag family can also appear on a publisher-marked stable release, such as an LTS group. Type inference is a convenience, not a guarantee of release quality.

Leave **Choose release types** unchecked to include all types, including types discovered in future syncs. Enable it to keep a fixed selected list. Newly discovered types then appear unchecked; they are not silently added to the selection. Previously seen but currently unavailable types stay visible, and an existing selection can resume matching if the type returns. Scheduled runs keep the repository’s type catalog current.

The shared limit is applied after type selection, by publication date, newest first. Enable **Use separate release limits** to choose a count for each type—for example, one LTS release and five nightly releases if those types exist in the repository. Counts are 1–100. Types without a custom count use the displayed default per-type limit, including newly discovered types when all types are enabled. With separate limits, the total retained count can exceed 100 across types.

Under **Customize type grouping**, add or edit a label and comma-separated tag patterns. Tag patterns are case-insensitive glob matches: `*`, `?`, and character classes such as `[ab]` are supported. Rules are ordered; the first matching rule assigns the release to that custom group, and unmatching releases retain their inferred group. Edit or remove rules to correct grouping and recheck types for updated examples. These rules select future matching tags automatically; they do not pin an exact set of releases. Grouping patterns match release tags, while asset patterns match downloadable filenames.

Existing saved configurations preserve their prior behavior until you explicitly edit type selection. This includes older stable-only choices and older configurations that included prereleases. Saving settings itself does not delete downloads; the next successful sync can remove recognized release folders outside the selection through normal retention. Saving settings on an archived sync keeps it archived; **Resume sync** is a separate action. Source-only entries save type choices for future release syncing.

The source branch is independent of release types and tags. Selecting an older release or multiple release types does not switch the source checkout to those tags; the checkout still follows its configured branch. Each selected release includes a source ZIP regardless of asset patterns. **Source only** skips all release assets and release source ZIPs. Asset Preview uses the current editor’s types, grouping rules, limits, and asset patterns, including unsaved choices, without saving them.

The Add review lists current matching assets and source ZIPs, their release types and tags, local folders, schedule, and first-run choice. Its known download size excludes files without published sizes, including source ZIPs, and excludes the source checkout and Git history. It is not a total disk-space estimate. Remote releases may change after the review; a normal sync uses the saved rules when it runs.

For CLI `run`, `setup`, and `schedule`, omitting type options preserves an existing saved selection; an unconfigured repository defaults to all types with a shared limit. `--all-release-types` explicitly includes every type. Repeat `--release-type TYPE_ID` to select types, and repeat `--type-limit TYPE_ID=COUNT` to enable separate limits. For example, if a repository has the `stable` and `prerelease:nightly` types:

```powershell
python .\github_sync.py run 'owner/repository' --release-type stable --release-type prerelease:nightly --type-limit stable=1 --type-limit prerelease:nightly=5
```

`--per-type-release-limits` enables separate limits using `--last` for types without custom counts. `--joint-release-limits` restores the shared `--last` limit; stored custom counts remain inactive. Custom grouping rules are edited on the dashboard. Older stable/prerelease flags remain available for compatibility, but cannot be combined with the new type-policy flags. Prefer `run-saved` to use a dashboard-configured sync exactly as saved.

The app skips unchanged assets based on saved asset identity and size. It records SHA-256 checksums for new downloads in `sync.log`; this is a record of downloaded bytes, not a publisher signature or malware check. Release source ZIPs are refreshed when the corresponding upstream tag changes.

Removing a pattern preserves old downloads. Use **Refresh Assets** to preview and remove recognized assets that no longer match saved patterns. It uses local metadata and, for older rolling-release assets, verified download history. Unrecognized files are kept. Newly matching files download on a later sync for releases within the configured limit.

**Refresh releases** is a separate operation for rebuilding release downloads. Save your release selection and asset patterns, then choose **Preview Refresh releases**. The preview lists every recognized local release folder to delete, their combined size, unrecognized items that will remain, and the types and tags currently selected for download. Nothing is deleted until you click the explicit confirmation button. The worker rechecks the preview and repository lock before deleting, then runs in the background with progress on the dashboard.

Refresh removes the complete listed release folders, including their assets, source ZIPs, and metadata. The source checkout and Git history are not deleted; the subsequent normal sync can update them. A failed metadata check or no eligible remote releases blocks deletion. If a download fails after deletion, removed files are not restored automatically; back up anything you need before confirming. Refresh is unavailable while syncing, while archived, or while set to source only. It does not silently resume archived syncs.

For cleanup and refresh, a changed configuration or file can invalidate the preview. Generate a new preview before confirming. Local cleanup and **Refresh Assets** remain available on archived syncs; **Refresh releases** does not. These operations require write/delete access to the installation’s release folders under the Windows account running the helper; no administrator elevation is requested by the app.

## Delete a sync

In **Manage sync**, choose **Delete sync**. The confirmation starts with **Also delete local files** unchecked every time you open it, including when changing repositories. Click **Preview deletion**, review the paths, and explicitly confirm:

- **Delete sync (keep files)** removes the saved dashboard entry and scheduled task. The source checkout, Git history, downloaded releases, local edits, and manually added files stay on the PC. Their contents are not scanned for this option.
- **Delete sync and files** additionally deletes the complete source and release folders shown in the preview. The preview shows which folders exist, the file count, and total bytes. All contents are included: `.git`, local modifications, unrecognized release folders, and manually added files. This is permanent deletion, unlike release cleanup that preserves unrecognized data.

Changing the checkbox invalidates the preview; generate a new preview before confirming. If deletion fails partway, the sync remains archived, completed deletions stay deleted, and the dashboard reports deleted, possibly partial, and remaining paths. Check those paths before retrying. No failed deletion is presented as a successfully removed sync. **Archive sync** remains a separate choice that keeps both the dashboard entry and every local file.

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

Intervals are 5–1440 minutes; new syncs default to **240 minutes (four hours)**, and 1440 creates a daily trigger. Updating the app does not change existing saved schedules. Optional start times are `HH:MM` in the PC's local Windows time. Dashboard timestamps use your browser's local time, while log timestamps are UTC.

Do not configure the same repository in two separate installations under the same Windows account: task names identify the repository, not the installation folder. Management checks task ownership before modifying actions. Wait for a running sync to finish before changing its settings or deleting its downloaded data.

## Back up, update, or uninstall

**Back up:** retain the installation folder, especially `sync-config.json`, `.sync-state`, `sync.log`, source checkouts, and release folders. `release.json` and source ZIP `.tag` files are part of the release records; preserve them. A filesystem backup is separate from Windows Task Scheduler registrations. Pause/archive the syncs or wait until they are idle before taking a consistent copy.

**Update:** back up first, wait for syncs to finish, and replace the application files with a newer release of this project. Preserve your generated configuration, state, logs, dependencies, source checkouts, and release folders. Run `python .\install.py` again if setup requirements changed, then reopen the dashboard launcher to load the current helper. The app does not automatically update itself.

**Move folders or Python:** scheduled tasks contain absolute script and interpreter paths. Moving the installation or removing its Python interpreter breaks those tasks. Prefer keeping both paths stable. To relocate, archive the syncs using the old installation first, move the complete folder, run setup with the chosen Python in the new folder, then resume the syncs there. Do not leave duplicate active installations.

**Uninstall:** use **Archive** or **Delete sync** for each active sync first, and confirm the corresponding `Git Sync - …` tasks are gone in Windows Task Scheduler. Leave **Also delete local files** unchecked if you want to keep the data. Close the dashboard and allow its helper to exit. Then delete the application folder only if you also intend to delete its stored source and release downloads; otherwise move or back up the data you want to keep. The tool does not uninstall Python, Git, or Git LFS.

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
