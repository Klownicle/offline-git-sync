"""On-demand, loopback-only dashboard controls. No service or scheduled task."""

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

import github_dashboard as dashboard
import github_management as management
import github_asset_preview as asset_preview
import github_progress as progress
from github_sources import parse_repository


IDLE_SECONDS = 600


def code_version(directory=None):
    """Fingerprint the helper's code, including optional files added by upgrades."""
    directory = Path(directory) if directory is not None else Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ("github_dashboard.py", "github_management.py", "github_storage.py",
                 "github_dashboard_server.py", "github_sync.py", "github_progress.py",
                 "github_asset_preview.py", "github_sources.py", "github_asset_history.py", "github_forgejo.py"):
        digest.update(name.encode("utf-8") + b"\0")
        try:
            content = (directory / name).read_bytes()
        except FileNotFoundError:
            if name not in {"github_storage.py", "github_progress.py", "github_asset_preview.py", "github_sources.py", "github_asset_history.py", "github_forgejo.py"}:
                raise
            content = b"<not-installed>"
        digest.update(content + b"\0")
    return digest.hexdigest()


# Capture once: an old process must not report the new files' version after an
# upgrade, because its imported Python modules still contain the old code.
CODE_VERSION = code_version()


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, base, address=("127.0.0.1", 0)):
        if address[0] != "127.0.0.1":
            raise ValueError("The dashboard must bind to 127.0.0.1")
        self.base = Path(base).resolve()
        self.token = secrets.token_urlsafe(32)
        self.instance = secrets.token_hex(16)
        self.version = CODE_VERSION
        self.last_request = time.monotonic()
        self.schedule_cache = {}
        self.cache_lock = threading.Lock()
        self.action_lock = threading.Lock()
        self.preview_lock = threading.Lock()
        self.active_lock = threading.Lock()
        self.active_requests = 0
        super().__init__(address, DashboardHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    def process_request_thread(self, request, client_address):
        with self.active_lock:
            self.active_requests += 1
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.active_lock:
                self.active_requests -= 1
                self.last_request = time.monotonic()

    def snapshot(self):
        data = dashboard.snapshot(self.base)
        # Task Scheduler may be unavailable. Read in a bounded pool and cache
        # results so ordinary status refreshes do not start dozens of processes.
        def schedule(row):
            repo, job = row["repository"], row["config"]
            signature = json.dumps(job, sort_keys=True)
            with self.cache_lock:
                cached = self.schedule_cache.get(repo)
            if cached and cached[0] == signature and time.monotonic() - cached[1] < 60:
                return cached[2]
            try:
                result = management.get_schedule(repo, job)
            except Exception as error:
                result = {"every_minutes": job.get("every_minutes"),
                          "start_time": job.get("start_time"), "error": str(error)}
            with self.cache_lock:
                self.schedule_cache[repo] = (signature, time.monotonic(), result)
            return result
        with ThreadPoolExecutor(max_workers=4) as pool:
            for row, result in zip(data["rows"], pool.map(schedule, data["rows"])):
                row["schedule"] = result
        return data

    def progress_snapshot(self):
        """Read small progress files without scanning downloads or Task Scheduler."""
        config = self.base / "sync-config.json"
        jobs = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
        return {"generated_at": dashboard.now(), "progress": {
            repository: progress.snapshot(self.base, repository) for repository in jobs}}

    def asset_preview(self, body):
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object")
        return asset_preview.preview(body.get("repository"), body.get("last"),
                                     body.get("source_only", False))

    def action(self, body):
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object")
        action, repository = body.get("action"), body.get("repository")
        if not isinstance(action, str) or not isinstance(repository, str):
            raise ValueError("An action and repository are required")
        import github_sync as sync
        # Adding has its own strict repository validation and duplicate checks.
        # Other actions continue to operate on a configured repository only.
        if action != "add" and repository not in sync.load_config():
            raise ValueError("This sync is no longer configured. Refresh the dashboard.")
        if action == "add":
            branch, start_time = body.get("branch"), body.get("start_time")
            result = management.add_sync(
                repository, body.get("asset_patterns"), body.get("every_minutes"), body.get("last"),
                branch=None if branch == "" else branch,
                source_only=body.get("source_only", False),
                start_time=None if start_time == "" else start_time,
                run_now=body.get("run_now", True))
            repository = result["repository"]
            message = ("Sync added. The first sync was launched in the background."
                       if result.get("started") else "Sync added. Scheduled syncing is enabled.")
        elif action == "patterns":
            result = management.update_asset_patterns(repository, body.get("asset_patterns"))
            message = "Asset patterns saved for future syncs. Existing downloads are kept."
        elif action in {"settings", "resume"}:
            operation = management.update_settings if action == "settings" else management.resume_sync
            result = operation(repository, body.get("every_minutes"), body.get("last"),
                               body.get("start_time") or None)
            message = "Sync settings saved." if action == "settings" else "Sync resumed."
        elif action == "archive":
            result = management.archive_sync(repository)
            message = "Sync archived; its scheduled task was removed. Local files are preserved."
        elif action == "remove":
            result = management.remove_sync(repository)
            message = "Sync removed from the dashboard. Local files are preserved."
        elif action == "cleanup-preview":
            return management.cleanup_preview(repository, body.get("count"))
        elif action == "cleanup":
            result = management.cleanup_releases(repository, body.get("count"), body.get("token"))
            message = "Release cleanup completed."
        elif action == "assets-cleanup-preview":
            return management.cleanup_assets_preview(repository)
        elif action == "assets-cleanup":
            try:
                result = management.cleanup_assets(repository, body.get("token"))
            except management.AssetCleanupError as error:
                # Deletion may already have committed for some files. Return
                # those results and a warning so the UI refreshes its size/counts.
                result = {**error.result, "warning": str(error)}
                message = f"Refresh Assets stopped after removing {result['count']} assets."
            else:
                count = result["count"]
                message = (f"Assets refreshed. Removed {count} nonmatching asset"
                           f"{'s' if count != 1 else ''}." if count else
                           "No recognized downloaded assets need removal for the saved patterns.")
        elif action == "open-folder":
            folder = body.get("folder")
            if folder not in {"source", "releases"}:
                raise ValueError("Unknown folder")
            descriptor = parse_repository(repository, allow_url=False)
            path = descriptor.source_path(self.base) if folder == "source" else descriptor.releases_path(self.base)
            if not path.is_dir() or not path.resolve().is_relative_to(self.base):
                raise ValueError("The local folder is unavailable")
            os.startfile(path)
            return {"message": "Folder opened."}
        else:
            raise ValueError("Unknown dashboard action")
        with self.cache_lock:
            self.schedule_cache.pop(repository, None)
        response = {**(result if isinstance(result, dict) else {}), "message": message}
        try:
            response["data"] = self.snapshot()
        except Exception as error:
            # The mutation has already committed; avoid presenting a successful
            # add/save as failed and prompting a duplicate retry.
            warning = f"Saved successfully, but the dashboard could not refresh: {error}. Refresh to reload it."
            response["warning"] = " ".join(filter(None, [response.get("warning"), warning]))
        return response


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "GitSyncDashboard"

    def log_message(self, *args):
        pass

    def respond(self, status, value, content_type="application/json; charset=utf-8"):
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=True)
        content = value.encode("utf-8") if isinstance(value, str) else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
                         "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(content)

    def allowed(self, mutation=False):
        expected_host = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected_host:
            self.respond(403, {"error": "Invalid dashboard host"})
            return False
        origin = self.headers.get("Origin")
        if (origin is not None and origin != self.server.origin) or self.headers.get(
                "Sec-Fetch-Site") == "cross-site":
            self.respond(403, {"error": "Cross-site dashboard requests are not allowed"})
            return False
        if mutation:
            token = self.headers.get("X-Dashboard-Token", "")
            if (origin != self.server.origin or not token.isascii()
                    or not secrets.compare_digest(token, self.server.token)):
                self.respond(403, {"error": "Dashboard session expired. Reopen the dashboard."})
                return False
        self.server.last_request = time.monotonic()
        return True

    def do_GET(self):
        if not self.allowed():
            return
        try:
            if self.path in {"/", "/dashboard.html"}:
                self.respond(200, dashboard.html_page(self.server.snapshot(),
                             {"token": self.server.token}), "text/html; charset=utf-8")
            elif self.path == "/api/status":
                self.respond(200, self.server.snapshot())
            elif self.path == "/api/progress":
                self.respond(200, self.server.progress_snapshot())
            elif self.path == "/health":
                self.respond(200, {"instance": self.server.instance, "pid": os.getpid(),
                                   "version": self.server.version})
            elif self.path in {"/README.md", "/sync.log"}:
                # Explicit allowlist; never serve checkouts, config, state or paths.
                with (self.server.base / self.path[1:]).open("rb") as stream:
                    if self.path == "/sync.log":
                        stream.seek(0, 2)
                        stream.seek(max(0, stream.tell() - 1024 * 1024))
                    content = stream.read(1024 * 1024)
                self.respond(200, content, "text/plain; charset=utf-8")
            else:
                self.respond(404, {"error": "Not found"})
        except Exception as error:
            self.respond(500, {"error": str(error)})

    def do_POST(self):
        if not self.allowed(mutation=True):
            return
        if self.path not in {"/api/action", "/api/asset-preview"}:
            self.respond(404, {"error": "Not found"})
            return
        if self.headers.get_content_type() != "application/json":
            self.respond(415, {"error": "Expected application/json"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 < size <= 16384:
                raise ValueError("Invalid request size")
            self.connection.settimeout(10)
            content = self.rfile.read(size)
            if len(content) != size:
                raise ValueError("Incomplete request")
            body = json.loads(content)
            if self.path == "/api/asset-preview":
                # A slow GitHub metadata request must not block local management
                # actions. Only explicit previews contact GitHub.
                if not self.server.preview_lock.acquire(blocking=False):
                    self.respond(409, {"error": "An asset preview is loading. Try again shortly."})
                    return
                try:
                    result = self.server.asset_preview(body)
                finally:
                    self.server.preview_lock.release()
                self.respond(200, result)
                return
            # Serializes dashboard mutations only; regular sync locks remain the
            # authority for conflicts with scheduled/manual sync processes.
            if not self.server.action_lock.acquire(blocking=False):
                self.respond(409, {"error": "Another dashboard action is in progress. Try again shortly."})
                return
            try:
                result = self.server.action(body)
            finally:
                self.server.action_lock.release()
            self.respond(200, result)
        except management.TaskActionConflict as error:
            self.respond(409, {"error": str(error), "code": "task_action_conflict",
                               "task_diagnostics": error.diagnostics})
        except (ValueError, TypeError, KeyError) as error:
            self.respond(400, {"error": str(error)})
        except dashboard.LockBusy:
            self.respond(409, {"error": "This sync is running or being edited. Try again after it finishes."})
        except Exception as error:
            self.respond(500, {"error": str(error)})


def endpoint_path(base):
    return Path(base) / ".sync-state" / "dashboard-server.json"


def read_endpoint(base):
    info = json.loads(endpoint_path(base).read_text(encoding="utf-8"))
    if (not isinstance(info, dict) or type(info.get("port")) is not int
            or not 1024 <= info["port"] <= 65535
            or type(info.get("pid")) is not int or info["pid"] <= 0
            or not isinstance(info.get("process_identity"), str) or not info["process_identity"]
            or not isinstance(info.get("instance"), str) or not info["instance"]):
        raise ValueError("Invalid dashboard helper identity")
    return info


def verified_endpoint(base):
    try:
        info = read_endpoint(base)
        if dashboard.process_identity(info["pid"]) != info["process_identity"]:
            return None
        url = f"http://127.0.0.1:{info['port']}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + "/health", timeout=2) as response:
            health = json.load(response)
        if (not isinstance(health, dict) or health.get("instance") != info["instance"]
                or type(health.get("pid")) is not int or health["pid"] != info["pid"]
                or read_endpoint(base) != info):
            return None
        return info, url, health
    except (OSError, ValueError, KeyError, TypeError):
        return None


def running_url(base):
    running = verified_endpoint(base)
    return running[1] if running else None


def stop_outdated_helper(base, expected):
    """Stop only this verified helper, while no management operation can mutate."""
    try:
        with dashboard.lock(base / ".sync-state" / "config.lock", timeout=0):
            running = verified_endpoint(base)
            if not running or running[0] != expected:
                raise RuntimeError("The dashboard helper identity changed. Reopen the dashboard.")
            pid = expected["pid"]
            if pid == os.getpid() or dashboard.process_identity(pid) != expected["process_identity"]:
                raise RuntimeError("Cannot safely verify the dashboard helper to restart it.")
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError as error:
                raise RuntimeError("The outdated dashboard helper could not be stopped. "
                                   "Close it and reopen the dashboard.") from error
            for _ in range(50):
                if dashboard.process_identity(pid) != expected["process_identity"]:
                    try:
                        with dashboard.lock(base / ".sync-state" / "dashboard-server.lock", timeout=0):
                            # A different process may have replaced the descriptor.
                            # Never delete another helper's endpoint.
                            try:
                                if read_endpoint(base) == expected:
                                    endpoint_path(base).unlink()
                            except FileNotFoundError:
                                pass
                        return
                    except dashboard.LockBusy:
                        pass
                time.sleep(0.1)
            raise RuntimeError("The outdated dashboard helper did not stop. Try reopening shortly.")
    except dashboard.LockBusy as error:
        raise RuntimeError("The dashboard is busy managing a sync. Try reopening after it finishes.") from error


def serve(base):
    base = Path(base).resolve()
    with dashboard.lock(base / ".sync-state" / "dashboard-server.lock", timeout=0):
        server = DashboardServer(base)
        endpoint = endpoint_path(base)
        dashboard.atomic_write(endpoint, json.dumps({"port": server.server_port,
                               "pid": os.getpid(), "process_identity": dashboard.process_identity(os.getpid()),
                               "instance": server.instance}))
        server.timeout = 1
        try:
            while server.active_requests or time.monotonic() - server.last_request < IDLE_SECONDS:
                server.handle_request()
        finally:
            server.server_close()
            try:
                if json.loads(endpoint.read_text(encoding="utf-8")).get("instance") == server.instance:
                    endpoint.unlink()
            except (OSError, ValueError):
                pass


def open_dashboard(base):
    base = Path(base).resolve()
    with dashboard.lock(base / ".sync-state" / "dashboard-launch.lock", timeout=15):
        running = verified_endpoint(base)
        url = running[1] if running else None
        if running and running[2].get("version") != CODE_VERSION:
            stop_outdated_helper(base, running[0])
            url = None
        if not url:
            python = Path(sys.executable)
            if os.name == "nt":
                python = python.with_name("pythonw.exe")
            command = [str(python), str(Path(__file__).with_name("github_sync.py")), "dashboard", "--serve"]
            subprocess.Popen(command, cwd=str(base), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
            for _ in range(100):
                url = running_url(base)
                if url:
                    break
                time.sleep(0.1)
            if not url:
                raise RuntimeError(f"The dashboard helper did not start. See {base / 'sync.log'}.")
    webbrowser.open(url)
    return url
