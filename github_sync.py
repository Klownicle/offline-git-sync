"""Sync public GitHub and compatible Forgejo/Gitea repositories on Windows."""

import argparse
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
import github_dashboard as dashboard
from github_progress import Tracker
from github_https import urlopen
from github_sources import parse_repository, is_repository_key, normalize_release
import github_forgejo as forgejo


# Keep the application, saved settings and downloaded data together. Scheduled
# tasks use this script's absolute path, so their working directory is irrelevant.
BASE = Path(__file__).resolve().parent
CONFIG = BASE / "sync-config.json"
HEADERS = {"User-Agent": "Windows-GitHub-Sync", "Accept": "application/vnd.github+json"}
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def emit(message, *, error=False):
    if not re.match(r"^\d{4}-\d{2}-\d{2}T", message):
        message = f"{datetime.now(timezone.utc).isoformat()} {message}"
    with (BASE / "sync.log").open("a", encoding="utf-8") as output:
        output.write(message + "\n")
    stream = sys.stderr if error else sys.stdout
    if stream is not None:
        print(message, file=stream, flush=True)


def git_executable():
    found = shutil.which("git")
    if found:
        return Path(found)
    # A task can inherit PATH from before Git was installed. Search the usual
    # system and per-user Git for Windows locations as well.
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "cmd" / "git.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "cmd" / "git.exe",
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        / "Programs" / "Git" / "cmd" / "git.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Git was not found. Install Git for Windows or add it to PATH.")


def git_environment(executable):
    environment = os.environ.copy()
    helper = executable.parent.parent / "mingw64" / "bin"
    if (helper / "git-remote-https.exe").exists():
        environment["GIT_EXEC_PATH"] = str(helper)
    # Scheduled jobs may inherit an older PATH than an interactive terminal.
    candidates = [executable.parent, helper, BASE / ".tools" / "git-lfs",
                  Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git LFS",
                  Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
                  / "Programs" / "Git LFS"]
    environment["PATH"] = os.pathsep.join(
        [str(path) for path in candidates if path.is_dir()] + [environment.get("PATH", "")])
    return environment


def sync_lfs(source, run_git, available):
    """LFS hydration must not block source/release downloads; report incompleteness."""
    pointers = run_git("-C", str(source), "grep", "--cached", "-l",
                       "^version https://git-lfs.github.com/spec/v1$", "--",
                       allow_no_matches=True)
    if not pointers:
        return "none"
    if not available:
        emit(f"WARNING: {source}: Git LFS is unavailable; LFS-managed files remain "
             "pointers unless already downloaded. Source and releases will still sync.")
        return "unavailable"
    try:
        run_git("-C", str(source), "lfs", "pull", "origin", lfs_filters=True)
    except RuntimeError as error:
        emit(f"WARNING: {source}: LFS download incomplete; keeping available files "
             f"and continuing release downloads. Will retry next sync. {error}")
        return "incomplete"
    return "ok"


def safe_name(value):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    if not name or name in {".", ".."}:
        raise ValueError(f"Unsafe name: {value!r}")
    return name


def release_page(owner, repo, page):
    url = f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=100&page={page}"
    request = urllib.request.Request(url, headers=HEADERS)
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def repository_release_page(repository, page):
    """Use a fixed provider API while preserving the GitHub paging helper."""
    reference = parse_repository(repository, allow_url=False)
    if reference.provider == "github":
        return release_page(reference.owner, reference.name, page)
    request = urllib.request.Request(reference.release_page_url(page), headers=reference.headers)
    with forgejo.urlopen(request, timeout=30) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise RuntimeError(f"{reference.host} release metadata exceeds the supported size")
    releases = json.loads(raw)
    if not isinstance(releases, list) or len(releases) > 100:
        raise RuntimeError(f"{reference.host} returned invalid release metadata")
    return [normalize_release(reference, release) for release in releases]


def download_asset(asset, destination, previous_id, progress=None, *, headers=None, opener=None):
    if (destination.exists() and destination.stat().st_size == asset["size"]
            and previous_id == asset["id"]):
        return False
    temporary = destination.with_name(destination.name + ".part")
    digest = hashlib.sha256()
    request = urllib.request.Request(asset["browser_download_url"], headers=headers or HEADERS)
    try:
        with (opener or urlopen)(request, timeout=90) as source, temporary.open("wb") as target:
            received = 0
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                if progress:
                    progress.transfer(received, asset["size"])
        if temporary.stat().st_size != asset["size"]:
            raise OSError(f"Incomplete download: {destination}")
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    emit(f"Downloaded {destination}: SHA256 {digest.hexdigest()}")
    return True


def download_source_archive(url, destination, tag_oid, progress=None, *, headers=None, opener=None):
    tag_marker = destination.with_name(destination.name + ".tag")
    previous_oid = tag_marker.read_text(encoding="ascii").strip() if tag_marker.exists() else None
    if (destination.exists() and zipfile.is_zipfile(destination)
            and previous_oid == tag_oid):
        return False
    if previous_oid and previous_oid != tag_oid:
        emit(f"Tag changed for {destination.name}; refreshing source ZIP")
    temporary = destination.with_name(destination.name + ".part")
    request = urllib.request.Request(url, headers=headers or HEADERS)
    try:
        with (opener or urlopen)(request, timeout=90) as source, temporary.open("wb") as target:
            length = source.headers.get("Content-Length", "")
            total = int(length) if length.isdigit() else None
            received = 0
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
                received += len(chunk)
                if progress:
                    progress.transfer(received, total)
        if not zipfile.is_zipfile(temporary):
            raise OSError(f"Invalid source ZIP: {destination}")
        os.replace(temporary, destination)
        tag_marker.write_text(tag_oid + "\n", encoding="ascii")
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    emit(f"Downloaded {destination}")
    return True


def prune_old_releases(releases_root, keep_tags):
    from github_management import prune_releases
    return prune_releases(releases_root, keep_tags)


def advance_source(source, branch, run_git):
    """Follow remote rewrites only when the old tip is known downloaded history."""
    target = f"refs/remotes/origin/{branch}"
    baseline = f"refs/github-sync/last/{branch}"
    before = run_git("-C", str(source), "rev-parse", "HEAD")
    after = run_git("-C", str(source), "rev-parse", target)
    if before != after:
        ahead = int(run_git("-C", str(source), "rev-list", "--count", f"{target}..HEAD"))
        if not ahead:
            run_git("-C", str(source), "merge", "--ff-only", target)
        else:
            saved = run_git("-C", str(source), "rev-parse", "--verify", "--quiet",
                            baseline, allow_no_matches=True)
            history = run_git("-C", str(source), "reflog", "show", "--format=%H", target)
            if before != saved and before not in history.splitlines():
                raise RuntimeError("Local branch has commits not identified as previously "
                                   "downloaded upstream history; refusing to replace them")
            if run_git("-C", str(source), "status", "--porcelain", "--untracked-files=normal"):
                raise RuntimeError("Upstream history changed, but this checkout has local "
                                   "changes; preserving them and stopping source update")
            recovery = f"refs/github-sync/backups/{branch}/{before}"
            run_git("-C", str(source), "update-ref", recovery, before)
            # --keep also refuses file conflicts if an edit appears after the check.
            run_git("-C", str(source), "reset", "--keep", after)
            emit(f"{source}: followed rewritten upstream history {before[:12]} -> "
                 f"{after[:12]}; previous revision retained at {recovery}")
    run_git("-C", str(source), "update-ref", baseline, after)
    return before, after


def sync(repository, asset_patterns, last, branch=None, source_only=False, *, saved=False):
    reference = parse_repository(repository)
    repository = reference.key
    # Also prevents manual run-all from overlapping the same scheduled job.
    job_lock = BASE / ".sync-state" / (dashboard.key(repository) + ".run.lock")
    try:
        with dashboard.lock(job_lock, timeout=0):
            configured = load_config().get(repository) if CONFIG.exists() else None
            if configured and configured.get("archived", False):
                emit(f"{repository}: sync is archived; invocation skipped")
                return
            if saved:
                if configured is None:
                    emit(f"{repository}: sync is no longer configured; invocation skipped")
                    return
                asset_patterns, last = configured["asset_patterns"], configured["last"]
                branch, source_only = configured.get("branch"), configured.get("source_only", False)
            report = dashboard.Report(BASE, repository, {
                "asset_patterns": asset_patterns, "last": last,
                "branch": branch, "source_only": source_only}, emit)
            progress = Tracker(BASE, repository, emit)
            try:
                report.start()
                report.safe(lambda: dashboard.refresh_metadata(BASE, repository, reference.headers))
                result = _sync(repository, asset_patterns, last, branch, source_only, report, progress)
            except Exception as error:
                progress.finish(error)
                report.finish(error)
                raise
            progress.finish(warnings=bool(report.state.get("warnings")))
            try:
                report.finish()
            except Exception as error:
                progress.finish(error)
                raise
            return result
    except dashboard.LockBusy:
        emit(f"{repository}: sync already running; skipped duplicate invocation")
    except Exception as error:
        raise RuntimeError(f"{repository}: {error}") from error


def _is_transient_fetch_error(message):
    """Retry known transport failures, never an accompanying permanent error."""
    text = message.lower()
    permanent = (
        r"authentication failed|authorization failed|could not read (?:username|password)|terminal prompts disabled",
        r"certificate|permission denied|insufficient permission|access denied|repository not found|not a git repository",
        r"cannot lock|could not lock|unable to (?:create|acquire)|unable to update local ref",
        r"cannot update (?:the )?ref|(?:unable|failed|cannot|could not) (?:to )?(?:write|open)|write error",
        r"no space left on device|disk quota exceeded|input/output error|read-only file system",
        r"invalid refspec|could(?: not|n't) find remote ref|bad object|would be overwritten",
        r"(?:http(?:/[0-9.]+)?\s+|returned error:\s*|response(?: code)?\s+)(?:401|403|404)\b",
    )
    if any(re.search(pattern, text) for pattern in permanent):
        return False
    transient = (
        r"connection (?:was )?(?:aborted|reset|timed out)\b",
        r"operation timed out\b|could not resolve host\b",
        r"unexpected disconnect while reading sideband packet",
        r"http/?2 stream \d+ (?:was not closed cleanly|was reset)",
        r"error in the http/?2 framing layer",
        r"(?:http(?:/[0-9.]+)?\s+|returned error:\s*)(?:500|502|503|504)\b",
    )
    return any(re.search(pattern, text) for pattern in transient)


def _sync(repository, asset_patterns, last, branch=None, source_only=False, report=None, progress=None):
    reference = parse_repository(repository)
    repository = reference.key
    if reference.provider != "github":
        forgejo.detect_repository(reference)
    source = reference.source_path(BASE)
    releases_root = reference.releases_path(BASE)
    download_options = ({"headers": reference.headers,
                         "opener": lambda request, *, timeout: forgejo.urlopen(
                             request, timeout=timeout, allow_cross_host_redirects=True)}
                        if reference.provider != "github" else {})
    source.parent.mkdir(parents=True, exist_ok=True)
    git = git_executable()
    environment = git_environment(git)
    lfs_available = shutil.which("git-lfs", path=environment["PATH"]) is not None

    def run_git(*arguments, lfs_filters=False, allow_no_matches=False, retry_fetch=False):
        command = [str(git), "-c", "http.sslBackend=openssl",
                   "-c", f"safe.directory={source}",
                   "-c", f"core.hooksPath={os.devnull}"]
        if reference.provider != "github":
            command += ["-c", "http.followRedirects=false", "-c", "protocol.allow=never",
                        "-c", "protocol.https.allow=always"]
        if not lfs_filters:
            # Check out LFS pointers first. Hydrate separately, so a missing server
            # object cannot leave clone/merge half-finished or block release sync.
            command += ["-c", "filter.lfs.process=", "-c", "filter.lfs.smudge=",
                        "-c", "filter.lfs.required=false"]
            if not lfs_available:
                command += ["-c", "filter.lfs.clean="]
        command += list(arguments)
        for attempt in range(3 if retry_fetch else 1):
            result = subprocess.run(command, env=environment, text=True, capture_output=True,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if allow_no_matches and result.returncode == 1:
                return ""
            if not result.returncode:
                return result.stdout.strip()
            message = result.stderr.strip() or f"Git failed: {result.returncode}"
            if not retry_fetch or attempt == 2 or not _is_transient_fetch_error(message):
                raise RuntimeError(message)
            delay = (2, 5)[attempt]
            detail = " ".join(message.split())[:240]
            emit(f"{repository}: temporary Git fetch failure; retry {attempt + 2}/3 "
                 f"in {delay}s: {detail}")
            time.sleep(delay)

    initial = not source.exists()
    if initial:
        clone_options = ["--branch", branch, "--single-branch"] if branch else []
        run_git("clone", *clone_options, reference.clone_url, str(source))
    elif not (source / ".git").is_dir():
        raise RuntimeError(f"{source} exists but is not a Git checkout")
    elif reference.provider != "github":
        origin = run_git("-C", str(source), "remote", "get-url", "origin")
        try:
            matches = origin.startswith("https://") and parse_repository(origin).key.casefold() == repository.casefold()
        except ValueError:
            matches = False
        if not matches:
            raise RuntimeError(f"{source} has a different origin URL; check its Git remote before syncing")
    before = run_git("-C", str(source), "rev-parse", "HEAD")
    current_branch = run_git("-C", str(source), "branch", "--show-current")
    if not current_branch:
        raise RuntimeError("Checkout is detached; cannot update source files")
    if branch and current_branch != branch:
        raise RuntimeError(f"{source} is on {current_branch}, expected {branch}; refusing to switch it")
    selected_branch = branch or current_branch
    if branch:
        fetch_args = ["-C", str(source), "fetch", "--force", "origin",
                      f"refs/heads/{branch}:refs/remotes/origin/{branch}"]
        if not source_only:
            fetch_args.append("--tags")
        run_git(*fetch_args, retry_fetch=True)
    else:
        # Release sync tracks tags, including tags that maintainers move.
        run_git("-C", str(source), "fetch", "--force", "origin", "--tags", retry_fetch=True)
    before, after = advance_source(source, selected_branch, run_git)
    if report:
        report.source(before, after, selected_branch, initial, run_git, source)
    if progress:
        progress.stage("lfs", "Downloading Git LFS files")
    lfs_status = sync_lfs(source, run_git, lfs_available)
    if report:
        report.lfs(lfs_status)

    if source_only:
        emit(f"{datetime.now(timezone.utc).isoformat()} {repository} "
             f"branch={selected_branch} source={'updated' if initial or before != after else 'unchanged'} "
             f"{after} lfs={lfs_status} releases=skipped")
        return

    releases_root.mkdir(exist_ok=True)
    if progress:
        progress.stage("releases", "Finding releases and matching assets")
    published = []
    for page in range(1, 100):
        releases = repository_release_page(repository, page)
        if not releases:
            break
        published.extend(release for release in releases
                         if reference.provider != "forgejo" or not release.get("draft", False))
        if reference.provider == "github" and len(releases) < 100:
            break
    else:
        raise RuntimeError("Release pagination limit reached")
    if not published:
        if progress:
            progress.plan(0)
        emit(f"{datetime.now(timezone.utc).isoformat()} {repository} "
             f"source={'updated' if initial or before != after else 'unchanged'} {after} "
             f"releases=0 lfs={lfs_status}; no published releases, retaining existing downloads")
        return
    selected = sorted(published, key=lambda item: item.get("published_at") or "", reverse=True)[:last]
    if progress:
        progress.plan(sum(1 + sum(any(fnmatch.fnmatch(asset["name"].lower(), pattern.lower())
                                     for pattern in asset_patterns) for asset in release["assets"])
                          for release in selected))
    downloaded = 0
    for release in selected:
        folder = releases_root / safe_name(release["tag_name"])
        folder.mkdir(exist_ok=True)
        metadata = folder / "release.json"
        previous_assets = {}
        if metadata.exists():
            old = json.loads(metadata.read_text(encoding="utf-8"))
            previous_assets = {asset["name"]: asset["id"] for asset in old.get("assets", [])}
        for asset in release["assets"]:
            if any(fnmatch.fnmatch(asset["name"].lower(), pattern.lower())
                   for pattern in asset_patterns):
                destination = folder / safe_name(asset["name"])
                if progress:
                    progress.file(release["tag_name"], destination.name, asset["size"])
                changed = download_asset(asset, destination, previous_assets.get(asset["name"]), progress,
                                         **download_options)
                if progress:
                    progress.completed(changed)
                if changed:
                    downloaded += 1
                    if report:
                        report.downloaded(release["tag_name"], destination.name)
        source_zip = folder / f"Source-code-{safe_name(release['tag_name'])}.zip"
        tag_oid = run_git("-C", str(source), "rev-parse",
                          f"refs/tags/{release['tag_name']}^{{commit}}")
        if progress:
            progress.file(release["tag_name"], source_zip.name)
        changed = download_source_archive(release["zipball_url"], source_zip, tag_oid, progress,
                                          **download_options)
        if progress:
            progress.completed(changed)
        if changed:
            downloaded += 1
            if report:
                report.downloaded(release["tag_name"], source_zip.name)
        metadata.write_text(json.dumps(release, indent=2), encoding="utf-8")
    if progress:
        progress.stage("cleanup", "Finishing release retention")
    removed = prune_old_releases(releases_root, {safe_name(item["tag_name"]) for item in selected})
    if report:
        report.removed(removed)
    summary = (f"{datetime.now(timezone.utc).isoformat()} {repository} "
               f"source={'updated' if initial or before != after else 'unchanged'} {after} "
               f"releases={len(selected)} new_files={downloaded} old_releases_removed={removed} "
               f"lfs={lfs_status}")
    emit(summary)


def install_task(repository, asset_patterns, minutes, last, branch=None, source_only=False,
                 start_time=None):
    reference = parse_repository(repository)
    repository = reference.key
    task_name = reference.task_name
    windowless_python = Path(sys.executable).with_name("pythonw.exe")
    if not windowless_python.is_file():
        raise FileNotFoundError(f"Windowless Python is missing: {windowless_python}")
    action = (f'"{windowless_python}" "{Path(__file__).resolve()}" '
              f'run-saved "{repository}"')
    # Task Scheduler rejects 1440 with /SC MINUTE; use a daily trigger.
    schedule = ["/SC", "DAILY", "/MO", "1"] if minutes == 1440 else [
        "/SC", "MINUTE", "/MO", str(minutes)]
    command = ["schtasks.exe", "/Create", "/F", *schedule,
               "/TN", task_name, "/TR", action]
    if start_time is not None:
        command += ["/ST", start_time]
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=15,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Task creation failed")
    emit(f"Installed or updated Windows task: {task_name} (every {minutes} minutes; hidden window)")


def load_config():
    if not CONFIG.is_file():
        raise FileNotFoundError(f"No sync configuration found: {CONFIG}")
    jobs = json.loads(CONFIG.read_text(encoding="utf-8"))
    validate_jobs(jobs)
    return jobs


def validate_jobs(jobs):
    if not isinstance(jobs, dict):
        raise ValueError(f"Invalid sync configuration: {CONFIG}")
    for repository, job in jobs.items():
        patterns = job.get("asset_patterns") if isinstance(job, dict) else None
        last = job.get("last") if isinstance(job, dict) else None
        branch = job.get("branch") if isinstance(job, dict) else None
        source_only = job.get("source_only", False) if isinstance(job, dict) else None
        archived = job.get("archived", False) if isinstance(job, dict) else None
        minutes = job.get("every_minutes") if isinstance(job, dict) else None
        start_time = job.get("start_time") if isinstance(job, dict) else None
        if (not is_repository_key(repository) or not isinstance(patterns, list)
                or not patterns or any(not isinstance(p, str) or not re.fullmatch(
                    r"[A-Za-z0-9.*?_-]+", p) for p in patterns)
                or type(last) is not int or not 1 <= last <= 100
                or (branch is not None and (not isinstance(branch, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch)
                    or ".." in branch or "//" in branch or branch.endswith(("/", ".lock"))))
                or type(source_only) is not bool or type(archived) is not bool
                or (minutes is not None and (type(minutes) is not int or not 5 <= minutes <= 1440))
                or (start_time is not None and (not isinstance(start_time, str)
                    or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start_time)))):
            raise ValueError(f"Invalid sync configuration for {repository!r}")
    return jobs


def resolve_run_branch(repository, branch):
    # Explicit, user-approved migrations let existing Windows task commands
    # keep working even when Task Scheduler cannot be edited in this session.
    if branch is None or not CONFIG.exists():
        return branch
    job = load_config().get(repository, {})
    redirects = job.get("branch_redirects", {})
    if not isinstance(redirects, dict):
        raise ValueError(f"Invalid branch redirects for {repository}")
    target = redirects.get(branch)
    if target is None:
        return branch
    if not isinstance(target, str) or not target or target != job.get("branch"):
        raise ValueError(f"Branch redirect for {repository} must match its configured branch")
    emit(f"{repository}: using approved branch migration {branch} -> {target}")
    return target


def save_config(repository, asset_patterns, last, branch=None, source_only=False,
                every_minutes=None, start_time=None):
    with dashboard.lock(BASE / ".sync-state" / (dashboard.key(repository) + ".run.lock"), timeout=0):
        with dashboard.lock(BASE / ".sync-state" / "config.lock", timeout=0):
            jobs = load_config() if CONFIG.exists() else {}
            job = dict(jobs.get(repository, {}))
            job.update(asset_patterns=asset_patterns, last=last, branch=branch,
                       source_only=source_only, archived=False)
            if every_minutes is not None:
                job.update(every_minutes=every_minutes, start_time=start_time)
            redirects = job.get("branch_redirects", {})
            if isinstance(redirects, dict):
                job["branch_redirects"] = {old: new for old, new in redirects.items() if new == branch}
            jobs[repository] = job
            validate_jobs(jobs)
            dashboard.atomic_write(CONFIG, json.dumps(jobs, indent=2) + "\n")
    try:
        dashboard.render(BASE)
    except Exception as error:
        emit(f"WARNING: dashboard could not update: {error}")


def run_all():
    jobs = load_config()
    if not jobs:
        raise RuntimeError(f"No repositories configured in {CONFIG}")
    failures = []
    for repository, job in jobs.items():
        if job.get("archived", False):
            continue
        try:
            sync(repository, job["asset_patterns"], job["last"],
                 job.get("branch"), job.get("source_only", False), saved=True)
        except Exception as error:
            emit(f"{repository} failed: {error}", error=True)
            failures.append(repository)
    if failures:
        raise RuntimeError("Sync failed for: " + ", ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run-all", help="Sync every repository in sync-config.json now")
    page = subparsers.add_parser("dashboard", help="Rebuild the local dashboard without syncing")
    page.add_argument("--open", action="store_true", help="Open the page in your default browser")
    page.add_argument("--serve", action="store_true", help="Run the local dashboard management server")
    saved = subparsers.add_parser("run-saved", help="Sync one repository using its saved settings")
    saved.add_argument("repository", help="GitHub owner/repository or a supported repository URL")
    for name in ("run", "setup", "schedule"):
        sub = subparsers.add_parser(name)
        sub.add_argument("repository", help="GitHub owner/repository or a supported repository URL")
        sub.add_argument("--asset-pattern", action="append", dest="asset_patterns",
                         help="Release filename or wildcard; repeat for multiple files")
        sub.add_argument("--last", type=int, default=5,
                         help="Keep only the latest N published releases (default: 5)")
        sub.add_argument("--branch", help="Track this branch instead of the default branch")
        sub.add_argument("--source-only", action="store_true",
                         help="Sync the branch without requiring published releases")
        if name in ("setup", "schedule"):
            sub.add_argument("--every-minutes", type=int, default=15)
            sub.add_argument("--start-time", help="Local Windows start time, HH:MM")
    args = parser.parse_args()
    if args.command == "dashboard":
        if args.serve or args.open:
            import github_dashboard_server
            if args.serve:
                github_dashboard_server.serve(BASE)
            else:
                github_dashboard_server.open_dashboard(BASE)
            return
        destination = dashboard.render(BASE)
        emit(f"Dashboard updated: {destination}")
        return
    if args.command == "run-all":
        run_all()
        return
    try:
        args.repository = parse_repository(args.repository).key
    except ValueError as error:
        parser.error(str(error))
    if args.command == "run-saved":
        sync(args.repository, [], 1, saved=True)
        return
    if args.command == "run":
        args.branch = resolve_run_branch(args.repository, args.branch)
    if args.branch:
        git = git_executable()
        valid_branch = subprocess.run([str(git), "check-ref-format", "--branch", args.branch],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if valid_branch.returncode:
            parser.error("invalid git branch name")
    asset_patterns = args.asset_patterns or ["*windows*x64*.zip"]
    if args.source_only and args.asset_patterns:
        emit("Note: --source-only skips releases; --asset-pattern is ignored.")
    if any(not re.fullmatch(r"[A-Za-z0-9.*?_-]+", pattern) for pattern in asset_patterns):
        parser.error("asset patterns may contain letters, numbers, dots, dashes, underscores, * and ?")
    if args.command in ("setup", "schedule") and not 5 <= args.every_minutes <= 1440:
        parser.error("every-minutes must be between 5 and 1440")
    if not 1 <= args.last <= 100:
        parser.error("last must be between 1 and 100")
    if args.command == "setup" and CONFIG.exists() and load_config().get(args.repository, {}).get("archived", False):
        parser.error("This sync is archived. Resume it on the dashboard, or use schedule before running it.")
    if args.command != "schedule":
        sync(args.repository, asset_patterns, args.last, args.branch, args.source_only)
    if args.command in ("setup", "schedule"):
        from github_management import configure_sync
        configure_sync(args.repository, asset_patterns, args.every_minutes, args.last,
                       args.branch, args.source_only, args.start_time)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        emit(f"GitHub sync failed: {error}", error=True)
        sys.exit(1)
