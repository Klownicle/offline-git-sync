"""Small, separately persisted progress records for dashboard-requested first runs."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import uuid

import github_dashboard as dashboard


def _path(base, repository):
    return Path(base) / ".sync-state" / (dashboard.key(repository) + ".first-sync.json")


def _lock(base, repository):
    return dashboard.lock(_path(base, repository).with_suffix(".lock"), timeout=2)


def _read(base, repository):
    try:
        state = json.loads(_path(base, repository).read_text(encoding="utf-8"))
        return state if isinstance(state, dict) and state.get("repository") == repository else None
    except (OSError, ValueError, TypeError):
        return None


def _write(base, repository, state):
    state["updated_at"] = dashboard.now()
    dashboard.atomic_write(_path(base, repository), json.dumps(state, separators=(",", ":")))


def queue(base, repository):
    """Called while the add operation still owns the repository's run lock."""
    state = {"id": uuid.uuid4().hex, "repository": repository, "status": "queued",
             "stage": "queued", "phase": "Waiting for the first sync to start",
             "queued_at": dashboard.now(), "started_at": None, "finished_at": None,
             "pid": None, "process_identity": None, "error": None,
             "files_completed": 0, "files_total": None, "files_downloaded": 0,
             "files_skipped": 0, "current_file": None, "current_release": None,
             "bytes_downloaded": 0, "bytes_total": None}
    with _lock(base, repository):
        _write(base, repository, state)
    return state["id"]


def clear(base, repository):
    """A newly re-added sync must not inherit an earlier add's first-run record."""
    with _lock(base, repository):
        _path(base, repository).unlink(missing_ok=True)


def launched(base, repository, identifier, pid):
    # A fast child may have claimed or completed the record before Popen returns.
    with _lock(base, repository):
        state = _read(base, repository)
        if state and state["id"] == identifier and state["status"] == "queued":
            state.update(pid=pid, process_identity=dashboard.process_identity(pid))
            _write(base, repository, state)


def launch_failed(base, repository, identifier, error):
    with _lock(base, repository):
        state = _read(base, repository)
        if state and state["id"] == identifier and state["status"] == "queued":
            state.update(status="failed", phase="First sync could not start",
                         error=str(error), finished_at=dashboard.now())
            _write(base, repository, state)


def snapshot(base, repository):
    """Read cheaply; derive interruption without racing the process's writes."""
    state = _read(base, repository)
    if not state or state.get("status") not in {"queued", "running"}:
        return state
    if state.get("pid"):
        identity = dashboard.process_identity(state["pid"])
        interrupted = not identity or identity != state.get("process_identity")
    else:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(state["queued_at"])
            interrupted = age.total_seconds() > 120
        except (KeyError, TypeError, ValueError):
            interrupted = True
    if interrupted:
        state.update(status="interrupted", phase="First sync was interrupted",
                     error="The first sync process is no longer running. Check the sync log for details.")
    return state


class Tracker:
    """Stage/file updates are immediate; chunk updates write at most once a second.

    The record is intentionally separate from Report and never renders the full
    dashboard on a chunk. Progress reporting errors must not stop actual syncing.
    """

    def __init__(self, base, repository, emit=lambda message: None):
        self.base, self.repository, self.emit = base, repository, emit
        self.state = None
        self.last_write = 0
        try:
            with _lock(base, repository):
                state = snapshot(base, repository)
                if state and state.get("status") == "queued":
                    state.update(status="running", stage="source", phase="Fetching source",
                                 started_at=dashboard.now(), pid=os.getpid(),
                                 process_identity=dashboard.process_identity(os.getpid()))
                    self.state = state
                    _write(base, repository, state)
                    self.last_write = time.monotonic()
        except Exception as error:
            self._disable(error)

    def _disable(self, error):
        self.state = None
        try:
            self.emit(f"WARNING: first-sync progress could not update: {error}")
        except Exception:
            pass

    def _persist(self, force=True):
        if self.state is None or (not force and time.monotonic() - self.last_write < 1):
            return
        try:
            with _lock(self.base, self.repository):
                current = _read(self.base, self.repository)
                if not current or current.get("id") != self.state["id"]:
                    self.state = None
                    return
                _write(self.base, self.repository, self.state)
                self.last_write = time.monotonic()
        except Exception as error:
            self._disable(error)

    def stage(self, stage, phase):
        if self.state is not None:
            self.state.update(stage=stage, phase=phase)
            self._persist()

    def plan(self, count):
        if self.state is not None:
            self.state.update(stage="downloads", phase="Downloading release files", files_total=count)
            self._persist()

    def file(self, release, name, size=None):
        if self.state is not None:
            self.state.update(current_release=release, current_file=name,
                              bytes_downloaded=0, bytes_total=size, phase=f"Downloading {name}")
            self._persist()

    def transfer(self, downloaded, total=None):
        if self.state is not None:
            self.state["bytes_downloaded"] = downloaded
            if type(total) is int and total >= 0:
                self.state["bytes_total"] = total
            self._persist(force=False)

    def completed(self, downloaded):
        if self.state is not None:
            self.state["files_completed"] += 1
            self.state["files_downloaded" if downloaded else "files_skipped"] += 1
            self.state.update(current_file=None, current_release=None,
                              bytes_downloaded=0, bytes_total=None,
                              phase="Downloading release files")
            self._persist()

    def finish(self, error=None, warnings=False):
        if self.state is not None:
            self.state.update(status="failed" if error else "warning" if warnings else "success",
                              stage="finished", finished_at=dashboard.now(),
                              error=str(error) if error else None,
                              phase="First sync failed" if error else
                                    "First sync finished with warnings" if warnings else "First sync complete")
            if error is None:
                self.state.update(current_file=None, current_release=None,
                                  bytes_downloaded=0, bytes_total=None)
            self._persist()
