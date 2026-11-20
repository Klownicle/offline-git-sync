"""Local sync settings, Windows task lifecycle, and guarded release cleanup."""

from contextlib import contextmanager
import csv
from datetime import datetime
import fnmatch
import hashlib
import io
import json
import locale
import ntpath
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

import github_dashboard as dashboard
from github_sources import parse_repository
import github_forgejo as forgejo


_schedule_error_cache = None
_UNSET = object()


def _sync():
    import github_sync
    return github_sync


def _repository(repository):
    return parse_repository(repository, allow_url=False).key.split("/")


def validate_settings(every_minutes, last, start_time):
    if type(every_minutes) is not int or not 5 <= every_minutes <= 1440:
        raise ValueError("Sync interval must be 5–1440 minutes")
    if type(last) is not int or not 1 <= last <= 100:
        raise ValueError("Release count must be 1–100")
    if start_time is not None and (not isinstance(start_time, str)
            or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start_time)):
        raise ValueError("Start time must be HH:MM in local Windows time")


def normalize_repository(repository):
    """Normalize an approved host's URL or an existing canonical repository key."""
    return parse_repository(repository).key


def validate_asset_patterns(asset_patterns):
    if not isinstance(asset_patterns, list) or not 1 <= len(asset_patterns) <= 50:
        raise ValueError("Provide 1–50 asset patterns")
    patterns, seen = [], set()
    for value in asset_patterns:
        if not isinstance(value, str):
            raise ValueError("Each asset pattern must be text")
        pattern = value.strip()
        if len(pattern) > 200 or not re.fullmatch(r"[A-Za-z0-9.*?_-]+", pattern):
            raise ValueError("Asset patterns may use letters, numbers, dots, hyphens, underscores, * and ?")
        if pattern.casefold() not in seen:
            patterns.append(pattern)
            seen.add(pattern.casefold())
    return patterns


def _validate_branch(branch):
    if branch is None:
        return
    if (not isinstance(branch, str) or branch == "HEAD" or len(branch) > 240
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch)
            or ".." in branch or "//" in branch or branch.endswith(("/", "."))
            or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))):
        raise ValueError("Branch must be a valid Git branch name, or left blank for the default branch")


def _safe_name(value):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    if not name or name in {".", ".."}:
        raise ValueError("Unsafe release tag")
    return name


def _no_links(path):
    """Check every existing ancestor, including junctions and other reparse points."""
    path = Path(os.path.abspath(path))
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Refusing linked or reparse path: {part}")
    return path


def _tree_stamp(folder):
    """Do not follow links; include all files so a changed preview is rejected."""
    _no_links(folder)
    entries = []
    pending = [folder]
    while pending:
        current = pending.pop()
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Linked or reparse entry: {current.name}")
        entries.append((str(current.relative_to(folder)), info.st_mode,
                        info.st_size, info.st_mtime_ns, info.st_ino))
        if stat.S_ISDIR(info.st_mode):
            pending.extend(current.iterdir())
        elif not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Unsupported file type: {current.name}")
    return sorted(entries)


def _inventory(base, repository):
    return _inventory_root(parse_repository(repository, allow_url=False).releases_path(base))


def _inventory_root(root):
    """Inventory an explicit release root, including host-prefixed repositories."""
    root = Path(root)
    releases, skipped = [], []
    try:
        _no_links(root)
        if not root.exists():
            return {"count": 0, "releases": [], "skipped": []}, []
        resolved_root = root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Release root is not a directory")
        children = list(root.iterdir())
    except (OSError, ValueError) as error:
        return {"count": 0, "releases": [], "skipped": [
            {"folder": str(root), "reason": str(error)}], "error": str(error)}, []
    revisions = []
    for folder in children:
        try:
            _no_links(folder)
            if not folder.is_dir():
                continue
            if folder.resolve(strict=True).parent != resolved_root:
                raise ValueError("Release folder escaped its root")
            metadata = folder / "release.json"
            _no_links(metadata)
            if not metadata.is_file():
                raise ValueError("No managed release.json")
            raw = metadata.read_bytes()
            release = json.loads(raw.decode("utf-8"))
            tag = release.get("tag_name") if isinstance(release, dict) else None
            published = release.get("published_at") if isinstance(release, dict) else None
            if not isinstance(tag, str) or _safe_name(tag) != folder.name:
                raise ValueError("Release tag does not match its folder")
            if not isinstance(published, str):
                raise ValueError("Release has no publication timestamp")
            parsed = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("Release publication timestamp has no time zone")
            stamp = _tree_stamp(folder)
            item = {"tag": tag, "folder": folder.name, "published_at": published}
            releases.append((parsed.timestamp(), item))
            revisions.append((folder.name, hashlib.sha256(raw).hexdigest(), stamp))
        except (OSError, ValueError, TypeError) as error:
            skipped.append({"folder": folder.name, "reason": str(error)})
    releases.sort(key=lambda entry: (entry[0], entry[1]["folder"]))
    return {"count": len(releases), "releases": [item for _, item in releases],
            "skipped": sorted(skipped, key=lambda item: item["folder"])}, sorted(revisions)


def inventory(base, repository):
    """All recognized local releases, oldest first, independent of download limit."""
    return _inventory(base, repository)[0]


def task_name(repository):
    return parse_repository(repository, allow_url=False).task_name


def _task_command(*arguments):
    try:
        result = subprocess.run(["schtasks.exe", *arguments], capture_output=True, timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        def decode(value):
            if isinstance(value, str):
                return value
            if value.startswith((b"\xff\xfe", b"\xfe\xff")):
                return value.decode("utf-16")
            if value and value.count(b"\0") > len(value) // 4:
                return value.decode("utf-16-le")
            return value.decode("utf-8-sig" if value.startswith(b"\xef\xbb\xbf")
                                else locale.getpreferredencoding(False), errors="replace")
        result.stdout, result.stderr = decode(result.stdout), decode(result.stderr)
        return result
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Windows Task Scheduler is unavailable: {error}") from error


def _task_xml(repository, *, read_cache=False):
    """Return XML, or None only after a successful listing confirms absence."""
    name = task_name(repository)
    global _schedule_error_cache
    if read_cache and _schedule_error_cache is not None:
        checked, error = _schedule_error_cache
        if time.monotonic() - checked < 60:
            raise RuntimeError(error)
    try:
        result = _task_command("/Query", "/TN", name, "/XML")
    except RuntimeError as error:
        if read_cache:
            _schedule_error_cache = (time.monotonic(), str(error))
        raise
    if result.returncode == 0:
        try:
            root = ET.fromstring(result.stdout.lstrip("\ufeff"))
        except ET.ParseError as error:
            raise RuntimeError("Task Scheduler returned invalid task XML") from error
        if root.tag.rsplit("}", 1)[-1] != "Task":
            raise RuntimeError("Task Scheduler returned unexpected task XML")
        return result.stdout
    listing = _task_command("/Query", "/FO", "CSV", "/NH")
    if listing.returncode == 0:
        rows = list(csv.reader(io.StringIO(listing.stdout)))
        # A localized error or status message is not proof that a task is absent.
        if rows and all(len(row) >= 3 and row[0].startswith("\\") for row in rows):
            names = {row[0].lstrip("\\").casefold() for row in rows}
            detail = (result.stderr.strip() or result.stdout.strip()).casefold()
            # Access denied and generic missing-path errors can conceal an existing
            # task. Only the specific missing-file response plus enumeration is proof.
            missing_file = "cannot find the file specified" in detail
            if missing_file and name.casefold() not in names:
                return None
    detail = result.stderr.strip() or result.stdout.strip() or "query failed"
    if read_cache and listing.returncode:
        message = listing.stderr.strip() or listing.stdout.strip() or detail
        _schedule_error_cache = (time.monotonic(), f"Cannot query Windows Task Scheduler: {message}")
    raise RuntimeError(f"Cannot verify scheduled task {name}: {detail}")


def _xml_schedule(xml):
    root = ET.fromstring(xml.lstrip("\ufeff"))
    namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    triggers = root.find("t:Triggers", namespace)
    if triggers is None or len(triggers) != 1:
        raise ValueError("Task has an unsupported or ambiguous schedule")
    trigger = triggers[0]
    interval = trigger.findtext("t:Repetition/t:Interval", namespaces=namespace)
    if interval:
        match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", interval)
        if not match or int(match[4] or 0):
            raise ValueError("Task repetition is not a whole number of minutes")
        minutes = int(match[1] or 0) * 1440 + int(match[2] or 0) * 60 + int(match[3] or 0)
    elif trigger.findtext("t:ScheduleByDay/t:DaysInterval", namespaces=namespace) == "1":
        minutes = 1440
    else:
        raise ValueError("Task schedule is not a supported repeating or daily schedule")
    boundary = trigger.findtext("t:StartBoundary", namespaces=namespace)
    start_time = datetime.fromisoformat(boundary.replace("Z", "+00:00")).strftime("%H:%M") if boundary else None
    validate_settings(minutes, 1, start_time)
    return {"every_minutes": minutes, "start_time": start_time}


def get_schedule(repository, job):
    if job.get("every_minutes") is not None:
        try:
            validate_settings(job["every_minutes"], job["last"], job.get("start_time"))
            return {"every_minutes": job["every_minutes"], "start_time": job.get("start_time")}
        except (ValueError, KeyError) as error:
            return {"every_minutes": None, "start_time": None, "error": str(error)}
    try:
        xml = _task_xml(repository, read_cache=True)
        if xml is None:
            raise ValueError("No scheduled task was found; choose an interval and start time")
        return _xml_schedule(xml)
    except (RuntimeError, ValueError, ET.ParseError) as error:
        return {"every_minutes": None, "start_time": None, "error": str(error)}


@contextmanager
def _locked(repository):
    sync = _sync()
    _repository(repository)
    with dashboard.lock(sync.BASE / ".sync-state" / (dashboard.key(repository) + ".run.lock"), timeout=0):
        with dashboard.lock(sync.BASE / ".sync-state" / "config.lock", timeout=0):
            yield sync


def _write_config(sync, jobs):
    sync.validate_jobs(jobs)
    dashboard.atomic_write(sync.CONFIG, json.dumps(jobs, indent=2) + "\n")


def _delete_task(repository):
    result = _task_command("/Delete", "/TN", task_name(repository), "/F")
    if result.returncode:
        # A racing external deletion is okay only when independently verified.
        if _task_xml(repository) is not None:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Task removal failed")


def _restore_task(repository, xml):
    if xml is None:
        if _task_xml(repository) is not None:
            _delete_task(repository)
        return
    sync = _sync()
    with tempfile.TemporaryDirectory(prefix="task-restore-", dir=sync.BASE / ".sync-state") as temporary:
        path = Path(temporary) / "task.xml"
        path.write_text(xml, encoding="utf-16")
        result = _task_command("/Create", "/F", "/TN", task_name(repository), "/XML", str(path))
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Task restoration failed")


def _commit_scheduler(sync, repository, jobs, operation):
    old_task = _task_xml(repository)
    operation(old_task)
    try:
        _write_config(sync, jobs)
    except Exception as error:
        try:
            _restore_task(repository, old_task)
        except Exception as rollback_error:
            raise RuntimeError(f"Settings could not be saved ({error}); task restoration also failed "
                               f"({rollback_error}). Check Windows Task Scheduler before retrying.") from error
        raise


def _render(sync):
    try:
        dashboard.render(sync.BASE)
    except Exception as error:
        try:
            sync.emit(f"WARNING: dashboard could not update: {error}")
        except Exception:
            # A committed settings update remains successful if refresh/logging
            # fails; the next dashboard request can regenerate the view.
            pass


def _install(sync, repository, job):
    sync.install_task(repository, job["asset_patterns"], job["every_minutes"], job["last"],
                      job.get("branch"), job.get("source_only", False), job.get("start_time"))


def _saved_command(sync, repository):
    python = Path(sync.sys.executable).with_name("pythonw.exe")
    if not python.is_file():
        raise FileNotFoundError(f"Windowless Python is missing: {python}")
    return [str(python), str(Path(sync.__file__).resolve()), "run-saved", repository]


def _install_new(sync, repository, job):
    """Never use /F for an add: even an external task-creation race must not overwrite."""
    schedule = ["/SC", "DAILY", "/MO", "1"] if job["every_minutes"] == 1440 else [
        "/SC", "MINUTE", "/MO", str(job["every_minutes"])]
    arguments = ["/Create", *schedule, "/TN", task_name(repository), "/TR",
                 subprocess.list2cmdline(_saved_command(sync, repository))]
    if job.get("start_time") is not None:
        arguments += ["/ST", job["start_time"]]
    result = _task_command(*arguments)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Task creation failed")


def _launch_saved(sync, repository):
    return subprocess.Popen(_saved_command(sync, repository), cwd=str(sync.BASE),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _split_windows_arguments(command):
    """Parse the Windows C-runtime quoting used by Python task commands."""
    arguments, index = [], 0
    while index < len(command):
        while index < len(command) and command[index] in " \t":
            index += 1
        if index == len(command):
            break
        argument, quoted = [], False
        while index < len(command) and (quoted or command[index] not in " \t"):
            if command[index] == "\\":
                end = index
                while end < len(command) and command[end] == "\\":
                    end += 1
                count = end - index
                if end < len(command) and command[end] == '"':
                    argument.extend("\\" * (count // 2))
                    index = end
                    if count % 2:
                        argument.append('"')
                        index += 1
                        continue
                else:
                    argument.extend("\\" * count)
                    index = end
                    continue
            if command[index] == '"':
                if quoted and index + 1 < len(command) and command[index + 1] == '"':
                    argument.append('"')
                    index += 2
                    continue
                quoted = not quoted
            else:
                argument.append(command[index])
            index += 1
        if quoted:
            raise ValueError("Scheduled action has an unterminated quote")
        arguments.append("".join(argument))
    return arguments


def _action_ownership_issue(sync, repository, executable, arguments):
    """Recognize our executable and argv without trusting the task's name alone."""
    def normalized(path):
        return ntpath.normcase(ntpath.normpath(path))
    # schtasks /TR accepts a quoted executable, and Task Scheduler can retain
    # those surrounding quotes in the XML Command element. Command is one path,
    # not an argv string: never accept appended flags or strip arbitrary quotes.
    executable = executable.strip()
    if executable.startswith('"') and executable.endswith('"'):
        executable = executable[1:-1]
    if not executable or '"' in executable or executable.endswith(("\\", "/")):
        return "The program path has malformed quotes or contains extra arguments."
    allowed_python = {normalized(str(Path(sync.sys.executable).with_name(name)))
                      for name in ("python.exe", "pythonw.exe")}
    if normalized(executable) not in allowed_python:
        return "The program is not this tool's Python or Pythonw executable."
    try:
        words = _split_windows_arguments(arguments)
    except ValueError:
        return "The task arguments contain an unterminated quote."
    if len(words) < 3:
        return "The task arguments do not identify a sync script, command, and repository."
    if (not ntpath.isabs(words[0])
            or normalized(words[0]) != normalized(str(Path(sync.__file__).resolve()))):
        return "The task does not launch this tool's github_sync.py script."
    if words[2].casefold() != repository.casefold():
        return "The task arguments name a different repository."
    if words[1] == "run-saved":
        return None if len(words) == 3 else "The run-saved command contains unexpected extra arguments."
    if words[1] != "run":
        return "The task is not a run or run-saved sync command."
    index = 3
    while index < len(words):
        option = words[index]
        if option == "--source-only":
            index += 1
        elif option in {"--asset-pattern", "--last", "--branch"} and index + 1 < len(words):
            # These are literal argv values, never shell commands. Unknown flags
            # and extra positional arguments cannot be a tool-owned action.
            index += 2
        else:
            return "The legacy run command has an unsupported option or incomplete arguments."
    return None


def _owns_action(sync, repository, executable, arguments):
    return _action_ownership_issue(sync, repository, executable, arguments) is None


class TaskActionConflict(ValueError):
    """An actionable, read-only diagnostic; the task and config remain unchanged."""
    def __init__(self, diagnostics, message=None):
        self.diagnostics = diagnostics
        super().__init__(message or "Scheduled task action is not uniquely owned by this sync; settings were not changed. "
                         "Open Scheduled task details below to review the mismatch. "
                         "Your unsaved patterns remain in the form.")


def _saved_action_xml(sync, repository, xml):
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    root = ET.fromstring(xml.lstrip("\ufeff"), parser=parser)
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    tag = lambda name: f"{{{namespace}}}{name}"
    actions = root.find(tag("Actions"))
    owned, details = [], []
    for action in [] if actions is None else actions:
        if not isinstance(action.tag, str):
            continue  # XML comments are not scheduled actions.
        executable = action.findtext(tag("Command"), "")
        arguments = action.findtext(tag("Arguments"), "")
        issue = (_action_ownership_issue(sync, repository, executable, arguments)
                 if action.tag == tag("Exec") else "This action is not a program execution action.")
        if issue is None:
            owned.append(action)
        details.append({"command": executable, "arguments": arguments,
                        "working_directory": action.findtext(tag("WorkingDirectory"), ""),
                        "reason": issue or "Matches this sync."})
    if len(owned) != 1:
        if len(owned) > 1:
            for detail in details:
                if detail["reason"] == "Matches this sync.":
                    detail["reason"] = "More than one action matches this sync; the intended action is ambiguous."
        raise TaskActionConflict({
            "task_name": task_name(repository), "repository": repository,
            "expected": {"command": str(Path(sync.sys.executable).with_name("pythonw.exe")),
                         "arguments": subprocess.list2cmdline([str(Path(sync.__file__).resolve()),
                                                               "run-saved", repository])},
            "actions": details,
            "help": "No task or settings were changed. Review this task's Actions tab in Windows Task Scheduler. "
                    "The dashboard can update one verified action while preserving the schedule. "
                    "Copy these details for troubleshooting if the intended action is unclear; "
                    "do not delete or recreate the sync to retry saving patterns."})
    command = _saved_command(sync, repository)
    action = owned[0]
    action.find(tag("Command")).text = command[0]
    arguments = action.find(tag("Arguments"))
    if arguments is None:
        arguments = ET.SubElement(action, tag("Arguments"))
    arguments.text = subprocess.list2cmdline(command[1:])
    return ET.tostring(root, encoding="unicode")


def _validate_add_paths(sync, jobs, repository):
    descriptor = parse_repository(repository, allow_url=False)
    candidates = {repository.casefold(), (repository + "-releases").casefold()}
    for existing in jobs:
        if candidates & {existing.casefold(), (existing + "-releases").casefold()}:
            raise ValueError(f"Local folders would overlap the existing sync {existing}")
    source = _no_links(descriptor.source_path(sync.BASE))
    releases = _no_links(descriptor.releases_path(sync.BASE))
    if source.exists() and (not source.is_dir() or not _no_links(source / ".git").is_dir()):
        raise ValueError("The local source folder already exists and is not a Git checkout")
    if releases.exists() and not releases.is_dir():
        raise ValueError("The local releases path already exists and is not a directory")


def _release_limits(job, last, separate_release_limits=_UNSET, stable_last=_UNSET,
                    prerelease_last=_UNSET):
    from github_releases import resolve_release_limits
    if separate_release_limits is not _UNSET and type(separate_release_limits) is not bool:
        raise ValueError("Separate release limits must be true or false")
    for value in (stable_last, prerelease_last):
        if value is not _UNSET and (type(value) is not int or not 1 <= value <= 100):
            raise ValueError("Stable and prerelease counts must be 1–100")
    return resolve_release_limits(job, last=last,
        separate_release_limits=None if separate_release_limits is _UNSET else separate_release_limits,
        stable_last=None if stable_last is _UNSET else stable_last,
        prerelease_last=None if prerelease_last is _UNSET else prerelease_last)


def _clear_deleted(sync, repository):
    try:
        sync.clear_deleted(repository)
    except Exception as error:
        warning = f"Sync saved, but its previous deletion marker could not be cleared: {error}"
        try:
            sync.emit("WARNING: " + warning)
        except Exception:
            pass
        return warning
    return None


def _prepare_add(repository, asset_patterns, every_minutes, last, branch=None,
                 source_only=False, start_time=None, run_now=True, *, include_prereleases=_UNSET,
                 separate_release_limits=_UNSET, stable_last=_UNSET, prerelease_last=_UNSET,
                 release_policy=_UNSET, detect=True):
    from github_releases import normalize_release_policy, resolve_release_policy
    repository = normalize_repository(repository)
    patterns = validate_asset_patterns(asset_patterns)
    validate_settings(every_minutes, last, start_time)
    _validate_branch(branch)
    if type(source_only) is not bool or type(run_now) is not bool:
        raise ValueError("Source-only and run-now options must be true or false")
    legacy = any(value is not _UNSET for value in (
        include_prereleases, separate_release_limits, stable_last, prerelease_last))
    include_prereleases = False if include_prereleases is _UNSET else include_prereleases
    if type(include_prereleases) is not bool:
        raise ValueError("Include prereleases must be true or false")
    limits = _release_limits(None, last, separate_release_limits, stable_last, prerelease_last)
    policy = resolve_release_policy(legacy=legacy) if release_policy is _UNSET else normalize_release_policy(release_policy)
    reference = parse_repository(repository, allow_url=False)
    if detect and reference.provider != "github":
        forgejo.detect_repository(reference)
    job = {"asset_patterns": patterns, "every_minutes": every_minutes, "last": last,
           "branch": branch, "source_only": source_only, "start_time": start_time,
           "archived": False, "include_prereleases": include_prereleases, **limits}
    if policy is not None:
        job["release_policy"] = policy
    return repository, job


def _validate_add(sync, jobs, repository, job):
    for existing in jobs:
        if existing.casefold() == repository.casefold():
            raise ValueError("This repository already has a sync, including archived syncs")
        if task_name(existing).casefold() == task_name(repository).casefold():
            raise ValueError(f"Scheduled task name conflicts with the existing sync {existing}")
    _validate_add_paths(sync, jobs, repository)
    sync.validate_jobs({**jobs, repository: job})


def preview_add_sync(repository, asset_patterns, every_minutes, last, branch=None,
                     source_only=False, start_time=None, run_now=True, *, include_prereleases=_UNSET,
                     separate_release_limits=_UNSET, stable_last=_UNSET, prerelease_last=_UNSET,
                     release_policy=_UNSET):
    """Read-only setup synopsis. Final Add checks everything again under its locks."""
    repository, job = _prepare_add(repository, asset_patterns, every_minutes, last, branch,
        source_only, start_time, run_now, include_prereleases=include_prereleases,
        separate_release_limits=separate_release_limits, stable_last=stable_last,
        prerelease_last=prerelease_last, release_policy=release_policy)
    sync = _sync()
    jobs = sync.load_config() if sync.CONFIG.exists() else {}
    _validate_add(sync, jobs, repository, job)
    if _task_xml(repository) is not None:
        raise ValueError("A scheduled task with this name already exists; it was not overwritten")
    # Do not acquire mutation locks: even creating lock files would write to disk.
    from github_asset_preview import preview as preview_assets
    assets = preview_assets(repository, last, False,
        include_prereleases=job["include_prereleases"],
        separate_release_limits=job["separate_release_limits"],
        stable_last=job["stable_last"], prerelease_last=job["prerelease_last"],
        release_policy=job.get("release_policy"), types_only=source_only)
    if source_only:
        assets.update(source_only=True,
            message="Source-only syncs update the source checkout without downloading release assets or source ZIPs.")
    reference = parse_repository(repository, allow_url=False)
    source, releases = reference.source_path(sync.BASE), reference.releases_path(sync.BASE)
    return {"repository": repository, "job": job, "run_now": run_now,
            "source_path": str(source), "releases_path": str(releases),
            "source_exists": source.exists(), "releases_exist": releases.exists(),
            "task_name": task_name(repository), "assets": assets}


def add_sync(repository, asset_patterns, every_minutes, last, branch=None,
             source_only=False, start_time=None, run_now=True, *, include_prereleases=_UNSET,
             separate_release_limits=_UNSET, stable_last=_UNSET, prerelease_last=_UNSET,
             release_policy=_UNSET, _catalog_descriptors=_UNSET):
    repository, job = _prepare_add(repository, asset_patterns, every_minutes, last, branch,
        source_only, start_time, run_now, include_prereleases=include_prereleases,
        separate_release_limits=separate_release_limits, stable_last=stable_last,
        prerelease_last=prerelease_last, release_policy=release_policy)
    progress_id, progress_warning, catalog_warning, marker_warning = None, None, None, None
    from github_progress import queue, clear, launched, launch_failed, snapshot as progress_snapshot
    with _locked(repository) as sync:
        jobs = sync.load_config() if sync.CONFIG.exists() else {}
        _validate_add(sync, jobs, repository, job)
        # A newly created lifecycle must not accept a Delete confirmation from
        # a previous sync with otherwise identical settings and local folders.
        # Assign only here, after the read-only Add review has been validated.
        job["sync_generation"] = uuid.uuid4().hex
        jobs[repository] = job
        def install(old):
            if old is not None:
                raise ValueError("A scheduled task with this name already exists; it was not overwritten")
            _install_new(sync, repository, job)
        _commit_scheduler(sync, repository, jobs, install)
        marker_warning = _clear_deleted(sync, repository)
        if _catalog_descriptors is not _UNSET:
            try:
                from github_release_catalog import update
                # Save the reviewed metadata before the first worker can discover
                # fresher types; this cache failure cannot undo a committed Add.
                update(sync.BASE, repository, _catalog_descriptors)
            except Exception as error:
                catalog_warning = f"Sync saved, but its release-type catalog could not be saved: {error}"
        try:
            if run_now:
                progress_id = queue(sync.BASE, repository)
            else:
                clear(sync.BASE, repository)
        except Exception as error:
            progress_warning = f"First-sync progress could not be prepared: {error}"
    result = {"repository": repository, "job": job, "started": False}
    warnings = [message for message in (marker_warning, catalog_warning, progress_warning) if message]
    if run_now:
        try:
            process = _launch_saved(sync, repository)
            result["started"] = True
        except Exception as error:
            warnings.append(f"Sync saved, but the first run could not start: {error}")
            if progress_id:
                try:
                    launch_failed(sync.BASE, repository, progress_id, error)
                except Exception as progress_error:
                    warnings.append(f"First-sync progress could not update: {progress_error}")
        else:
            if progress_id and type(getattr(process, "pid", None)) is int:
                try:
                    launched(sync.BASE, repository, progress_id, process.pid)
                except Exception as error:
                    warnings.append(f"First-sync progress could not update: {error}")
    try:
        result["first_sync"] = progress_snapshot(sync.BASE, repository) if run_now else None
    except Exception as error:
        result["first_sync"] = None
        warnings.append(f"First-sync progress could not be read: {error}")
    if warnings:
        result["warning"] = " ".join(warnings)
    _render(sync)
    return result


def update_asset_patterns(repository, asset_patterns):
    patterns = validate_asset_patterns(asset_patterns)
    with _locked(repository) as sync:
        jobs = sync.load_config()
        job = _job(jobs, repository)
        job["asset_patterns"] = patterns
        jobs[repository] = job
        sync.validate_jobs(jobs)
        if job.get("archived", False):
            _write_config(sync, jobs)
        else:
            def update_action(old):
                if old is not None:
                    _restore_task(repository, _saved_action_xml(sync, repository, old))
            _commit_scheduler(sync, repository, jobs, update_action)
    _render(sync)
    return job


def _job(jobs, repository):
    if repository not in jobs:
        raise ValueError("Sync is no longer configured; refresh the dashboard")
    return dict(jobs[repository])


def update_settings(repository, every_minutes, last, start_time=None, *, include_prereleases=_UNSET,
                    separate_release_limits=_UNSET, stable_last=_UNSET, prerelease_last=_UNSET,
                    release_policy=_UNSET):
    validate_settings(every_minutes, last, start_time)
    if include_prereleases is not _UNSET and type(include_prereleases) is not bool:
        raise ValueError("Include prereleases must be true or false")
    if release_policy is not _UNSET:
        from github_releases import normalize_release_policy
        release_policy = normalize_release_policy(release_policy)
    with _locked(repository) as sync:
        jobs = sync.load_config()
        job = _job(jobs, repository)
        limits = _release_limits(job, last, separate_release_limits, stable_last, prerelease_last)
        job.update(every_minutes=every_minutes, last=last, start_time=start_time)
        job.update(limits)
        if include_prereleases is not _UNSET:
            job["include_prereleases"] = include_prereleases
        if release_policy is not _UNSET:
            job["release_policy"] = release_policy
        elif any(value is not _UNSET for value in (
                include_prereleases, separate_release_limits, stable_last, prerelease_last)):
            job.pop("release_policy", None)
        jobs[repository] = job
        if job.get("archived", False):
            _write_config(sync, jobs)
        else:
            _commit_scheduler(sync, repository, jobs, lambda old: _install(sync, repository, job))
    _render(sync)
    return job


def archive_sync(repository):
    with _locked(repository) as sync:
        jobs = sync.load_config()
        job = _job(jobs, repository)
        if job.get("every_minutes") is None:
            schedule = get_schedule(repository, job)
            if not schedule.get("error"):
                job.update(schedule)
        job["archived"] = True
        jobs[repository] = job
        _commit_scheduler(sync, repository, jobs, lambda old: _delete_task(repository) if old is not None else None)
    _render(sync)
    return job


def resume_sync(repository, every_minutes=None, last=None, start_time=_UNSET, *, include_prereleases=_UNSET,
                separate_release_limits=_UNSET, stable_last=_UNSET, prerelease_last=_UNSET,
                release_policy=_UNSET):
    if include_prereleases is not _UNSET and type(include_prereleases) is not bool:
        raise ValueError("Include prereleases must be true or false")
    if release_policy is not _UNSET:
        from github_releases import normalize_release_policy
        release_policy = normalize_release_policy(release_policy)
    with _locked(repository) as sync:
        jobs = sync.load_config()
        job = _job(jobs, repository)
        minutes = every_minutes if every_minutes is not None else job.get("every_minutes")
        release_count = last if last is not None else job["last"]
        clock_time = job.get("start_time") if start_time is _UNSET else start_time
        validate_settings(minutes, release_count, clock_time)
        limits = _release_limits(job, release_count, separate_release_limits, stable_last, prerelease_last)
        job.update(every_minutes=minutes, last=release_count, start_time=clock_time, archived=False)
        job.update(limits)
        if include_prereleases is not _UNSET:
            job["include_prereleases"] = include_prereleases
        if release_policy is not _UNSET:
            job["release_policy"] = release_policy
        elif any(value is not _UNSET for value in (
                include_prereleases, separate_release_limits, stable_last, prerelease_last)):
            job.pop("release_policy", None)
        jobs[repository] = job
        _commit_scheduler(sync, repository, jobs, lambda old: _install(sync, repository, job))
    _render(sync)
    return job


def remove_sync(repository):
    from github_delete import remove_preserving_files
    return remove_preserving_files(repository)


def configure_sync(repository, asset_patterns, every_minutes, last, branch=None,
                   source_only=False, start_time=None, *, include_prereleases=None,
                   separate_release_limits=None, stable_last=None, prerelease_last=None,
                   release_policy=None):
    """CLI setup/schedule share the same locks and transactional task updates."""
    repository = normalize_repository(repository)
    validate_settings(every_minutes, last, start_time)
    if include_prereleases is not None and type(include_prereleases) is not bool:
        raise ValueError("Include prereleases must be true or false")
    reference = parse_repository(repository, allow_url=False)
    if reference.provider != "github":
        forgejo.detect_repository(reference)
    with _locked(repository) as sync:
        jobs = sync.load_config() if sync.CONFIG.exists() else {}
        other_jobs = {key: value for key, value in jobs.items() if key != repository}
        for existing in other_jobs:
            if existing.casefold() == repository.casefold():
                raise ValueError("This repository is already configured with different capitalization")
            if task_name(existing).casefold() == task_name(repository).casefold():
                raise ValueError(f"Scheduled task name conflicts with the existing sync {existing}")
        _validate_add_paths(sync, other_jobs, repository)
        job = dict(jobs.get(repository, {}))
        if repository not in jobs:
            job["sync_generation"] = uuid.uuid4().hex
        from github_releases import resolve_include_prereleases, resolve_release_policy
        selected_channel = resolve_include_prereleases(jobs.get(repository), include_prereleases)
        legacy = any(value is not None for value in (
            include_prereleases, separate_release_limits, stable_last, prerelease_last))
        selected_policy = resolve_release_policy(jobs.get(repository), release_policy, legacy=legacy)
        limits = _release_limits(jobs.get(repository), last,
            _UNSET if separate_release_limits is None else separate_release_limits,
            _UNSET if stable_last is None else stable_last,
            _UNSET if prerelease_last is None else prerelease_last)
        job.update(asset_patterns=asset_patterns, every_minutes=every_minutes, last=last,
                   branch=branch, source_only=source_only, start_time=start_time, archived=False,
                   include_prereleases=selected_channel, **limits)
        if selected_policy is not None:
            job["release_policy"] = selected_policy
        elif legacy:
            job.pop("release_policy", None)
        redirects = job.get("branch_redirects", {})
        job["branch_redirects"] = {old: new for old, new in redirects.items() if new == branch}
        jobs[repository] = job
        sync.validate_jobs(jobs)
        _commit_scheduler(sync, repository, jobs, lambda old: _install(sync, repository, job))
        _clear_deleted(sync, repository)
    _render(sync)
    return job


def _preview(base, repository, count):
    local, revisions = _inventory(base, repository)
    if local.get("error"):
        raise ValueError(local["error"])
    if type(count) is not int or not 1 <= count <= local["count"]:
        raise ValueError(f"Choose 1–{local['count']} local releases to remove")
    data = [str(Path(base).resolve()), repository, count, local, revisions]
    token = hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()
    return {"releases": local["releases"][:count], "token": token, "count": count,
            "remaining": local["count"] - count, "skipped": local["skipped"]}


def cleanup_preview(repository, count):
    with _locked(repository) as sync:
        _job(sync.load_config(), repository)
        return _preview(sync.BASE, repository, count)


def cleanup_releases(repository, count, token):
    with _locked(repository) as sync:
        _job(sync.load_config(), repository)
        preview = _preview(sync.BASE, repository, count)
        if not isinstance(token, str) or token != preview["token"]:
            raise ValueError("Local releases changed. Preview cleanup again before deleting.")
        root = _no_links(parse_repository(repository, allow_url=False).releases_path(sync.BASE)).resolve(strict=True)
        paths = []
        for release in preview["releases"]:
            path = _no_links(root / release["folder"])
            if path.resolve(strict=True).parent != root:
                raise ValueError("Cleanup target escaped the release directory")
            _tree_stamp(path)
            paths.append(path)
        removed = []
        try:
            for path in paths:
                # Recheck immediately before each recursive delete as well.
                _tree_stamp(path)
                shutil.rmtree(path)
                removed.append(path.name)
        finally:
            from github_storage import invalidate_size
            invalidate_size(sync.BASE, repository)
    _render(sync)
    return {"removed": removed, "count": len(removed), "remaining": preview["remaining"]}


def _asset_file_identity(info):
    return (info.st_mode, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, getattr(info, "st_file_attributes", 0))


def _legacy_asset_pattern_ambiguity(name, patterns):
    """Could a stripped prefix/suffix have made the unknown original match?

    Download logs retain sanitized local names, not original upstream names.
    safe_name strips leading/trailing dots and spaces. For supported globs,
    dots can represent either stripped character: only ? and * match spaces.
    A star can consume arbitrarily many padding characters, so only the dot/?
    tokens before/after the outermost other literals need to be enumerated.
    """
    combinations = 0
    for pattern in patterns:
        if not isinstance(pattern, str) or not re.fullmatch(r"[A-Za-z0-9.*?_-]+", pattern):
            return True
        pattern = pattern.lower()
        tokens = [character for character in pattern if character != "*"]
        literals = [index for index, character in enumerate(tokens) if character not in ".?"]
        leading = literals[0] if literals else len(tokens)
        trailing = len(tokens) - literals[-1] - 1 if literals else len(tokens)
        combinations += (leading + 1) * (trailing + 1)
        if combinations > 2048:
            return True  # Preserve rather than spend unbounded preview work.
        for prefix in range(leading + 1):
            for suffix in range(trailing + 1):
                if fnmatch.fnmatch("." * prefix + name.lower() + "." * suffix, pattern):
                    return True
    return False


def _asset_cleanup_preview(base, repository, job):
    """Select known, unmatched asset files without guessing from local suffixes."""
    from github_asset_history import legacy_asset_records, verified_legacy_asset
    local, revisions = _inventory(base, repository)
    if local.get("error"):
        raise ValueError(local["error"])
    root = _no_links(parse_repository(repository, allow_url=False).releases_path(base))
    patterns = list(job["asset_patterns"])
    skipped = [dict(item) for item in local["skipped"]]
    assets, candidates = [], []
    legacy_records = legacy_asset_records(base)
    metadata_revisions = {folder: digest for folder, digest, _ in revisions}
    resolved_root = root.resolve(strict=True) if root.exists() else None
    for release in local["releases"]:
        folder = _no_links(root / release["folder"])
        if folder.resolve(strict=True).parent != resolved_root:
            raise ValueError("Release folder changed. Preview Refresh Assets again.")
        metadata = _no_links(folder / "release.json")
        raw = metadata.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != metadata_revisions[release["folder"]]:
            raise ValueError("Release metadata changed. Preview Refresh Assets again.")
        content = json.loads(raw.decode("utf-8"))
        metadata_assets = content.get("assets")
        if not isinstance(metadata_assets, list):
            skipped.append({"folder": folder.name, "reason": "Release metadata has no verified asset list; files kept"})
            continue
        names = {}
        try:
            for asset in metadata_assets:
                if not isinstance(asset, dict) or not isinstance(asset.get("name"), str) or not asset["name"]:
                    raise ValueError("Release metadata contains an invalid asset name")
                mapped = _safe_name(asset["name"])
                names.setdefault(mapped.casefold(), []).append(asset["name"])
        except ValueError as error:
            skipped.append({"folder": folder.name, "reason": f"{error}; files kept"})
            continue
        source_name = f"Source-code-{_safe_name(release['tag'])}.zip"
        protected = {"release.json", source_name.casefold(), (source_name + ".tag").casefold()}
        entries = sorted(folder.iterdir(), key=lambda path: (path.name.casefold(), path.name))
        actual_names = {}
        for entry in entries:
            actual_names.setdefault(entry.name.casefold(), []).append(entry.name)
        metadata_identity = _asset_file_identity(metadata.lstat())
        folder_identity = (folder.stat().st_dev, folder.stat().st_ino)
        root_identity = (root.stat().st_dev, root.stat().st_ino)
        for entry in entries:
            folded = entry.name.casefold()
            if folded in protected or folded.endswith(".part"):
                continue
            identified = names.get(folded, [])
            if len(identified) > 1 or len(actual_names[folded]) != 1:
                skipped.append({"folder": folder.name, "name": entry.name,
                                "reason": "Asset names collide after case or filename normalization; kept"})
                continue
            # Use the original upstream name, exactly as the sync's selection
            # does. A sanitized local filename may match different patterns.
            original_name = identified[0] if identified else entry.name
            if any(fnmatch.fnmatch(original_name.lower(), pattern.lower()) for pattern in patterns):
                continue
            entry = _no_links(entry)
            info = entry.lstat()
            if not stat.S_ISREG(info.st_mode):
                skipped.append({"folder": folder.name, "name": entry.name,
                                "reason": "Asset path is not a regular file; kept"})
                continue
            if entry.resolve(strict=True).parent != folder.resolve(strict=True):
                raise ValueError("Asset path changed. Preview Refresh Assets again.")
            if not identified and _legacy_asset_pattern_ambiguity(entry.name, patterns):
                skipped.append({"folder": folder.name, "name": entry.name,
                                "reason": "Original upstream asset name is unavailable; stripped dots or spaces may match a saved pattern, so the file was kept"})
                continue
            if not identified and not verified_legacy_asset(base, entry, legacy_records):
                skipped.append({"folder": folder.name, "name": entry.name,
                                "reason": "Not identified in release.json or verified against a recorded download checksum; kept"})
                continue
            item = {"tag": release["tag"], "folder": folder.name, "name": entry.name, "size": info.st_size}
            assets.append(item)
            candidates.append({"item": item, "path": entry, "identity": _asset_file_identity(info),
                               "metadata": metadata, "metadata_digest": digest,
                               "metadata_identity": metadata_identity, "folder_identity": folder_identity,
                               "root_identity": root_identity})
    assets.sort(key=lambda item: (item["folder"].casefold(), item["name"].casefold(), item["name"]))
    candidates.sort(key=lambda item: (item["item"]["folder"].casefold(), item["item"]["name"].casefold(), item["item"]["name"]))
    skipped.sort(key=lambda item: (item["folder"].casefold(), item.get("name", "").casefold(), item["reason"]))
    token_data = [str(Path(base).resolve()), repository, job, local, revisions, assets,
                  [(str(item["path"]), item["identity"], item["metadata_identity"]) for item in candidates]]
    token = hashlib.sha256(json.dumps(token_data, sort_keys=True).encode("utf-8")).hexdigest()
    preview = {"token": token, "count": len(assets), "bytes": sum(item["size"] for item in assets),
               "release_count": len({item["folder"] for item in assets}), "patterns": patterns,
               "assets": assets, "skipped": skipped}
    return preview, candidates, root


def cleanup_assets_preview(repository):
    """Preview manual removal using current saved patterns across all local releases."""
    with _locked(repository) as sync:
        job = _job(sync.load_config(), repository)
        return _asset_cleanup_preview(sync.BASE, repository, job)[0]


def _verify_asset_candidate(root, candidate):
    """Recheck each direct file and its metadata immediately before unlinking."""
    root = _no_links(root)
    path = _no_links(candidate["path"])
    folder = _no_links(path.parent)
    metadata = _no_links(candidate["metadata"])
    root_stat, folder_stat = root.stat(), folder.stat()
    if ((root_stat.st_dev, root_stat.st_ino) != candidate["root_identity"]
            or (folder_stat.st_dev, folder_stat.st_ino) != candidate["folder_identity"]
            or folder.resolve(strict=True).parent != root.resolve(strict=True)
            or path.resolve(strict=True).parent != folder.resolve(strict=True)):
        raise ValueError("Release or asset path changed. Preview Refresh Assets again.")
    if (_asset_file_identity(metadata.lstat()) != candidate["metadata_identity"]
            or hashlib.sha256(metadata.read_bytes()).hexdigest() != candidate["metadata_digest"]):
        raise ValueError("Release metadata changed. Preview Refresh Assets again.")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or _asset_file_identity(info) != candidate["identity"]:
        raise ValueError("Asset file changed. Preview Refresh Assets again.")


def _asset_cleanup_result(removed):
    return {"count": len(removed), "bytes": sum(item["size"] for item in removed),
            "release_count": len({item["folder"] for item in removed}), "removed": list(removed)}


class AssetCleanupError(RuntimeError):
    """A deletion stopped; result describes only files already removed."""
    def __init__(self, error, removed):
        self.result = _asset_cleanup_result(removed)
        super().__init__(f"Refresh Assets stopped after removing {self.result['count']} assets "
                         f"({self.result['bytes']} bytes): {error}. Preview again before retrying.")


def cleanup_assets(repository, token):
    """Remove only the reviewed unmatched assets; never alter release metadata."""
    removed = []
    try:
        with _locked(repository) as sync:
            job = _job(sync.load_config(), repository)
            preview, candidates, root = _asset_cleanup_preview(sync.BASE, repository, job)
            if not isinstance(token, str) or token != preview["token"]:
                raise ValueError("Saved settings or local files changed. Preview Refresh Assets again before deleting.")
            # Validate every target before making the first change. Recheck
            # immediately before each unlink as external programs can edit too.
            for candidate in candidates:
                _verify_asset_candidate(root, candidate)
            try:
                for candidate in candidates:
                    _verify_asset_candidate(root, candidate)
                    candidate["path"].unlink()
                    removed.append(dict(candidate["item"]))
            except Exception as error:
                raise AssetCleanupError(error, removed) from error
    finally:
        if removed:
            # Refresh errors must not hide an accurate partial deletion result.
            try:
                from github_storage import invalidate_size
                invalidate_size(sync.BASE, repository)
            except Exception:
                pass
            try:
                _render(sync)
            except Exception:
                pass
    return _asset_cleanup_result(removed)


def prune_releases(releases_root, keep_tags):
    """Use the same path and metadata protections for automatic retention."""
    root = Path(releases_root)
    if not root.name.endswith("-releases"):
        raise ValueError("Unexpected release directory")
    local, _ = _inventory_root(root)
    if local.get("error"):
        raise ValueError(local["error"])
    resolved_root = _no_links(root).resolve(strict=True)
    candidates = []
    for item in local["releases"]:
        if item["folder"] in keep_tags:
            continue
        path = _no_links(root / item["folder"])
        if path.resolve(strict=True).parent != resolved_root:
            raise ValueError("Cleanup target escaped the release directory")
        _tree_stamp(path)
        candidates.append(path)
    for path in candidates:
        _tree_stamp(path)
        shutil.rmtree(path)
    return len(candidates)
