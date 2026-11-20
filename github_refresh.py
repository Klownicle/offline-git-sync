"""Confirmed, background release refreshes; source checkouts are never deleted."""

import hashlib
import fnmatch
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

import github_dashboard as dashboard
import github_management as management
import github_progress as progress
from github_releases import (classify_releases, resolve_include_prereleases,
                             resolve_release_limits, resolve_release_policy)
from github_sources import parse_repository


PREVIEW_SECONDS = 900


def _job(sync, repository):
    jobs = sync.load_config()
    job = management._job(jobs, repository)
    if job.get("archived", False):
        raise ValueError("Resume this archived sync before refreshing releases.")
    if job.get("source_only", False):
        raise ValueError("Enable release downloads before refreshing a source-only sync.")
    management._validate_add_paths(sync, {key: value for key, value in jobs.items() if key != repository},
                                   repository)
    return job


def _issued(token):
    try:
        issued = token.split(":", 1)[0]
        age = (time.time_ns() - int(issued)) / 1_000_000_000
        if not 0 <= age <= PREVIEW_SECONDS:
            raise ValueError
        return issued
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Refresh preview expired. Preview Refresh releases again.") from None


def _download_name(value):
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ValueError("Upstream returned an invalid download name; nothing was deleted.")
    name = management._safe_name(value)
    if len(name.encode("utf-16-le")) // 2 > 240:
        raise ValueError("Upstream returned a filename too long for Windows; nothing was deleted.")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if name.split(".", 1)[0].upper() in reserved:
        raise ValueError("Upstream returned a reserved Windows filename; nothing was deleted.")
    return name


def _download_url(value):
    if (not isinstance(value, str) or len(value) > 8192 or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ValueError("Upstream returned an invalid download URL; nothing was deleted.")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        raise ValueError("Upstream returned an unsafe download URL; nothing was deleted.")


def _validate_selected(selected, patterns):
    folders = set()
    for release in selected:
        folder = _download_name(release.get("tag_name"))
        if folder.casefold() in folders:
            raise ValueError("Selected releases use overlapping Windows folder names; nothing was deleted.")
        folders.add(folder.casefold())
        _download_url(release.get("zipball_url"))
        assets = release.get("assets")
        if not isinstance(assets, list):
            raise ValueError("Upstream returned an invalid asset list; nothing was deleted.")
        archive = f"source-code-{folder}.zip".casefold()
        if len((archive + ".part").encode("utf-16-le")) // 2 > 255:
            raise ValueError("The source archive filename is too long for Windows; nothing was deleted.")
        names = {"release.json", archive, archive + ".tag", archive + ".part"}
        for asset in assets:
            if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
                raise ValueError("Upstream returned invalid asset metadata; nothing was deleted.")
            if not any(fnmatch.fnmatch(asset["name"].lower(), pattern.lower()) for pattern in patterns):
                continue
            name = _download_name(asset.get("name")).casefold()
            if name in names or name + ".part" in names:
                raise ValueError("Selected assets use overlapping Windows filenames; nothing was deleted.")
            names.update((name, name + ".part"))
            if type(asset.get("id")) is not int or type(asset.get("size")) is not int or asset["size"] < 0:
                raise ValueError("Upstream returned invalid asset metadata; nothing was deleted.")
            _download_url(asset.get("browser_download_url"))


def _selection_revision(selected, patterns):
    """Bind download inputs; mutable download counters and prose are irrelevant."""
    result = []
    for release in selected:
        item = {key: release.get(key) for key in (
            "id", "tag_name", "published_at", "draft", "prerelease", "zipball_url", "type_id", "type_label", "type_source")}
        item["assets"] = [{key: asset.get(key) for key in (
            "id", "name", "size", "browser_download_url", "digest", "updated_at")}
            for asset in release["assets"] if any(
                fnmatch.fnmatch(asset["name"].lower(), pattern.lower()) for pattern in patterns)]
        result.append(item)
    return result


def _preview(sync, repository, job, issued, selected=None, *, with_stamps=False):
    local, revisions = management._inventory(sync.BASE, repository)
    if local.get("error"):
        raise ValueError(local["error"])
    reference = parse_repository(repository, allow_url=False)
    root = management._no_links(reference.releases_path(sync.BASE))
    base = management._no_links(sync.BASE).resolve()
    if not root.resolve().is_relative_to(base) or root.resolve() == base:
        raise ValueError("Release root escaped the sync directory")
    include_prereleases = resolve_include_prereleases(job)
    limits = resolve_release_limits(job, last=job["last"])
    policy = resolve_release_policy(job)
    if selected is None:
        selected = sync.select_repository_releases(repository, job["last"], include_prereleases,
                                                    release_policy=policy, **limits)
    if not selected:
        raise ValueError("No releases match the saved release channel. Nothing was deleted.")
    descriptions = classify_releases(selected, policy["type_rules"] if policy else None)
    selected = descriptions["releases"]
    _validate_selected(selected, job["asset_patterns"])
    skipped = list(local["skipped"])
    if root.exists():
        for child in root.iterdir():
            if not child.is_dir() and not any(item["folder"] == child.name for item in skipped):
                skipped.append({"folder": child.name, "reason": "Unrecognized file; kept"})
    kept = {item["folder"].casefold() for item in skipped}
    chosen = []
    for release in selected:
        tag = release.get("tag_name")
        if not isinstance(tag, str):
            raise ValueError("Upstream returned an invalid release tag; nothing was deleted.")
        folder = management._safe_name(tag)
        if folder.casefold() in kept:
            raise ValueError(f"Release {tag} overlaps an unrecognized local folder or file. "
                             "Move that item aside before refreshing; nothing was deleted.")
        chosen.append({"tag": tag, "prerelease": release.get("prerelease", False),
                       "published_at": release.get("published_at"),
                       "type_id": release["type_id"], "type_label": release["type_label"],
                       "type_source": release.get("type_source")})
    sizes = {folder: sum(item[2] for item in stamp if stat.S_ISREG(item[1]))
             for folder, _, stamp in revisions}
    releases = [{**release, "bytes": sizes[release["folder"]]} for release in local["releases"]]
    data = [str(base), repository, job, local, revisions,
            _selection_revision(selected, job["asset_patterns"]), descriptions["release_types"], issued]
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()
    result = {"token": f"{issued}:{digest}", "count": len(releases),
            "bytes": sum(sizes.values()), "releases": releases, "skipped": skipped,
            "selected": chosen, "include_prereleases": include_prereleases,
            "release_policy": policy, "release_types": descriptions["release_types"], **limits,
            "last": job["last"], "source_preserved": True, "expires_in": PREVIEW_SECONDS}
    if with_stamps:
        result["_stamps"] = {folder: stamp for folder, _, stamp in revisions}
    return result


def preview(repository):
    with management._locked(repository) as sync:
        return _preview(sync, repository, _job(sync, repository), str(time.time_ns()))


def _request_path(base, repository):
    return management._no_links(Path(base) / ".sync-state" /
                                (dashboard.key(repository) + ".refresh-request.json"))


def _read_request(base, repository):
    path = _request_path(base, repository)
    if not path.exists():
        return None
    content = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(content, dict) or content.get("repository") != repository:
        raise ValueError("Invalid refresh request record")
    return content


def _update_request(base, repository, request_id, *, expected_status=None, **changes):
    path = _request_path(base, repository)
    with dashboard.lock(path.with_suffix(".lock"), timeout=5):
        current = _read_request(base, repository)
        if current and current["id"] == request_id and (
                expected_status is None or current["status"] == expected_status):
            current.update(changes, updated_at=dashboard.now())
            dashboard.atomic_write(path, json.dumps(current, indent=2))
            return current
    return None


def _request_progress(request):
    state = {key: request.get(key) for key in (
        "id", "repository", "status", "queued_at", "updated_at", "error", "pid",
        "process_identity", "finished_at")}
    state.update(kind="refresh_releases", stage=state["status"],
                 phase="Release refresh could not start" if state["status"] == "failed" else
                       "Waiting to refresh releases", files_completed=0, files_total=None,
                 bytes_downloaded=0, bytes_total=None)
    return state


def progress_snapshot(base, repository, current=None):
    """Expose a launch failure even when the worker never acquired its run lock."""
    request = _read_request(base, repository)
    if not request or request["status"] == "complete":
        return current
    if current and current.get("queued_at", "") >= request["queued_at"]:
        return current
    state = _request_progress(request)
    if state["status"] in {"queued", "running"}:
        pid = request.get("pid")
        identity = dashboard.process_identity(pid) if pid else None
        if (pid and (not identity or identity != request.get("process_identity"))) or (
                not pid and time.time() - request["created"] > 120):
            state.update(status="failed", stage="failed", phase="Release refresh was interrupted",
                         error="The refresh worker did not start. Preview and confirm again; "
                               "no release deletion was started by this request.")
    return state


def _launch(sync, repository, request_id):
    python = Path(sys.executable).with_name("pythonw.exe")
    if not python.is_file():
        raise FileNotFoundError(f"Windowless Python is missing: {python}")
    return subprocess.Popen([str(python), str(Path(__file__).resolve()), repository, request_id],
        cwd=str(sync.BASE), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def start(repository, token):
    issued = _issued(token)
    with management._locked(repository) as sync:
        job = _job(sync, repository)
        checked = _preview(sync, repository, job, issued)
        if token != checked["token"]:
            raise ValueError("Settings, local files, or upstream releases changed. Preview Refresh releases again.")
        previous = _read_request(sync.BASE, repository)
        if previous and previous.get("token") == token:
            raise ValueError("This refresh confirmation was already used. Preview Refresh releases again.")
        if previous and previous["status"] in {"queued", "running"}:
            state = progress_snapshot(sync.BASE, repository)
            if state["status"] != "failed":
                raise ValueError("A release refresh is already pending for this sync.")
        request = {"id": secrets.token_hex(16), "repository": repository, "token": token,
                   "status": "queued", "queued_at": dashboard.now(), "created": time.time(),
                   "pid": None, "error": None}
        dashboard.atomic_write(_request_path(sync.BASE, repository), json.dumps(request, indent=2))
    try:
        process = _launch(sync, repository, request["id"])
    except Exception as error:
        _update_request(sync.BASE, repository, request["id"], status="failed", error=str(error),
                        finished_at=dashboard.now())
        raise RuntimeError(f"Release refresh could not start; no releases were deleted: {error}") from error
    result = {"started": True, "first_sync": _request_progress(request),
              "refresh_request": {"id": request["id"], "queued_at": request["queued_at"]}}
    try:
        _update_request(sync.BASE, repository, request["id"], pid=process.pid,
                        process_identity=dashboard.process_identity(process.pid))
    except Exception as error:
        result["warning"] = f"Refresh was launched, but its launch status could not update: {error}"
    return result


def _verify_tags(sync, repository, selected):
    source = parse_repository(repository, allow_url=False).source_path(sync.BASE)
    git = sync.git_executable()
    environment = sync.git_environment(git)
    for release in selected:
        tag = release["tag_name"]
        result = subprocess.run([str(git), "-c", f"safe.directory={source}",
                                 "-c", f"core.hooksPath={os.devnull}",
                                 "-C", str(source), "rev-parse", "--verify",
                                 f"refs/tags/{tag}^{{commit}}"], env=environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise ValueError(f"Source tag {tag} is unavailable. Nothing was deleted; try again after the upstream tag is available.")


def _delete(sync, repository, job, token, selected, report, result):
    _verify_tags(sync, repository, selected)
    checked = _preview(sync, repository, _job(sync, repository), _issued(token), selected,
                       with_stamps=True)
    if token != checked["token"]:
        raise ValueError("Settings, local files, or upstream releases changed; refresh cancelled before deletion. "
                         "Preview Refresh releases again.")
    root = management._no_links(parse_repository(repository, allow_url=False).releases_path(sync.BASE))
    if not checked["releases"]:
        return
    resolved = root.resolve(strict=True)
    paths = []
    for release in checked["releases"]:
        path = management._no_links(root / release["folder"])
        if path.resolve(strict=True).parent != resolved:
            raise ValueError("Refresh target escaped its release directory")
        stamp = checked["_stamps"][release["folder"]]
        if management._tree_stamp(path) != stamp:
            raise ValueError("A release folder changed after its preview; nothing was deleted")
        paths.append((path, stamp))
    try:
        for path, stamp in paths:
            if management._tree_stamp(path) != stamp:
                raise ValueError("A release folder changed during refresh; stopped deleting")
            result["affected"] = path.name
            shutil.rmtree(path)
            result["removed"].append(path.name)
            result["affected"] = None
            report.removed(len(result["removed"]))
    finally:
        from github_storage import invalidate_size
        invalidate_size(sync.BASE, repository)


def _run_request(sync, repository, request):
    request_id = request["id"]
    report = tracker = None
    result = {"removed": [], "affected": None}
    try:
        _update_request(sync.BASE, repository, request_id, status="running", pid=os.getpid(),
                        process_identity=dashboard.process_identity(os.getpid()))
        job = _job(sync, repository)
        # Revalidate before touching the checkout, and again immediately before deleting.
        checked = _preview(sync, repository, job, _issued(request["token"]))
        if checked["token"] != request["token"]:
            raise ValueError("Settings, local files, or upstream releases changed. Preview Refresh releases again.")
        progress.queue(sync.BASE, repository, kind="refresh_releases")
        tracker = progress.Tracker(sync.BASE, repository, sync.emit)
        report = dashboard.Report(sync.BASE, repository, job, sync.emit)
        report.start()
        sync._sync(repository, job["asset_patterns"], job["last"], job.get("branch"), False,
            report, tracker, include_prereleases=resolve_include_prereleases(job),
            release_policy=resolve_release_policy(job), **resolve_release_limits(job, last=job["last"]),
            before_releases=lambda selected: _delete(sync, repository, job, request["token"],
                                                     selected, report, result))
        tracker.finish(warnings=bool(report.state.get("warnings")))
        report.finish()
        _update_request(sync.BASE, repository, request_id, status="complete", result=result,
                        finished_at=dashboard.now())
    except Exception as error:
        detail = str(error)
        if result["removed"] or result["affected"]:
            detail = (f"Refresh stopped after deleting {len(result['removed'])} release folders. "
                      + (f"Folder {result['affected']} may be partly deleted. " if result["affected"] else "")
                      + f"Downloads may be incomplete. Source checkout was kept. {error}")
        if tracker:
            tracker.finish(detail)
        if report:
            report.finish(detail)
        _update_request(sync.BASE, repository, request_id, status="failed", error=detail,
                        result=result, finished_at=dashboard.now())
        sync.emit(f"{repository}: release refresh failed: {detail}", error=True)
        return False
    return True


def run(repository, request_id):
    """The worker consumes one request and owns the run lock through deletion/sync."""
    sync = management._sync()
    try:
        with dashboard.lock(sync.BASE / ".sync-state" / (dashboard.key(repository) + ".run.lock"), timeout=0):
            request = _read_request(sync.BASE, repository)
            if not request or request["id"] != request_id or request["status"] != "queued":
                return False
            return _run_request(sync, repository, request)
    except dashboard.LockBusy:
        detail = "This sync is already running. Nothing was deleted; preview and confirm again after it finishes."
        _update_request(sync.BASE, repository, request_id, expected_status="queued",
                        status="failed", error=detail, finished_at=dashboard.now())
        sync.emit(f"{repository}: release refresh cancelled: {detail}", error=True)
        return False
    finally:
        management._render(sync)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("This worker is started through the dashboard's confirmed Refresh releases action.")
    repository = parse_repository(sys.argv[1], allow_url=False).key
    raise SystemExit(0 if run(repository, sys.argv[2]) else 1)
