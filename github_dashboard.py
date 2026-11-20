"""Local, dependency-free status reporting for github_sync.py."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.request
from github_https import urlopen
from github_sources import parse_repository


class LockBusy(TimeoutError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        # Antivirus/browser readers can hold a file briefly on Windows.
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def lock(path, timeout=10):
    """Kernel-owned lock: released even if a sync process is terminated."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockBusy(f"Another sync is using {path.name}")
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)


def key(repository):
    return hashlib.sha256(repository.encode("utf-8")).hexdigest()[:24]


def state_path(base, repository):
    return base / ".sync-state" / (key(repository) + ".json")


def read_state(base, repository):
    path = state_path(base, repository)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def metadata_path(base, repository):
    return base / ".sync-state" / (key(repository) + ".metadata.json")


def read_metadata(base, repository):
    path = metadata_path(base, repository)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def refresh_metadata(base, repository, headers=None):
    """Cache public descriptions separately from run state; retry failures next sync."""
    metadata = read_metadata(base, repository)
    if metadata.get("checked_at"):
        elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(metadata["checked_at"])
        if 0 <= elapsed.total_seconds() < 86400:
            return
    remote = parse_repository(repository, allow_url=False)
    # The provider supplies its own headers; GitHub-specific headers must never
    # carry over when a repository belongs to another supported host.
    request_headers = dict(remote.headers)
    if metadata.get("etag"):
        request_headers["If-None-Match"] = metadata["etag"]
    request = urllib.request.Request(remote.api_url, headers=request_headers)
    try:
        if remote.provider == "github":
            opener = urlopen
        else:
            from github_forgejo import urlopen as opener
        with opener(request, timeout=15) as response:
            if remote.provider == "github":
                content = json.load(response)
            else:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError(f"{remote.host} repository metadata exceeds the 2 MiB limit")
                content = json.loads(raw)
            description = content.get("description") or ""
            if not isinstance(description, str):
                raise ValueError(f"{remote.host} returned an invalid repository description")
            metadata = {"description": description, "etag": response.headers.get("ETag")}
    except urllib.error.HTTPError as error:
        if error.code != 304:
            raise
    metadata["checked_at"] = now()
    atomic_write(metadata_path(base, repository), json.dumps(metadata, indent=2))


def process_identity(pid):
    """Identify a live process; creation time excludes a recycled Windows PID."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return str(pid)
        except OSError:
            return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)  # Query limited + synchronize.
    if not handle:
        return None
    try:
        # Windows retains an exited process object while Popen or another owner
        # holds a handle. Its creation time still exists, so check liveness too.
        # WAIT_TIMEOUT means it is still running. A signaled handle has exited.
        if kernel.WaitForSingleObject(handle, 0) != 0x102:
            return None
        values = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
            return None
        return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def summarize(change):
    parts = []
    source = change.get("source")
    if source:
        if source.get("initial"):
            parts.append("Initial source download")
        elif source.get("rewritten"):
            parts.append("Followed rewritten upstream history")
        else:
            count = source.get("commit_count")
            parts.append(f"{count} new commit{'s' if count != 1 else ''}" if count is not None
                         else "Source updated")
        if source.get("file_count") is not None:
            parts.append(f"{source['file_count']} source files changed")
        if source.get("after"):
            parts.append(f"revision {source['after'][:8]}")
    count = change.get("download_count", len(change.get("downloads", [])))
    if count:
        parts.append(f"{count} release file{'s' if count != 1 else ''} downloaded")
    removed = change.get("removed", 0)
    if removed:
        parts.append(f"{removed} old release{'s' if removed != 1 else ''} removed")
    if change.get("lfs_hydrated"):
        parts.append("Previously incomplete LFS files downloaded")
    return " · ".join(parts)


def import_log(base, jobs):
    """Import only attributable, timestamped results; never invent missing times."""
    states = {}
    revisions = {}
    log = base / "sync.log"
    if not log.exists():
        return states
    pattern = re.compile(r"^(\d{4}-\d\d-\d\dT\S+) ((?:[A-Za-z0-9.-]+/)?[\w.-]+/[\w.-]+) "
                         r"(?:branch=(\S+) )?source=(updated|unchanged) ([0-9a-f]{40})\b(.*)")
    failure = re.compile(r"^(\d{4}-\d\d-\d\dT\S+) (?:(?:GitHub|Git|Repository|Forgejo|Gitea) sync failed: )?"
                         r"((?:[A-Za-z0-9.-]+/)?[\w.-]+/[\w.-]+)(?::| failed:) (.*)")
    with log.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            match = pattern.match(line)
            if match and match[2] in jobs:
                timestamp, repo, branch, changed, revision, rest = match.groups()
                state = states.setdefault(repo, {"repository": repo})
                fields = dict(re.findall(r"(\w+)=([\w-]+)", rest))
                lfs = fields.get("lfs", "unknown")
                warning = lfs in {"incomplete", "unavailable"}
                state.update(status="warning" if warning else "success",
                             finished_at=timestamp, last_success_at=timestamp,
                             revision=revision, lfs=lfs, imported=True, error=None,
                             warnings=[f"Git LFS {lfs}; some large files are unavailable."] if warning else [])
                if branch:
                    state["branch"] = branch
                count = int(fields.get("new_files", fields.get("new_assets", "0")))
                removed = int(fields.get("old_releases_removed", "0"))
                change = {"at": timestamp, "imported": True, "download_count": count,
                          "removed": removed, "downloads": []}
                if changed == "updated":
                    change["source"] = {"before": revisions.get(repo), "after": revision}
                if summarize(change):
                    change["summary"] = summarize(change)
                    state["last_change"] = change
                revisions[repo] = revision
            else:
                match = failure.match(line)
                if match and match[2] in jobs:
                    state = states.setdefault(match[2], {"repository": match[2]})
                    state.update(status="failed", finished_at=match[1], error=match[3],
                                 imported=True)
    return states


def bootstrap(base, jobs):
    missing = {repo: job for repo, job in jobs.items() if not state_path(base, repo).exists()}
    if not missing:
        return
    states = import_log(base, missing)
    for repo in missing:
        # A sync can start while the old log is being scanned.
        with lock(base / ".sync-state" / (key(repo) + ".state.lock")):
            if state_path(base, repo).exists():
                continue
            state = states.get(repo, {"repository": repo, "status": "unknown"})
            atomic_write(state_path(base, repo), json.dumps(state, indent=2))


def snapshot(base):
    """Read dashboard state and all recognized local release folders."""
    from github_management import inventory
    from github_storage import sync_size
    from github_progress import snapshot as first_sync_snapshot
    base = Path(base).resolve()
    config = base / "sync-config.json"
    jobs = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    bootstrap(base, jobs)
    rows = []
    for repo, job in jobs.items():
        remote = parse_repository(repo, allow_url=False)
        state = read_state(base, repo)
        if state.get("status") == "running":
            identity = process_identity(state.get("pid", 0)) if state.get("pid") else None
            if not identity or identity != state.get("process_identity"):
                state.update(status="interrupted",
                             error="The sync process stopped without recording a result.")
        source = remote.source_path(base)
        if not state.get("branch") and (source / ".git" / "HEAD").is_file():
            head = (source / ".git" / "HEAD").read_text(encoding="utf-8").strip()
            if head.startswith("ref: refs/heads/"):
                state["branch"] = head.removeprefix("ref: refs/heads/")
        metadata = read_metadata(base, repo)
        try:
            local_releases = inventory(base, repo)
        except (OSError, ValueError) as error:
            local_releases = {"count": None, "releases": [], "skipped": [], "error": str(error)}
        rows.append({**state, "repository": repo, "config": job,
                     "web_url": remote.web_url, "host": remote.host,
                     "provider": remote.provider, "owner": remote.owner, "name": remote.name,
                     "description": metadata.get("description", ""),
                     "inventory": local_releases,
                     "first_sync": first_sync_snapshot(base, repo),
                     "size": sync_size(base, repo),
                     "checkout_exists": (source / ".git").is_dir(),
                     "source_url": source.as_uri(),
                     "releases_url": remote.releases_path(base).as_uri()})
    return {"generated_at": now(), "rows": rows}


def html_page(data, management=None):
    """Render a static snapshot or an authenticated local management page."""
    def script_json(value):
        # This is data, never markup; commit subjects and errors are untrusted.
        return json.dumps(value, ensure_ascii=True).replace("<", "\\u003c").replace(
            ">", "\\u003e").replace("&", "\\u0026")
    values = {"__PAYLOAD__": script_json(data), "__MANAGEMENT__": script_json(management)}
    return re.sub(r"__PAYLOAD__|__MANAGEMENT__", lambda match: values[match[0]], HTML)


def render(base, management=None):
    """Serialize static generation; never persist a management token to disk."""
    base = Path(base).resolve()
    with lock(base / ".sync-state" / "dashboard.lock"):
        page = html_page(snapshot(base), management)
        if management is not None:
            return page
        atomic_write(base / "dashboard.html", page)
    return base / "dashboard.html"


class Report:
    def __init__(self, base, repository, job, emit):
        self.base, self.repository, self.job, self.emit = base, repository, job, emit
        self.state = {}
        self.change = {"downloads": [], "removed": 0}

    def safe(self, operation):
        try:
            return operation()
        except Exception as error:
            self.emit(f"WARNING: {self.repository}: dashboard could not update: {error}")

    def start(self):
        def operation():
            config = self.base / "sync-config.json"
            jobs = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
            bootstrap(self.base, {**jobs, self.repository: self.job})
            self.state = read_state(self.base, self.repository)
            self.state.update(repository=self.repository, status="running", started_at=now(),
                              pid=os.getpid(), process_identity=process_identity(os.getpid()),
                              phase="Fetching source", warnings=[], error=None, imported=False)
            self.persist()
        self.safe(operation)

    def persist(self):
        self.state["updated_at"] = now()
        if summarize(self.change):
            self.change["summary"] = summarize(self.change)
            self.state["last_change"] = dict(self.change)
        atomic_write(state_path(self.base, self.repository), json.dumps(self.state, indent=2))
        render(self.base)

    def source(self, before, after, branch, initial, run_git, source):
        def operation():
            self.state.update(revision=after, branch=branch, phase="Downloading Git LFS files")
            if initial or before != after:
                info = {"before": None if initial else before, "after": after, "initial": initial}
                # Summary failures must not turn successful source downloads into failures.
                try:
                    history = after if initial else f"{before}..{after}"
                    info["commits"] = run_git("-C", str(source), "log", "-5",
                                             "--format=%h %s", history).splitlines()
                    if not initial:
                        info["commit_count"] = int(run_git("-C", str(source), "rev-list", "--count", history))
                        info["rewritten"] = int(run_git("-C", str(source), "rev-list", "--count",
                                                       f"{after}..{before}")) > 0
                        info["file_count"] = len(run_git("-C", str(source), "diff",
                                                       "--name-only", before, after).splitlines())
                        info["diffstat"] = run_git("-C", str(source), "diff", "--shortstat", before, after)
                except Exception as error:
                    info["detail_note"] = f"Commit summary unavailable: {error}"
                self.change.update(at=now(), source=info)
            self.persist()
        self.safe(operation)

    def lfs(self, status):
        def operation():
            if status == "ok" and self.state.get("lfs") in {"incomplete", "unavailable"}:
                self.change.update(at=now(), lfs_hydrated=True)
            self.state.update(lfs=status, phase="Checking releases")
            if status in {"incomplete", "unavailable"}:
                self.state["warnings"].append(f"Git LFS {status}; some large files remain unavailable. See sync.log.")
            self.persist()
        self.safe(operation)

    def downloaded(self, tag, filename):
        self.change["downloads"].append(f"{tag} / {filename}")
        self.change["at"] = now()
        self.state["phase"] = f"Downloaded {filename}"
        self.safe(self.persist)

    def removed(self, count):
        if count:
            self.change.update(removed=count, at=now())
            self.safe(self.persist)

    def finish(self, error=None):
        def operation():
            from github_storage import invalidate_size
            invalidate_size(self.base, self.repository)
            self.state.update(status="failed" if error else
                              ("warning" if self.state.get("warnings") else "success"),
                              finished_at=now(), error=str(error) if error else None,
                              phase="Finished", last_run_changed=bool(summarize(self.change)))
            if not error:
                self.state["last_success_at"] = self.state["finished_at"]
            if summarize(self.change):
                self.change["partial"] = bool(error)
            self.persist()
        self.safe(operation)


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Git sync · Local dashboard</title>
<style>
:root{color-scheme:light;--ink:#17292c;--muted:#617477;--line:#dce5e2;--green:#126747;--paper:#f3f6f2}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.55 system-ui,Segoe UI,sans-serif}
header{background:#142f2b;color:#fff;padding:35px max(28px,calc((100vw - 1440px)/2))}
.top{display:flex;align-items:center;justify-content:space-between;gap:20px}.brand{font-size:12px;letter-spacing:.18em;font-weight:700;color:#a3d2bb}
h1{font-size:38px;font-weight:600;letter-spacing:-1.5px;margin:12px 0 5px}header p{margin:0;color:#c1d1ca;max-width:660px}
.live{font-size:12px;border:1px solid #536e60;padding:8px 13px;border-radius:30px;white-space:nowrap}.live:before{content:"";display:inline-block;width:7px;height:7px;background:#a1eac1;border-radius:50%;margin-right:8px}
main{max-width:1496px;margin:auto;padding:26px 28px 50px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:28px}
.stat{background:white;border:1px solid var(--line);border-radius:10px;padding:18px 22px}.stat span{font-size:12px;font-weight:600;color:var(--muted)}.stat strong{display:block;font-size:30px;font-weight:600;line-height:1.4}
.toolbar{display:flex;justify-content:space-between;gap:18px;align-items:center;margin:0 0 16px}.toolbar h2{font-size:18px;margin:0}.subtitle{font-size:12px;color:var(--muted)}
.controls{display:flex;gap:8px;flex-wrap:wrap}input,select,button{font:inherit;background:white;color:var(--ink);border:1px solid #cbd8d2;border-radius:7px;padding:9px 12px}input{width:235px}button{cursor:pointer;font-weight:600}button:hover{background:#e8f1ea}input:focus,select:focus,button:focus-visible{outline:2px solid #168759;outline-offset:2px}
.table-wrap{overflow-x:auto;background:white;border:1px solid var(--line);border-radius:11px}table{width:100%;border-collapse:collapse;table-layout:fixed;min-width:1080px}
th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.1em;padding:14px 18px;background:#eaf0e9;color:#586d65}
td{border-top:1px solid var(--line);padding:18px;vertical-align:top}th:nth-child(1){width:23%}th:nth-child(2){width:12%}th:nth-child(3){width:15%}th:nth-child(4){width:17%}th:nth-child(5){width:33%}
.sync-size{font-size:15px;font-weight:650;white-space:nowrap;font-variant-numeric:tabular-nums}.size-breakdown{font-size:11px;color:var(--muted);margin-top:5px;line-height:1.7}
.owner{font-size:11px;color:var(--muted)}.repo{font-size:16px;font-weight:650;overflow-wrap:anywhere}.repo a{color:var(--ink);text-decoration:none}.repo a:hover{text-decoration:underline}
.host-badge{display:inline-block;max-width:100%;margin:6px 0 0;padding:2px 6px;border:1px solid #d5dfda;border-radius:5px;background:#f2f6f3;color:#49615b;font-size:10px;overflow-wrap:anywhere}.host-badge.forgejo{background:#edf1fb;border-color:#d2daed;color:#3b5687}
.description{font-size:12px;line-height:1.5;color:#485e59;margin-top:7px;overflow-wrap:anywhere}
.meta{font-size:11px;color:var(--muted);margin-top:6px;overflow-wrap:anywhere}.badge{display:inline-block;border-radius:5px;padding:3px 8px;font-size:11px;font-weight:650;white-space:nowrap}
.success{color:#196546;background:#e7f2e8}.running{color:#245ca0;background:#e6effa}.warning{color:#8d5b0b;background:#fff0cf}.failed,.interrupted{color:#a13737;background:#fceaea}.unknown{background:#eef0f0;color:#5f6d6d}
.archived{color:#685583;background:#eee8f5}.local-count{font-size:12px;font-weight:650;color:#365449;margin-top:9px}.row-actions{margin-top:10px}.row-actions button{font-size:11px;padding:5px 10px}.link-button{border:0;background:none;padding:0;color:var(--green);font-size:12px;font-weight:400;text-decoration:underline}.link-button:hover{background:none}.notice{padding:14px 18px;background:#e8efe4;border:1px solid #cddbc8;border-radius:9px;margin-bottom:22px;font-size:12px}.notice p{margin:4px 0}.notice code{overflow-wrap:anywhere}.status-message{border-radius:7px;padding:10px 14px;margin-bottom:16px;background:#e7f2e8;color:#196546}.status-message.error{background:#fff0ec;color:#9b4334}[hidden]{display:none!important}
dialog{border:1px solid var(--line);border-radius:14px;width:min(660px,calc(100% - 24px));max-height:calc(100% - 40px);padding:0;color:var(--ink);box-shadow:0 24px 90px #142f2b44}dialog::backdrop{background:#102a2866;backdrop-filter:blur(2px)}.modal-head{display:flex;justify-content:space-between;gap:18px;padding:21px 24px;border-bottom:1px solid var(--line);align-items:flex-start}.modal-head h2{margin:3px 0;font-size:21px;overflow-wrap:anywhere}.modal-head .close{padding:4px 10px;font-size:20px;font-weight:400}.modal-body{padding:0 24px 22px}.modal-section{padding:20px 0;border-bottom:1px solid var(--line)}.modal-section:last-child{border-bottom:0;padding-bottom:0}.modal-section h3{font-size:14px;margin:0 0 6px}.modal-section p{margin:5px 0 12px;font-size:12px;color:var(--muted)}.form-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin:14px 0}.field{font-size:11px;font-weight:650;display:block}.field input{display:block;width:100%;margin-top:6px}.field small{display:block;color:var(--muted);font-weight:400;margin-top:4px}.modal-buttons{display:flex;align-items:center;gap:9px;flex-wrap:wrap}.primary{background:#126747;color:white;border-color:#126747}.primary:hover{background:#0b5439}.danger{color:#a13737;border-color:#d8aaa3}.danger:hover{background:#fff0ec}.cleanup-controls{display:flex;gap:10px;align-items:end;margin:12px 0}.cleanup-controls input{width:110px}.cleanup-preview{border:1px solid #e5d0b1;background:#fff9ed;border-radius:8px;padding:13px 15px;margin-top:14px}.cleanup-preview p{color:#6a5435}.release-list{max-height:190px;overflow:auto;padding-left:21px;font-size:12px}.release-list li{padding:3px 0;overflow-wrap:anywhere}.release-folder{color:var(--muted);font-size:11px}.modal-error{margin:12px 0 0}.modal-info{font-size:12px;color:var(--muted)}button:disabled{cursor:wait;opacity:.55}.saved-history{margin-top:8px!important}.skipped-list{color:var(--muted);font-size:11px}.modal-section details{margin-top:12px}
.pattern-list{display:grid;gap:10px;margin:14px 0}.pattern-row{display:flex;gap:9px;align-items:end}.pattern-row .field{flex:1;min-width:0}.pattern-row input{font-family:ui-monospace,Consolas,monospace}.pattern-row button{flex:none}.pattern-help code{overflow-wrap:anywhere}.inline-status{margin:10px 0 0;font-size:12px;color:var(--green)}.check-field{display:flex;gap:9px;align-items:flex-start;font-size:12px;font-weight:600;margin:14px 0}.check-field input{width:16px;min-width:16px;flex:none;margin:3px 0 0;accent-color:var(--green)}.wide-field{margin-top:14px}.status-message.caution{color:#795315;background:#fff0cf}.selected-row{box-shadow:inset 4px 0 0 var(--green)}
.task-diagnostics{border:1px solid #e5d0b1;background:#fffaf1;border-radius:8px;padding:12px 14px;margin-top:12px;font-size:12px}.task-diagnostics p{margin:8px 0}.task-diagnostics pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:270px;overflow:auto;font:11px/1.6 ui-monospace,Consolas,monospace;background:white;border:1px solid var(--line);border-radius:6px;padding:11px}.task-diagnostics textarea{display:block;width:100%;margin:10px 0 0;resize:vertical;font:11px/1.6 ui-monospace,Consolas,monospace;padding:10px;border:1px solid var(--line);border-radius:6px}.task-diagnostics button{font-size:12px}
.first-sync{margin:12px 0;padding:11px 12px;border:1px solid #cbded3;border-radius:8px;background:#f4f9f5;font-size:12px}.first-sync strong{display:block;font-size:12px}.first-sync progress{display:block;width:100%;height:9px;margin:9px 0;accent-color:var(--green)}.first-sync small{display:block;color:var(--muted);overflow-wrap:anywhere}.first-sync .problem{margin-top:6px;font-size:11px}.first-sync .progress-phase{margin-top:3px;overflow-wrap:anywhere}#asset-dialog{width:min(880px,calc(100% - 24px))}.asset-summary{padding:11px 13px;border:1px solid var(--line);border-radius:7px;background:#f4f7f3;font-size:12px;margin:14px 0}.asset-release{border:1px solid var(--line);border-radius:9px;margin:14px 0;overflow:hidden}.asset-release-head{background:#eaf0e9;padding:12px 14px}.asset-release-head h3{font-size:14px;margin:0;overflow-wrap:anywhere}.asset-release-head p{margin:3px 0 0;font-size:11px;color:var(--muted)}.asset-item{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 18px;padding:10px 14px;border-top:1px solid var(--line);font-size:12px}.asset-item.match{background:#eaf6ec;box-shadow:inset 3px 0 #35805a}.asset-item.source-archive{background:#eef4fa;box-shadow:inset 3px 0 #477cad}.asset-name{overflow-wrap:anywhere;font-family:ui-monospace,Consolas,monospace}.asset-mark{font-size:10px;font-weight:650;white-space:nowrap;text-align:right}.asset-size{font-size:11px;color:var(--muted)}.asset-message{font-size:12px;color:var(--muted);padding:16px 0}.asset-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.asset-caption{font-size:12px;color:var(--muted)}
.time{font-size:12px;font-weight:600}.exact{display:block;font-size:11px;color:var(--muted);margin-top:3px}.change-title{font-size:13px;font-weight:550}.change-time{font-size:11px;color:var(--green);margin-bottom:6px}
.note{font-size:11px;color:var(--muted);margin-top:6px}.problem{font-size:12px;color:#9b4334;background:#fff5ef;padding:9px 11px;border-radius:6px;margin-top:9px;overflow-wrap:anywhere;white-space:pre-wrap;max-height:150px;overflow:auto}
details{margin-top:8px;font-size:12px}summary{cursor:pointer;color:var(--green);width:fit-content}details ul{margin:8px 0;padding-left:18px}details li{padding:3px 0;overflow-wrap:anywhere}.detail-links{display:flex;gap:14px;margin-top:10px}a{color:var(--green)}
.empty{text-align:center;padding:50px;color:var(--muted)}footer{display:flex;justify-content:space-between;gap:15px;color:var(--muted);font-size:11px;padding-top:18px}.footer-links{white-space:nowrap}
@media(max-width:780px){header{padding:25px 20px}h1{font-size:30px}main{padding:20px 14px}.stats{grid-template-columns:repeat(2,1fr);gap:10px}.stat{padding:12px 16px}.toolbar{align-items:flex-start;flex-direction:column}.controls{width:100%}input{flex:1;min-width:150px}.top{align-items:flex-start}.live{font-size:10px}footer{flex-direction:column}.form-grid{grid-template-columns:1fr}.field input{min-width:0}.cleanup-controls{align-items:end;flex-wrap:wrap}.cleanup-controls input{min-width:0}.modal-head{padding:18px}.modal-body{padding:0 18px 18px}
table{min-width:0;display:block}thead{display:none}tbody,tr,td{display:block;width:100%}tr{padding:16px 18px;border-bottom:1px solid var(--line)}tr:last-child{border-bottom:0}td{padding:5px 0;border:0}td:nth-child(3):before,td:nth-child(4):before,td:nth-child(5):before{display:block;font-size:10px;color:var(--muted);letter-spacing:.08em;margin:8px 0 5px}td:nth-child(3):before{content:"SYNC SIZE"}td:nth-child(4):before{content:"LAST SYNC COMPLETED"}td:nth-child(5):before{content:"LAST CHANGE"}}
</style></head><body>
<header><div class="top"><div><div class="brand">OFFLINE GIT SYNC / LOCAL SYNC MONITOR</div><h1>Your repositories, in sync.</h1></div><div class="live" id="refresh-label">Refreshes every 30s</div></div>
<p>Source and release downloads at a glance. Each sync updates this page; the last change stays visible until something new arrives.</p></header>
<main><section class="stats" aria-label="Sync overview">
<div class="stat"><span>CONFIGURED REPOSITORIES</span><strong id="total">—</strong></div>
<div class="stat"><span>SYNCING NOW</span><strong id="running">—</strong></div>
<div class="stat"><span>NEED ATTENTION</span><strong id="attention">—</strong></div>
<div class="stat"><span>CHANGED IN THE LAST 24H</span><strong id="changed">—</strong></div></section>
<div id="static-notice" class="notice" hidden><strong>Manage syncs from the local dashboard</strong><p>This file shows the latest saved snapshot. To add a sync, edit asset patterns, change schedules, clean up releases, archive, or remove a sync, double-click <code>Open dashboard.vbs</code> in the application folder in File Explorer. <a href="Open%20dashboard.vbs">Open dashboard launcher</a> (your browser may download the launcher).</p><p>You can also run <code>python .\github_sync.py dashboard --open</code> in PowerShell from the application folder.</p></div>
<div id="notice" class="status-message" role="status" hidden></div>
<div id="notice-warning" class="status-message caution" role="status" hidden></div>
<div class="toolbar"><div><h2>Repository activity</h2><div class="subtitle" id="count"></div></div><div class="controls">
<input id="search" type="search" placeholder="Search names or descriptions…" aria-label="Search names or descriptions">
<select id="filter" aria-label="Filter by status"><option value="all">All statuses</option><option value="active">Active syncs</option><option value="archived">Archived</option><option value="attention">Needs attention</option><option value="running">Syncing now</option><option value="success">Successful</option></select>
<select id="sort" aria-label="Sort repositories"><option value="change">Latest change</option><option value="sync">Latest sync</option><option value="name">Repository A–Z</option><option value="size-desc">Sync size · largest first</option><option value="size-asc">Sync size · smallest first</option></select>
<button type="button" id="add-sync" class="primary" hidden>Add sync</button><button type="button" id="refresh">Refresh</button></div></div>
<div class="table-wrap"><table><thead><tr><th>Repository</th><th>Latest result</th><th title="Source checkout including Git history, plus every local release folder">Sync Size</th><th>Last sync completed</th><th>Last change</th></tr></thead><tbody id="rows"></tbody></table><div id="empty" class="empty" hidden>No repositories match this view.</div></div>
<footer><div id="generated"></div><div class="footer-links"><a href="README.md">Usage guide</a> &nbsp; / &nbsp; <a href="sync.log">Full sync log</a></div></footer>
<p class="note">Times use your browser’s local timezone. “Last change” is when a change was downloaded, not the upstream commit date. Local counts include recognized releases saved from earlier syncs, independently of the current release selection. Archived syncs keep their files and history; their scheduled task is removed. Sync Size is the sum of file sizes in the source checkout (including Git history) and all local release folders. Sizes refresh at most once per minute, and after syncs or cleanup; linked folders are excluded.</p>
</main>
<dialog id="manage-dialog" aria-labelledby="manage-title"><div class="modal-head"><div><div id="manage-brand" class="brand" style="color:var(--green)">MANAGE SYNC</div><h2 id="manage-title"></h2><div id="manage-state" class="modal-info"></div></div><button type="button" id="manage-close" class="close" aria-label="Close management">×</button></div><div class="modal-body"><div id="manage-error" class="problem modal-error" role="alert" hidden></div><details id="manage-task-diagnostics" class="task-diagnostics" hidden><summary>Scheduled task details</summary><p id="manage-task-help"></p><p>Your unsaved edits are still in the form. After reviewing the task’s Actions in Windows Task Scheduler, retry <strong>Save asset patterns</strong>.</p><pre id="manage-task-details"></pre><button type="button" id="manage-copy-task">Copy task details</button><p id="manage-copy-status" role="status" hidden></p><textarea id="manage-copy-fallback" aria-label="Task details to copy" readonly rows="9" hidden></textarea></details><div id="manage-content"></div></div></dialog>
<dialog id="asset-dialog" aria-labelledby="asset-title"><div class="modal-head"><div><div class="brand" style="color:var(--green)">ASSET PREVIEW</div><h2 id="asset-title">Release assets</h2><div id="asset-context" class="modal-info"></div></div><button type="button" id="asset-close" class="close" aria-label="Close asset preview">×</button></div><div class="modal-body"><section class="modal-section"><h3>Try your asset patterns</h3><p class="asset-caption">All release assets are shown below. Assets matching any valid pattern are highlighted as you type. Changes stay in your unsaved form when you close this preview.</p><div id="asset-editor"></div><div id="asset-pattern-warning" class="problem" role="status" hidden></div></section><div id="asset-loading" class="asset-message" role="status" hidden>Loading release assets from the repository…</div><div id="asset-error" class="problem" role="alert" hidden></div><div class="asset-actions"><button type="button" id="asset-retry" hidden>Retry preview</button><button type="button" id="asset-return">Back to sync editor</button></div><div id="asset-summary" class="asset-summary" role="status" hidden></div><div id="asset-results"></div></div></dialog>
<script type="application/json" id="data">__PAYLOAD__</script><script type="application/json" id="management">__MANAGEMENT__</script><script>
let data=JSON.parse(document.getElementById('data').textContent);
const management=JSON.parse(document.getElementById('management').textContent);
const $=id=>document.getElementById(id), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date=s=>s?new Date(s):null;
const exact=s=>s&&Number.isFinite(+new Date(s))?new Date(s).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'Not recorded';
function ago(s){if(!s||!Number.isFinite(+date(s)))return 'Not recorded';const seconds=Math.max(0,(Date.now()-date(s))/1000);if(seconds<60)return 'Just now';if(seconds<3600)return `${Math.floor(seconds/60)}m ago`;if(seconds<86400)return `${Math.floor(seconds/3600)}h ago`;return `${Math.floor(seconds/86400)}d ago`}
const activeFirst=p=>p&&['queued','running'].includes(p.status);
const archived=r=>Boolean(r.config?.archived), statusOf=r=>archived(r)?'archived':activeFirst(r.first_sync)?'running':(r.status||'unknown');
function needs(r){return !archived(r)&&!activeFirst(r.first_sync)&&['warning','failed','interrupted','unknown'].includes(r.status||'unknown')}
const labels={success:'Successful',running:'Syncing',warning:'Warning',failed:'Failed',interrupted:'Interrupted',unknown:'No result yet',archived:'Archived'};
const inventoryOf=r=>r.inventory||{count:null,releases:[],skipped:[]};
function canonicalPublicHost(host){if(typeof host!=='string'||host!==host.toLowerCase()||host.length>253||/[^a-z0-9.-]/.test(host))return false;const labels=host.split('.');if(labels.length<2||labels.some(label=>!/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))||!/^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$/.test(labels.at(-1))||/^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])$/.test(labels[0]))return false;return !['localhost','local','localdomain','internal','intranet','lan','home','home.arpa','test','invalid','example','onion'].some(suffix=>host===suffix||host.endsWith('.'+suffix))}
function canonicalRepositoryPart(value){return typeof value==='string'&&value.length>=1&&value.length<=100&&!/[^A-Za-z0-9_.-]/.test(value)&&value!=='.'&&value!=='..'&&!value.endsWith('.')&&!/^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(value)}
function repositoryInfo(row){const key=String(row.repository||''),parts=key.split('/'),forgejo=parts.length===3,host=forgejo?parts[0]:'github.com',owner=parts[forgejo?1:0],name=parts[forgejo?2:1];const valid=(parts.length===2||forgejo&&host!=='github.com'&&canonicalPublicHost(host))&&canonicalRepositoryPart(owner)&&canonicalRepositoryPart(name)&&(forgejo?/^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(owner):/^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$/.test(owner));if(!valid)return {host:'',owner:'',name:key||'Unknown repository',provider:'unknown',web_url:''};return {host,owner,name,provider:forgejo?'forgejo':'github',web_url:`https://${host}/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`}}
function hostBadgeHTML(row){const remote=repositoryInfo(row);return `<div class="host-badge${remote.provider==='forgejo'?' forgejo':''}" title="${esc(remote.host)}">${remote.provider==='forgejo'?'Forgejo / Gitea · '+esc(remote.host):remote.provider==='github'?'GitHub':'Invalid repository'}</div>`}
function repositoryLinkHTML(row){const remote=repositoryInfo(row);return remote.web_url?`<a href="${esc(remote.web_url)}" target="_blank" rel="noreferrer">${esc(remote.name)}</a>`:esc(remote.name)}
function commitLink(row,revision,label){const text=label??String(revision||'').slice(0,12),url=repositoryInfo(row).web_url;if(!url||!/^[0-9a-f]{7,40}$/i.test(revision||''))return esc(text);return `<a href="${esc(url+'/commit/'+revision)}" target="_blank" rel="noreferrer">${esc(text)}</a>`}
function commitSummaryHTML(row,summary){const match=String(summary||'').match(/^([0-9a-f]{7,40})\s+(.+)$/i);return match?commitLink(row,match[1],summary):esc(summary)}
const localText=r=>inventoryOf(r).count===null?'Local release count unavailable':`${inventoryOf(r).count} release${inventoryOf(r).count===1?'':'s'} synced locally`;
function scheduleOf(r){return r.schedule||{every_minutes:r.config?.every_minutes,start_time:r.config?.start_time}}
function scheduleText(r){if(archived(r))return 'Schedule removed';const s=scheduleOf(r);return s.every_minutes?`Every ${s.every_minutes} minutes${s.start_time?' · starts '+s.start_time:''}`:'Schedule not recorded'}
function formatBytes(bytes){if(!Number.isFinite(bytes)||bytes<0)return 'Unavailable';if(bytes===0)return '0 B';const units=['B','KiB','MiB','GiB','TiB','PiB'],i=Math.min(Math.floor(Math.log(bytes)/Math.log(1024)),units.length-1);return `${(bytes/1024**i).toLocaleString(undefined,{maximumFractionDigits:i===0?0:2})} ${units[i]}`}
function sizeValue(r){return !r.size?.pending&&Number.isFinite(r.size?.total_bytes)?r.size.total_bytes:null}
function newestProgress(current,incoming){if(!current)return incoming||null;if(!incoming)return current;const currentTime=Date.parse(current.updated_at||current.queued_at||'')||0,incomingTime=Date.parse(incoming.updated_at||incoming.queued_at||'')||0;if(incomingTime<currentTime)return current;if(incomingTime===currentTime&&current.id===incoming.id&&!activeFirst(current)&&activeFirst(incoming))return current;return incoming}
function acceptSnapshot(incoming){const previous=new Map(data.rows.map(row=>[row.repository,row.first_sync]));incoming.rows.forEach(row=>{row.first_sync=newestProgress(previous.get(row.repository),row.first_sync)});data=incoming;updateAssetCleanupAvailability()}
function firstSyncHTML(p){if(!p)return '';const active=activeFirst(p),terminal={success:'First sync complete',warning:'First sync finished with warnings',failed:'First sync failed',interrupted:'First sync interrupted'},title=active?'First sync in progress':terminal[p.status]||'First sync';const stage={queued:'Waiting to start',source:'Syncing source files',lfs:'Syncing Git LFS files',releases:'Finding releases',downloads:'Downloading release files',cleanup:'Finishing release cleanup',finished:'Finished'},phase=p.phase||stage[p.stage]||'Preparing sync';const total=Number.isFinite(p.files_total)&&p.files_total>0?p.files_total:null,done=Number.isFinite(p.files_completed)?Math.max(0,p.files_completed):0;const measured=active&&p.stage==='downloads'&&total!==null;const bar=active?`<progress aria-label="First sync progress"${measured?` max="${total}" value="${Math.min(done,total)}"`:''}></progress>`:p.status==='success'?'<progress aria-label="First sync complete" max="1" value="1"></progress>':'';const counts=total!==null?`${done.toLocaleString()} of ${total.toLocaleString()} release files completed`:'';const downloading=active&&p.stage==='downloads'&&p.current_file,knownBytes=Number.isFinite(p.bytes_total)&&p.bytes_total>0,transferred=Number.isFinite(p.bytes_downloaded)?Math.max(0,p.bytes_downloaded):null;const bytes=downloading&&transferred!==null?`${formatBytes(transferred)}${knownBytes?' of '+formatBytes(p.bytes_total):''} in current file`:'';const byteBar=downloading&&knownBytes&&transferred!==null?`<progress aria-label="Current file download progress" max="${p.bytes_total}" value="${Math.min(transferred,p.bytes_total)}"></progress>`:'';return `<strong>${esc(title)}</strong><div class="progress-phase">${esc(phase)}</div>${bar}${counts?`<small>${esc(counts)}</small>`:''}${active&&p.current_release?`<small>Release: ${esc(p.current_release)}</small>`:''}${active&&p.current_file?`<small>${esc(p.current_file)}</small>`:''}${byteBar}${bytes?`<small>${esc(bytes)}</small>`:''}${p.error?`<div class="problem">${esc(p.error)}</div>`:''}${!active&&p.finished_at?`<small>${esc(exact(p.finished_at))}</small>`:''}`}
function paintProgress(){const rows=new Map(data.rows.map(row=>[row.repository,row]));document.querySelectorAll('[data-first-sync]').forEach(element=>{const progress=rows.get(element.dataset.firstSync)?.first_sync;element.hidden=!progress;element.innerHTML=firstSyncHTML(progress)});$('running').textContent=data.rows.filter(row=>statusOf(row)==='running').length;$('attention').textContent=data.rows.filter(needs).length}
function sizeHTML(r){const s=r.size;if(sizeValue(r)===null)return `<div class="sync-size">${s?.pending?'Calculating…':'Unavailable'}</div>`;const total=`${s.complete?'':'≥ '}${formatBytes(s.total_bytes)}`,note=s.complete?(s.stale?'Cached size':''):'Partial size · some files could not be counted';return `<div class="sync-size" title="${esc(s.total_bytes.toLocaleString()+' bytes counted')}">${esc(total)}</div><div class="size-breakdown">Source + Git: ${esc(formatBytes(s.source_bytes))}<br>All releases: ${esc(formatBytes(s.releases_bytes))}</div>${note?`<div class="note" title="${esc((s.errors||[]).join('; '))}">${esc(note)}</div>`:''}`}
function compareRows(a,b,sort){if(sort==='size-desc'||sort==='size-asc'){const x=sizeValue(a),y=sizeValue(b);if(x===null||y===null)return x===y?a.repository.localeCompare(b.repository):x===null?1:-1;return (sort==='size-desc'?y-x:x-y)||a.repository.localeCompare(b.repository)}return (sort==='name'?a.repository.localeCompare(b.repository):((date(sort==='sync'?b.finished_at:b.last_change?.at)||0)-(date(sort==='sync'?a.finished_at:a.last_change?.at)||0)))||a.repository.localeCompare(b.repository)}
function loadUI(){try{return JSON.parse(localStorage.getItem('git-sync-view')||'{}')}catch{return {}}}
let ui=loadUI();['search','filter','sort'].forEach(id=>{if(ui[id])$(id).value=ui[id]});
function saveUI(){ui={...ui,search:$('search').value,filter:$('filter').value,sort:$('sort').value,scroll:scrollY};try{localStorage.setItem('git-sync-view',JSON.stringify(ui))}catch{}}
function folderLink(r,folder,label){return management?`<button type="button" class="link-button" data-folder="${folder}" data-repository="${esc(r.repository)}">${label}</button>`:`<a href="${esc(folder==='source'?r.source_url:r.releases_url)}">${label}</a>`}
function skippedHTML(inv){const skipped=inv.skipped||[];return skipped.length?`<details><summary>${skipped.length} unrecognized folder${skipped.length===1?'':'s'} preserved</summary><p class="note">These folders are excluded from the synced release count and cleanup because their release metadata could not be verified.</p><ul class="skipped-list">${skipped.map(v=>`<li>${esc(typeof v==='string'?v:(v.folder||v.name||v.path||'Unknown folder'))}${v&&typeof v==='object'&&v.reason?': '+esc(v.reason):''}</li>`).join('')}</ul></details>`:''}
function draw(){
const query=$('search').value.toLowerCase(), filter=$('filter').value, sort=$('sort').value;
const all=data.rows;$('total').textContent=all.length;$('running').textContent=all.filter(r=>statusOf(r)==='running').length;$('attention').textContent=all.filter(needs).length;
$('changed').textContent=all.filter(r=>r.last_change?.at&&Date.now()-date(r.last_change.at)<86400000).length;
const rows=all.filter(r=>`${r.repository} ${repositoryInfo(r).host} ${r.description||''}`.toLowerCase().includes(query)&&(filter==='all'||(filter==='attention'?needs(r):filter==='active'?!archived(r):statusOf(r)===filter)));
rows.sort((a,b)=>compareRows(a,b,sort));
$('count').textContent=`${rows.length} of ${all.length} repositories · ${all.filter(archived).length} archived`;$('empty').hidden=rows.length>0;
$('rows').innerHTML=rows.map(r=>{
const remote=repositoryInfo(r), {owner,name}=remote, c=r.last_change, source=c?.source, cfg=r.config||{}, inv=inventoryOf(r);
const status=statusOf(r), notes=r.error?[r.error]:(r.warnings||[]), commits=source?.commits||[], files=c?.downloads||[];
const summary=c?.summary||'No change recorded yet', branch=r.branch||cfg.branch||'Default branch';
const mode=cfg.source_only?'Source only':`Source + latest ${cfg.last} releases`;
const latest=archived(r)?`<div class="note">Files and history kept.<br>Last result: ${esc(labels[r.status]||r.status||'No result yet')}</div>`:r.status==='running'?`<div class="note">${esc(r.phase||'Syncing')}<br>Started ${esc(ago(r.started_at))}</div>`:(r.last_run_changed===false?'<div class="note">No new changes</div>':'');
const details=[...commits.map(v=>`<li>${commitSummaryHTML(r,v)}</li>`),...files.map(v=>`<li>${esc(v)}</li>`)].join('');
return `<tr data-row-repository="${esc(r.repository)}"${highlighted===r.repository?' class="selected-row"':''}><td><div class="owner">${esc(owner)}</div><div class="repo">${repositoryLinkHTML(r)}</div>${hostBadgeHTML(r)}${r.description?`<div class="description">${esc(r.description)}</div>`:''}<div class="meta">${esc(branch)}<br>${esc(mode)}<br>${esc(scheduleText(r))}</div><div class="local-count">${esc(localText(r))}</div>${inv.skipped?.length?`<div class="note">${inv.skipped.length} unrecognized folder${inv.skipped.length===1?'':'s'} preserved</div>`:''}${!r.checkout_exists?'<div class="note">Checkout not present</div>':''}${management?`<div class="row-actions"><button type="button" data-manage="${esc(r.repository)}">Manage sync</button></div>`:''}</td>
<td><span class="badge ${labels[status]?status:'unknown'}">${esc(labels[status]||status)}</span>${latest}</td>
<td>${sizeHTML(r)}</td>
<td><div class="time">${esc(ago(r.finished_at))}</div><span class="exact">${esc(exact(r.finished_at))}</span>${r.status==='failed'||r.status==='interrupted'?`<div class="note">Last successful: ${esc(exact(r.last_success_at))}</div>`:''}${r.imported?'<div class="note">Imported from sync.log</div>':''}</td>
<td><div class="first-sync" data-first-sync="${esc(r.repository)}"${r.first_sync?'':' hidden'}>${firstSyncHTML(r.first_sync)}</div>${c?`<div class="change-time">${esc(exact(c.at))} · ${esc(ago(c.at))}</div>`:''}<div class="change-title">${esc(summary)}</div>
${source?.commits?.length?`<div class="note">${commitSummaryHTML(r,source.commits[0])}</div>`:''}${c?.partial?'<div class="note">Partial changes from a run that later failed.</div>':''}${c?.imported?'<div class="note">Historical log summary; detailed commit tracking starts with the next change.</div>':''}
<details data-repo="${esc(r.repository)}"${ui.open?.includes(r.repository)?' open':''}><summary>Details &amp; folders</summary>${source?.diffstat?`<div>${esc(source.diffstat)}</div>`:''}${source?.before?`<div class="note">${commitLink(r,source.before)} → ${commitLink(r,source.after)}</div>`:''}${details?`<ul>${details}</ul>`:''}${commits.length===5?'<div class="note">Showing the latest five commit subjects.</div>':''}<div class="note">Asset patterns: ${esc((cfg.asset_patterns||[]).join(', '))}</div>${inv.error?`<div class="problem">${esc(inv.error)}</div>`:''}${skippedHTML(inv)}<div class="detail-links">${folderLink(r,'source','Source folder')}${!cfg.source_only||inv.count||inv.skipped?.length?folderLink(r,'releases','Release folder'):''}</div></details>${notes.map(n=>`<div class="problem">${esc(n)}</div>`).join('')}</td></tr>`}).join('');
document.querySelectorAll('details[data-repo]').forEach(d=>d.addEventListener('toggle',()=>{ui.open=[...document.querySelectorAll('details[data-repo][open]')].map(x=>x.dataset.repo);saveUI()}));
$('generated').textContent=`Page updated ${exact(data.generated_at)} · ${Intl.DateTimeFormat().resolvedOptions().timeZone}`;
}
let selected=null, highlighted=null, busy=false, refreshing=false, preview=null, dataVersion=0,progressPolling=false,assetPreview=null,assetRequestId=0,assetCleanupPreview=null;
function notice(message,error=false,warning=''){$('notice').textContent=message;$('notice').className='status-message'+(error?' error':'');$('notice').hidden=false;$('notice-warning').textContent=warning||'';$('notice-warning').hidden=!warning}
function taskDiagnosticsText(diagnostics){const lines=[`Task: ${diagnostics.task_name||'Not recorded'}`,`Repository: ${diagnostics.repository||'Not recorded'}`];if(diagnostics.help)lines.push('',String(diagnostics.help));const actions=Array.isArray(diagnostics.actions)?diagnostics.actions:[];if(!actions.length)lines.push('','No executable actions were available to inspect.');actions.forEach((action,index)=>{lines.push('',`Stored action ${index+1}`,`Program: ${action.command||'(empty)'}`,`Arguments: ${action.arguments||'(empty)'}`,`Reason: ${action.reason||'Could not verify this action'}`)});if(diagnostics.expected)lines.push('','Expected sync action',`Program: ${diagnostics.expected.command||'(empty)'}`,`Arguments: ${diagnostics.expected.arguments||'(empty)'}`);return lines.join('\n')}
function modalError(error){const message=typeof error==='string'?error:(error?.message||''),diagnostics=error?.code==='task_action_conflict'&&error.taskDiagnostics&&typeof error.taskDiagnostics==='object'?error.taskDiagnostics:null;$('manage-error').textContent=message;$('manage-error').hidden=!message;const details=$('manage-task-diagnostics');details.hidden=!diagnostics;details.open=false;$('manage-task-help').textContent=diagnostics?.help||'';$('manage-task-details').textContent=diagnostics?taskDiagnosticsText(diagnostics):'';$('manage-copy-status').hidden=true;$('manage-copy-status').textContent='';$('manage-copy-fallback').hidden=true;$('manage-copy-fallback').value='';if(message)$('manage-error').scrollIntoView({block:'nearest'})}
async function copyTaskDetails(){const text=$('manage-task-details').textContent;if(!text)return;const status=$('manage-copy-status'),fallback=$('manage-copy-fallback');try{if(!navigator.clipboard?.writeText)throw new Error('Clipboard unavailable');await navigator.clipboard.writeText(text);if($('manage-task-details').textContent!==text)return;fallback.hidden=true;status.textContent='Task details copied.';status.hidden=false}catch{if($('manage-task-details').textContent!==text)return;fallback.value=text;fallback.hidden=false;fallback.focus();fallback.select();status.textContent='Automatic copying is unavailable. The task details are selected below; press Ctrl+C to copy them.';status.hidden=false}}
$('manage-copy-task').addEventListener('click',copyTaskDetails);
function setBusy(value){busy=value;document.querySelectorAll('#manage-dialog button,#manage-dialog input,#manage-dialog select,#manage-dialog textarea').forEach(el=>{if(value){el.dataset.wasDisabled=el.disabled?'true':'false';el.disabled=true;if(el.dataset.busyLabel){el.dataset.idleLabel=el.textContent;el.textContent=el.dataset.busyLabel}}else{if('wasDisabled' in el.dataset){el.disabled=el.dataset.wasDisabled==='true';delete el.dataset.wasDisabled}if('idleLabel' in el.dataset){el.textContent=el.dataset.idleLabel;delete el.dataset.idleLabel}}});$('manage-dialog').setAttribute('aria-busy',String(value))}
async function request(action,repository,fields={}){const response=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Token':management.token},body:JSON.stringify({action,repository,...fields})});let result;try{result=await response.json()}catch{throw new Error('The dashboard returned an invalid response. Refresh and try again.')}if(!response.ok||result.error){const error=new Error(result.error||`Request failed (${response.status}).`);error.code=result.code;if(result.code==='task_action_conflict')error.taskDiagnostics=result.task_diagnostics;throw error}return result}
function recordAddedSync(result){if(!result.repository||!result.job)return;let row=data.rows.find(item=>item.repository===result.repository);if(!row){row={repository:result.repository,...repositoryInfo({repository:result.repository}),config:result.job,status:'unknown',inventory:{count:null,releases:[],skipped:[]},size:{pending:true},description:''};data.rows.push(row)}row.first_sync=newestProgress(row.first_sync,result.first_sync)}
async function runAction(action,fields={},success){if(busy)return;modalError('');if(action!=='assets-cleanup-preview')clearAssetCleanupPreview();setBusy(true);dataVersion++;try{const result=await request(action,selected,fields);if(result.data){dataVersion++;acceptSnapshot(result.data)}if(action==='add')recordAddedSync(result);if(result.data||action==='add')draw();if(success)success(result)}catch(error){modalError(error)}finally{setBusy(false);updateAssetCleanupAvailability();pollProgress()}}
function savedAssetPatterns(){return data.rows.find(row=>row.repository===selected)?.config?.asset_patterns||[]}
function patternSignature(patterns){return JSON.stringify([...new Set((patterns||[]).map(value=>String(value).trim().toLowerCase()))].sort())}
function assetPatternsDirty(){return !$('edit-patterns')||patternSignature(rawPatterns('edit'))!==patternSignature(savedAssetPatterns())}
function clearAssetCleanupPreview(){assetCleanupPreview=null;const element=$('assets-cleanup-preview');if(element){element.hidden=true;element.innerHTML=''}}
function updateAssetCleanupAvailability(){const button=$('assets-cleanup-preview-button');if(!button)return;const dirty=assetPatternsDirty();button.disabled=busy||dirty;$('assets-cleanup-save-note').hidden=!dirty;$('assets-cleanup-saved-patterns').textContent=savedAssetPatterns().join(', ');if(assetCleanupPreview&&patternSignature(assetCleanupPreview.patterns)!==patternSignature(savedAssetPatterns()))clearAssetCleanupPreview()}
function assetCleanupFileHTML(asset){return `<li><strong>${esc(asset.name)}</strong><div class="release-folder">${esc(asset.tag)} · ${esc(asset.folder)} · ${esc(formatBytes(asset.size))}</div></li>`}
function assetCleanupPreviewHTML(result){const assets=result.assets||[],skipped=result.skipped||[],count=result.count,patterns=(result.patterns||[]).map(pattern=>`<code>${esc(pattern)}</code>`).join(', ');return `<strong>Will delete ${esc(count)} asset${count===1?'':'s'}</strong><p>${esc(formatBytes(result.bytes))} across ${esc(result.release_count)} release${result.release_count===1?'':'s'}. Using saved patterns: ${patterns||'None'}.</p>${count>0?`<p>These local files no longer match the saved patterns:</p><ol class="release-list" aria-label="Example assets selected for deletion">${assets.slice(0,5).map(assetCleanupFileHTML).join('')}</ol>${assets.length>5?`<details><summary>View all ${esc(count)} assets to delete</summary><ol class="release-list" aria-label="All assets selected for deletion">${assets.map(assetCleanupFileHTML).join('')}</ol></details>`:''}<p>Only the listed files will be deleted. Source archives, release metadata, and unrecognized files are kept.</p><div class="modal-buttons"><button type="button" id="confirm-assets-cleanup" class="danger" data-busy-label="Deleting assets…">Delete ${esc(count)} asset${count===1?'':'s'}</button><button type="button" id="cancel-assets-cleanup">Cancel</button></div>`:'<p>No recognized local release assets need removal. No files will be changed.</p>'}${skipped.length?`<details><summary>${skipped.length} item${skipped.length===1?'':'s'} kept</summary><ul class="skipped-list">${skipped.map(item=>`<li>${esc(item.folder)}${item.name?' / '+esc(item.name):''}${item.reason?': '+esc(item.reason):''}</li>`).join('')}</ul></details>`:''}`}
function showAssetCleanupPreview(result,repository){if(selected!==repository)return;if(assetPatternsDirty()||patternSignature(result.patterns)!==patternSignature(savedAssetPatterns())){clearAssetCleanupPreview();modalError('The saved patterns changed. Refresh the dashboard, then preview Refresh Assets again.');return}assetCleanupPreview={...result,repository};const element=$('assets-cleanup-preview');element.innerHTML=assetCleanupPreviewHTML(result);element.hidden=false;$('confirm-assets-cleanup')?.addEventListener('click',applyAssetCleanup);$('cancel-assets-cleanup')?.addEventListener('click',clearAssetCleanupPreview);element.scrollIntoView({block:'nearest'})}
function previewAssetCleanup(){if(busy)return;clearAssetCleanupPreview();updateAssetCleanupAvailability();if(assetPatternsDirty()){modalError('Save your asset pattern changes before using Refresh Assets.');return}const repository=selected;runAction('assets-cleanup-preview',{},result=>showAssetCleanupPreview(result,repository))}
function finishAssetCleanup(result,repository){clearAssetCleanupPreview();const row=data.rows.find(item=>item.repository===repository);if(result.data&&row&&selected===repository)renderManager(row);else if(!result.data){const previous=data;Promise.resolve().then(()=>refresh()).then(()=>{if(data!==previous&&selected===repository){const updated=data.rows.find(item=>item.repository===repository);if(updated)renderManager(updated)}})}notice(result.message||`Refresh Assets removed ${result.count||0} assets.`,false,result.warning)}
function applyAssetCleanup(){if(busy||!assetCleanupPreview)return;const approved=assetCleanupPreview;if(approved.repository!==selected||assetPatternsDirty()||patternSignature(approved.patterns)!==patternSignature(savedAssetPatterns())){clearAssetCleanupPreview();modalError('The patterns changed. Preview Refresh Assets again before deleting files.');return}if(!(approved.count>0)||typeof approved.token!=='string')return;runAction('assets-cleanup',{token:approved.token},result=>finishAssetCleanup(result,approved.repository))}
function patternRowHTML(value){return `<div class="pattern-row"><label class="field">Asset pattern<input type="text" data-asset-pattern maxlength="200" required value="${esc(value)}" placeholder="e.g. *windows*x64*.zip" autocomplete="off" spellcheck="false"></label><button type="button" data-remove-pattern>Remove</button></div>`}
function patternEditorHTML(prefix,patterns){return `<div id="${prefix}-patterns" class="pattern-list">${(patterns?.length?patterns:['*windows*x64*.zip']).map(patternRowHTML).join('')}</div><div class="modal-buttons"><button type="button" id="${prefix}-add-pattern">Add pattern</button>${prefix!=='asset'?`<button type="button" id="${prefix}-asset-preview">Asset Preview</button>`:''}</div><p class="pattern-help">Match asset filenames using <code>*</code> for any text or <code>?</code> for one character, or enter an exact filename. Matching ignores letter case. Use letters, numbers, dots, hyphens, and underscores. Keep at least one pattern. Open Asset Preview to check filenames, then choose patterns such as <code>*.apk</code> or <code>*.zip</code>.</p><p>Each release’s source ZIP is also downloaded regardless of asset patterns, unless this sync is set to source only.</p>`}
function updatePatternControls(prefix){const list=$(`${prefix}-patterns`),add=$(`${prefix}-add-pattern`),rows=[...list.children];add.disabled=rows.length>=50;add.title=rows.length>=50?'Maximum of 50 asset patterns':'';rows.forEach((row,index)=>{row.querySelector('input').setAttribute('aria-label',`Asset pattern ${index+1}`);const remove=row.querySelector('[data-remove-pattern]');remove.disabled=rows.length===1;remove.setAttribute('aria-label',`Remove asset pattern ${index+1}`)})}
function bindPatternEditor(prefix,onChange=()=>{}){const list=$(`${prefix}-patterns`),add=$(`${prefix}-add-pattern`);list.addEventListener('input',event=>{event.target.setCustomValidity?.('');onChange()});list.addEventListener('click',event=>{const remove=event.target.closest('[data-remove-pattern]');if(busy||!remove||remove.disabled)return;const row=remove.closest('.pattern-row'),next=row.nextElementSibling||row.previousElementSibling;row.remove();updatePatternControls(prefix);next?.querySelector('input').focus();onChange()});add.addEventListener('click',()=>{if(busy||list.children.length>=50)return;list.insertAdjacentHTML('beforeend',patternRowHTML(''));updatePatternControls(prefix);list.lastElementChild.querySelector('input').focus();onChange()});$(`${prefix}-asset-preview`)?.addEventListener('click',()=>openAssetPreview(prefix));updatePatternControls(prefix)}
function rawPatterns(prefix){return [...$(`${prefix}-patterns`).querySelectorAll('[data-asset-pattern]')].map(input=>input.value)}
function patternMatchers(patterns){const invalid=[],matchers=[];patterns.forEach((raw,index)=>{const pattern=raw.trim();if(!pattern||pattern.length>200||!/^[A-Za-z0-9.*?_-]+$/.test(pattern)){invalid.push(index+1);return}const expression=[...pattern.toLowerCase()].map(character=>character==='*'?'.*':character==='?'?'.':character.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('');const matcher=new RegExp('^'+expression+'(?![\\s\\S])','su');matchers.push({test:value=>matcher.test(String(value).toLowerCase())})});return {invalid,matchers}}
function syncPreviewPatterns(){if(!assetPreview)return;const values=rawPatterns('asset'),parent=$(`${assetPreview.prefix}-patterns`);if(parent){parent.innerHTML=values.map(patternRowHTML).join('');updatePatternControls(assetPreview.prefix);if(assetPreview.prefix==='edit'){$('patterns-status').hidden=true;$('patterns-warning').hidden=true;clearAssetCleanupPreview();updateAssetCleanupAvailability()}}renderAssetMatches()}
function renderAssetMatches(){if(!assetPreview)return;const {invalid,matchers}=patternMatchers(rawPatterns('asset'));$('asset-pattern-warning').hidden=!invalid.length;$('asset-pattern-warning').textContent=invalid.length?`Pattern ${invalid.join(', ')} ${invalid.length===1?'is':'are'} blank or invalid. Only valid patterns are highlighted; fix these before saving.`:'';const payload=assetPreview.payload;if(!payload){$('asset-summary').hidden=true;$('asset-results').innerHTML='';return}const releases=payload.releases||[];let assetCount=0,matchedCount=0,knownBytes=0;const html=releases.map(release=>{const assets=release.assets||[];const items=assets.map(asset=>{assetCount++;const matches=matchers.some(pattern=>pattern.test(asset.name||''));if(matches){matchedCount++;if(Number.isFinite(asset.size))knownBytes+=asset.size}return `<div class="asset-item${matches?' match':''}"><div class="asset-name">${esc(asset.name)}</div><div class="asset-mark">${matches?'Matches a pattern':'Not matched'}</div><div class="asset-size">${Number.isFinite(asset.size)?esc(formatBytes(asset.size)):'Size unavailable'}</div></div>`}).join('');const archive=release.source_archive;return `<section class="asset-release"><div class="asset-release-head"><h3>${esc(release.tag)}${release.prerelease?' · prerelease':''}</h3>${release.name&&release.name!==release.tag?`<p>${esc(release.name)}</p>`:''}<p>${esc(exact(release.published_at))} · ${assets.length.toLocaleString()} release assets</p></div>${items}${archive?`<div class="asset-item source-archive"><div class="asset-name">${esc(archive.name)}</div><div class="asset-mark">Always included · source ZIP</div><div class="asset-size">Size available when downloaded</div></div>`:''}${!assets.length?'<div class="asset-item">No uploaded release assets.</div>':''}</section>`}).join('');$('asset-summary').hidden=false;if(payload.source_only){$('asset-summary').textContent=payload.message||'Source-only sync: no release assets or source ZIPs will be downloaded.'}else if(!releases.length){$('asset-summary').textContent=payload.message||'No published releases were found for this repository.'}else{$('asset-summary').textContent=`${matchedCount.toLocaleString()} of ${assetCount.toLocaleString()} assets match across ${releases.length.toLocaleString()} releases · ${formatBytes(knownBytes)} in matching assets, plus each release’s source ZIP.`}$('asset-results').innerHTML=html}
async function fetchAssetPreview(){if(!assetPreview)return;const context=assetPreview,requestId=++assetRequestId;context.payload=null;$('asset-loading').hidden=false;$('asset-error').hidden=true;$('asset-retry').hidden=true;renderAssetMatches();try{const response=await fetch('/api/asset-preview',{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Token':management.token},body:JSON.stringify(context.params)});let payload;try{payload=await response.json()}catch{throw new Error('The asset preview returned an invalid response. Try again.')}if(!response.ok||payload.error)throw new Error(payload.error||`Preview failed (${response.status}).`);if(assetPreview!==context||requestId!==assetRequestId)return;context.payload=payload;renderAssetMatches()}catch(error){if(assetPreview!==context||requestId!==assetRequestId)return;$('asset-error').textContent=error.message;$('asset-error').hidden=false;$('asset-retry').hidden=false}finally{if(assetPreview===context&&requestId===assetRequestId)$('asset-loading').hidden=true}}
function openAssetPreview(prefix){if(busy||!management)return;const row=data.rows.find(item=>item.repository===selected),isNew=prefix==='new',repository=isNew?$('new-repository').value.trim():selected,limitInput=$(isNew?'new-release-limit':'release-limit');if(!repository){$('new-repository').reportValidity();return}if(!limitInput.reportValidity())return;const params={repository,last:Number(limitInput.value),source_only:isNew?$('new-source-only').checked:Boolean(row?.config?.source_only)};assetPreview={prefix,params,payload:null};$('asset-title').textContent=repository;$('asset-context').textContent=`${params.source_only?'Source-only sync':`Latest ${params.last} published releases`}${row&&archived(row)?' · Archived: preview shows what will download when resumed.':''}`;$('asset-editor').innerHTML=patternEditorHTML('asset',rawPatterns(prefix));bindPatternEditor('asset',syncPreviewPatterns);$('asset-error').hidden=true;$('asset-retry').hidden=true;$('asset-results').innerHTML='';$('asset-dialog').showModal();fetchAssetPreview()}
function closeAssetPreview(){if(assetPreview)syncPreviewPatterns();assetPreview=null;assetRequestId++;$('asset-dialog').close()}
$('asset-close').addEventListener('click',closeAssetPreview);$('asset-return').addEventListener('click',closeAssetPreview);$('asset-retry').addEventListener('click',fetchAssetPreview);$('asset-dialog').addEventListener('cancel',event=>{event.preventDefault();closeAssetPreview()});
function readPatterns(prefix){const inputs=[...$(`${prefix}-patterns`).querySelectorAll('[data-asset-pattern]')];if(!inputs.length){modalError('Add at least one asset pattern.');return null}for(const input of inputs){const valid=/^[A-Za-z0-9.*?_-]+$/.test(input.value.trim());input.setCustomValidity(valid?'':'Enter an asset pattern using letters, numbers, dots, hyphens, underscores, * or ?.');if(!valid){input.reportValidity();return null}}return inputs.map(input=>input.value.trim())}
function openAddSync(){if(busy||!management)return;clearAssetCleanupPreview();selected=null;preview=null;modalError('');$('manage-brand').textContent='ADD SYNC';$('manage-title').textContent='Add a repository';$('manage-state').textContent='Set its schedule and choose which release assets to download.';$('manage-content').innerHTML=`<form id="add-sync-form"><section class="modal-section"><h3>Repository</h3><label class="field wide-field">Repository<input id="new-repository" type="text" required placeholder="owner/repository or a repository URL" autocomplete="off" spellcheck="false"><small>Use owner/repository for GitHub, or paste a full public HTTPS repository URL such as https://git.example.org/owner/repository. Forgejo / Gitea compatibility is checked automatically.</small></label><label class="field wide-field">Branch (optional)<input id="new-branch" type="text" placeholder="Default branch" autocomplete="off" spellcheck="false"><small>Leave blank to follow the repository’s default branch.</small></label></section><section class="modal-section"><h3>Sync schedule & release selection</h3><div class="form-grid"><label class="field">Every (minutes)<input id="new-every-minutes" type="number" min="5" max="1440" step="1" required value="15"><small>5–1440 minutes</small></label><label class="field">Start time (optional)<input id="new-start-time" type="time"><small>PC local time · HH:MM</small></label><label class="field">Latest releases to sync<input id="new-release-limit" type="number" min="1" max="100" step="1" required value="5"><small>1–100 releases</small></label></div><p>A start time anchors the repeated schedule. Leave it blank to start the schedule shortly.</p><label class="check-field"><input id="new-source-only" type="checkbox"><span>Sync source only</span></label><p id="new-source-note" hidden>Release downloads are skipped. The release limit and asset patterns are saved for future release syncing; existing files are kept.</p></section><section class="modal-section"><h3>Asset patterns</h3>${patternEditorHTML('new',['*windows*x64*.zip'])}<p>Assets matching any of these patterns will be downloaded. Existing downloaded assets are kept when the patterns change.</p></section><section class="modal-section"><label class="check-field"><input id="new-run-now" type="checkbox" checked><span>Run the first sync now</span></label><p>The first sync runs in the background. Its progress and results appear on the dashboard.</p><div class="modal-buttons"><button type="submit" class="primary" data-busy-label="Adding sync…">Add sync</button><button type="button" id="add-sync-cancel">Cancel</button></div></section></form>`;bindPatternEditor('new');$('new-source-only').addEventListener('change',()=>{$('new-source-note').hidden=!$('new-source-only').checked});$('add-sync-cancel').addEventListener('click',()=>{if(!busy)closeManager()});$('add-sync-form').addEventListener('submit',event=>{event.preventDefault();if(busy)return;const patterns=readPatterns('new');if(!patterns)return;const repository=$('new-repository').value.trim(),fields={repository,branch:$('new-branch').value.trim()||null,every_minutes:Number($('new-every-minutes').value),start_time:$('new-start-time').value,last:Number($('new-release-limit').value),source_only:$('new-source-only').checked,run_now:$('new-run-now').checked,asset_patterns:patterns};runAction('add',fields,result=>{highlighted=result.repository||repository;$('search').value='';$('filter').value='all';saveUI();draw();closeManager();notice(result.message||`${highlighted}: sync added.`,false,result.warning)})});$('manage-dialog').showModal();$('new-repository').focus()}
function renderManager(r){
preview=null;clearAssetCleanupPreview();modalError('');const cfg=r.config||{}, inv=inventoryOf(r), schedule=scheduleOf(r), isArchived=archived(r);
$('manage-brand').textContent='MANAGE SYNC';$('manage-title').textContent=r.repository;$('manage-state').textContent=`${isArchived?'Archived · scheduled task removed':'Active sync'} · ${localText(r)}`;
$('manage-content').innerHTML=`<section class="modal-section"><h3>${isArchived?'Resume this sync':'Sync schedule & release selection'}</h3><p>${isArchived?'Set the schedule below to resume automatic syncing. Existing files and saved history remain available.':'Change how often this repository syncs and how many recent releases it checks.'}</p><form id="settings-form"><div class="form-grid"><label class="field">Every (minutes)<input id="every-minutes" type="number" min="5" max="1440" step="1" required value="${esc(schedule.every_minutes??'')}" placeholder="5–1440"><small>5–1440 minutes</small></label><label class="field">Start time (optional)<input id="start-time" type="time" value="${esc(schedule.start_time||'')}"><small>PC local time · HH:MM</small></label><label class="field">Latest releases to sync<input id="release-limit" type="number" min="1" max="100" step="1" required value="${esc(cfg.last??'')}"><small>1–100 releases</small></label></div>${!schedule.every_minutes?`<p>Current interval is unavailable. Enter an interval to ${isArchived?'resume syncing':'save a new schedule'}.</p>`:''}${schedule.error?`<p>${esc(schedule.error)}</p>`:''}<p>A start time anchors the repeated schedule. Leave it blank to start the new schedule shortly.</p>${cfg.source_only?'<p>This sync is set to source only. The release selection is saved for future release syncing; cleanup below remains available for existing releases.</p>':'<p>This selection controls future syncs. To remove releases already on this PC, use cleanup below.</p>'}<button class="primary" type="submit">${isArchived?'Resume sync':'Save settings'}</button></form></section>
<section class="modal-section"><h3>Asset patterns</h3><p>Choose which release assets future syncs download. Saving patterns keeps already downloaded files${isArchived?' and leaves this sync archived':''}.</p>${cfg.source_only?'<p>This sync is set to source only. These patterns are saved but ignored while release downloads are disabled.</p>':''}<form id="patterns-form">${patternEditorHTML('edit',cfg.asset_patterns)}<button type="submit" class="primary" data-busy-label="Saving patterns…">Save asset patterns</button><div id="patterns-status" class="inline-status" role="status" hidden></div><div id="patterns-warning" class="problem" role="status" hidden></div></form></section>
<section class="modal-section"><h3>Refresh Assets</h3><p>Manually remove local release assets that no longer match your saved asset patterns. Preview the exact files before deleting them. Source archives, release metadata, and unrecognized files are kept.</p><p>Saved patterns: <code id="assets-cleanup-saved-patterns">${esc((cfg.asset_patterns||[]).join(', '))}</code></p><p>This action does not download anything. New matching assets are downloaded during a later normal release sync${isArchived?(cfg.source_only?', after this sync is resumed and release syncing is enabled':', after this sync is resumed'):cfg.source_only?', if release syncing is enabled':''}.</p><p id="assets-cleanup-save-note" class="modal-info" hidden>Save your asset pattern changes before using Refresh Assets.</p><button type="button" id="assets-cleanup-preview-button" data-busy-label="Checking local assets…">Refresh Assets</button><div id="assets-cleanup-preview" class="cleanup-preview" hidden></div></section>
<section class="modal-section"><h3>Local releases</h3><p><strong>${esc(localText(r))}</strong>. The count includes recognized releases from previous syncs, beyond the current latest-release selection.</p>${inv.error?`<div class="problem">${esc(inv.error)}</div>`:''}${skippedHTML(inv)}${inv.count>0?`<form id="cleanup-form"><div class="cleanup-controls"><label class="field">Oldest releases to remove<input id="cleanup-count" type="number" min="1" max="${esc(inv.count)}" step="1" required value="1"></label><button type="submit">Preview cleanup</button></div></form><p>Review the exact releases before deleting their folders and downloaded files. The source checkout is kept. An active sync may download a removed release again if it is still in the latest-release selection.</p>`:'<p>No recognized local releases are available to clean up.</p>'}<div id="cleanup-preview" class="cleanup-preview" hidden></div></section>
<section class="modal-section"><h3>${isArchived?'Archived sync':'Stop automatic syncing'}</h3><p>${isArchived?'This sync stays on the dashboard with its saved history. Its files remain on this PC, and cleanup stays available.':'Archive keeps this repository on the dashboard and removes its scheduled task. Source files, downloaded releases, and saved history remain on this PC.'}</p><div class="modal-buttons">${!isArchived?'<button type="button" id="archive-sync">Archive sync</button>':''}<button type="button" id="remove-sync" class="danger">Remove from dashboard</button></div><p class="saved-history">Removing a sync removes its scheduled task and dashboard entry. All source files and downloaded releases remain on this PC.</p></section>`;
$('settings-form').addEventListener('submit',event=>{event.preventDefault();if(busy)return;const fields={every_minutes:Number($('every-minutes').value),last:Number($('release-limit').value),start_time:$('start-time').value};runAction(isArchived?'resume':'settings',fields,result=>{closeManager();notice(result.message||`${r.repository}: ${isArchived?'sync resumed':'settings saved'}.`)})});
bindPatternEditor('edit',()=>{$('patterns-status').hidden=true;$('patterns-warning').hidden=true;clearAssetCleanupPreview();updateAssetCleanupAvailability()});$('patterns-form').addEventListener('submit',event=>{event.preventDefault();if(busy)return;clearAssetCleanupPreview();const patterns=readPatterns('edit');if(!patterns)return;$('patterns-status').hidden=true;$('patterns-warning').hidden=true;runAction('patterns',{asset_patterns:patterns},result=>{if(!result.data){const updated=data.rows.find(row=>row.repository===selected);if(updated){updated.config={...updated.config,asset_patterns:result.asset_patterns||patterns};draw()}}$('patterns-status').textContent=result.message||'Asset patterns saved. Existing downloads are kept.';$('patterns-status').hidden=false;$('patterns-warning').textContent=result.warning||'';$('patterns-warning').hidden=!result.warning;updateAssetCleanupAvailability()})});
$('assets-cleanup-preview-button').addEventListener('click',previewAssetCleanup);updateAssetCleanupAvailability();
$('cleanup-form')?.addEventListener('submit',event=>{event.preventDefault();runAction('cleanup-preview',{count:Number($('cleanup-count').value)},result=>{preview=result;$('cleanup-preview').hidden=false;$('cleanup-preview').innerHTML=`<strong>Delete ${esc(result.count)} oldest release${result.count===1?'':'s'}?</strong><p>${esc(result.remaining)} recognized release${result.remaining===1?'':'s'} will remain locally. These complete release folders will be deleted:</p><ol class="release-list">${(result.releases||[]).map(v=>`<li><strong>${esc(v.tag)}</strong><div class="release-folder">${esc(v.folder)}${v.published_at?' · published '+esc(exact(v.published_at)):''}</div></li>`).join('')}</ol><div class="modal-buttons"><button type="button" id="confirm-cleanup" class="danger">Delete these ${esc(result.count)} releases</button><button type="button" id="cancel-cleanup">Cancel cleanup</button></div>`;$('confirm-cleanup').addEventListener('click',()=>{if(!preview)return;const approved=preview;runAction('cleanup',{count:approved.count,token:approved.token},result=>{const updated=data.rows.find(x=>x.repository===selected);if(updated)renderManager(updated);notice(result.message||`${approved.count} local releases removed.`)})});$('cancel-cleanup').addEventListener('click',()=>{preview=null;$('cleanup-preview').hidden=true});$('cleanup-preview').scrollIntoView({block:'nearest'})})});
$('cleanup-count')?.addEventListener('input',()=>{preview=null;$('cleanup-preview').hidden=true});
$('archive-sync')?.addEventListener('click',()=>{if(!busy&&confirm(`Archive ${r.repository}?\n\nIts scheduled task will be removed. The source checkout and all releases stay on this PC. This sync stays on the dashboard as Archived, with cleanup still available.`))runAction('archive',{},result=>{closeManager();notice(result.message||`${r.repository} archived. Files kept.`)})});
$('remove-sync').addEventListener('click',()=>{if(!busy&&confirm(`Remove ${r.repository} from the dashboard?\n\nIts scheduled task and dashboard entry will be removed. All source files and downloaded releases stay untouched on this PC.\n\nChoose Cancel and Archive instead if you want to keep it on the dashboard.`))runAction('remove',{},result=>{closeManager();notice(result.message||`${r.repository} removed from dashboard. Files kept.`)})});
}
function openManager(repository){if(busy)return;const row=data.rows.find(r=>r.repository===repository);if(!row)return;selected=repository;renderManager(row);$('manage-dialog').showModal()}
function closeManager(){clearAssetCleanupPreview();$('manage-dialog').close();selected=null;preview=null}
$('manage-close').addEventListener('click',()=>{if(!busy)closeManager()});$('manage-dialog').addEventListener('cancel',event=>{if(busy)event.preventDefault();else{clearAssetCleanupPreview();selected=null;preview=null}});
$('manage-content').addEventListener('input',()=>{if(selected){clearAssetCleanupPreview();updateAssetCleanupAvailability()}});
$('rows').addEventListener('click',async event=>{const manage=event.target.closest('[data-manage]');if(manage){openManager(manage.dataset.manage);return}const folder=event.target.closest('[data-folder]');if(folder&&!folder.disabled){folder.disabled=true;try{await request('open-folder',folder.dataset.repository,{folder:folder.dataset.folder})}catch(error){notice(error.message,true)}finally{folder.disabled=false}}});
async function refresh(){if(!management){saveUI();location.reload();return}if(refreshing||busy)return;refreshing=true;const version=dataVersion;$('refresh').disabled=true;try{const response=await fetch('/api/status',{headers:{'X-Dashboard-Token':management.token},cache:'no-store'});const result=await response.json();if(!response.ok||result.error)throw new Error(result.error||'Unable to refresh dashboard.');if(version===dataVersion){acceptSnapshot(result);draw();$('refresh-label').textContent=data.rows.some(row=>activeFirst(row.first_sync))?'First sync progress every 2s':'Refreshes every 30s'}}catch(error){if(version===dataVersion){$('refresh-label').textContent='Refresh unavailable';notice(`Could not refresh: ${error.message}`,true)}}finally{refreshing=false;$('refresh').disabled=false}}
async function pollProgress(){if(!management||busy||progressPolling||!data.rows.some(row=>activeFirst(row.first_sync)))return;progressPolling=true;const version=dataVersion;try{const response=await fetch('/api/progress',{headers:{'X-Dashboard-Token':management.token},cache:'no-store'});const result=await response.json();if(!response.ok||result.error)throw new Error(result.error||'Progress unavailable');if(version!==dataVersion)return;let finished=false;data.rows.forEach(row=>{if(!Object.prototype.hasOwnProperty.call(result.progress||{},row.repository))return;const previous=row.first_sync;row.first_sync=newestProgress(previous,result.progress[row.repository]);if(activeFirst(previous)&&row.first_sync&&!activeFirst(row.first_sync))finished=true});paintProgress();$('refresh-label').textContent=data.rows.some(row=>activeFirst(row.first_sync))?'First sync progress every 2s':'Refreshes every 30s';if(finished)refresh()}catch(error){if(version===dataVersion)$('refresh-label').textContent='Progress temporarily unavailable · retrying'}finally{progressPolling=false}}
['search','filter','sort'].forEach(id=>$(id).addEventListener('input',()=>{saveUI();draw()}));$('refresh').addEventListener('click',refresh);$('add-sync').addEventListener('click',openAddSync);
$('static-notice').hidden=Boolean(management);$('add-sync').hidden=!management;draw();if(ui.scroll)requestAnimationFrame(()=>scrollTo(0,ui.scroll));setInterval(refresh,30000);setInterval(pollProgress,2000);pollProgress();
</script></body></html>"""
