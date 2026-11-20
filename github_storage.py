"""Logical size of a checkout and every local release, with a short disk cache.

Links and Windows reparse points are excluded, including on ancestor paths.
The size is a sum of regular-file lengths, not allocated disk space. Hard-linked
files are counted at each name; this deliberately matches the visible trees.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from github_sources import parse_repository


CACHE_TTL = 60
LOCK_TIMEOUT = 0.25
_SCHEMA = 1
_MAX_ERRORS = 20
_MAX_CACHE_BYTES = 65536
_MAX_BYTES = 2 ** 63 - 1


def _now():
    return datetime.now(timezone.utc).isoformat()


def _repository(repository):
    return parse_repository(repository, allow_url=False).key.split("/")


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _checked_path(path):
    """Inspect before traversal; abspath normalizes without resolving links."""
    path = Path(os.path.abspath(path))
    for ancestor in reversed((path, *path.parents)):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if _linked(info):
            raise ValueError(f"Linked or reparse path excluded: {ancestor}")
    return path


def _empty():
    return {"total_bytes": 0, "source_bytes": 0, "releases_bytes": 0,
            "complete": True, "skipped": 0, "errors": [],
            "scanned_at": _now(), "stale": False}


def _skip(result, path, error):
    result["complete"] = False
    result["skipped"] += 1
    if len(result["errors"]) < _MAX_ERRORS:
        result["errors"].append(f"{path}: {error}"[:1024])


def _tree_size(root, result):
    try:
        root = _checked_path(root)
        info = root.lstat()
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as error:
        _skip(result, root, error)
        return 0
    if not stat.S_ISDIR(info.st_mode):
        _skip(result, root, "Expected a directory")
        return 0
    total = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            # A queued directory may have changed since it was enumerated.
            _checked_path(directory)
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if _linked(info):
                            _skip(result, entry.path, "Linked or reparse entry excluded")
                        elif stat.S_ISDIR(info.st_mode):
                            pending.append(Path(entry.path))
                        elif stat.S_ISREG(info.st_mode):
                            total += info.st_size
                        else:
                            _skip(result, entry.path, "Unsupported file type excluded")
                    except OSError as error:
                        _skip(result, entry.path, error)
        except (OSError, ValueError) as error:
            _skip(result, directory, error)
    return total


def _scan(base, repository):
    descriptor = parse_repository(repository, allow_url=False)
    result = _empty()
    result["source_bytes"] = _tree_size(descriptor.source_path(base), result)
    result["releases_bytes"] = _tree_size(descriptor.releases_path(base), result)
    result["total_bytes"] = result["source_bytes"] + result["releases_bytes"]
    result["scanned_at"] = _now()
    return result


def _paths(base, repository):
    name = hashlib.sha256(repository.encode("utf-8")).hexdigest()[:24]
    parent = base / ".sync-state"
    return parent / (name + ".size.json"), parent / (name + ".size.lock")


def _read_cache(path, repository):
    """Return validated data and a revision, including invalidation tombstones."""
    try:
        _checked_path(path)
        with path.open("rb") as handle:
            raw = handle.read(_MAX_CACHE_BYTES + 1)
        revision = hashlib.sha256(raw).hexdigest()
        if len(raw) > _MAX_CACHE_BYTES:
            return None, revision
        data = json.loads(raw)
        if (not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != _SCHEMA
                or data.get("repository") != repository):
            return None, revision
        result = data.get("size")
        if not isinstance(result, dict):
            return None, revision
        for key in ("source_bytes", "releases_bytes", "total_bytes", "skipped"):
            if type(result.get(key)) is not int or not 0 <= result[key] <= _MAX_BYTES:
                return None, revision
        if (result["total_bytes"] != result["source_bytes"] + result["releases_bytes"]
                or type(result.get("complete")) is not bool
                or result.get("stale") is not False
                or not isinstance(result.get("errors"), list)
                or len(result["errors"]) > _MAX_ERRORS
                or any(not isinstance(error, str) or len(error) > 1024 for error in result["errors"])
                or (result["complete"] and (result["skipped"] or result["errors"]))
                or (not result["complete"] and (not result["skipped"] or not result["errors"]))
                or not isinstance(result.get("scanned_at"), str)):
            return None, revision
        timestamp = datetime.fromisoformat(result["scanned_at"])
        if timestamp.tzinfo is None or timestamp > datetime.now(timezone.utc):
            return None, revision
        return {key: result[key] for key in _empty()}, revision
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None, None


def _fresh(result):
    if result is None:
        return False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(result["scanned_at"])
    return 0 <= age.total_seconds() < CACHE_TTL


def _write_cache(path, repository, result):
    import github_dashboard as dashboard
    _checked_path(path)
    dashboard.atomic_write(path, json.dumps({"schema": _SCHEMA,
        "repository": repository, "size": result}, ensure_ascii=True))


def _write_lock(path):
    return _checked_path(path.with_name(path.stem + ".write.lock"))


def sync_size(base, repository, *, force=False):
    """Read cached size or scan once per 60 s, independently of sync settings.

    A concurrent scan is given 250 ms to finish. If still busy, a valid older
    result has ``stale=True``; without one, an incomplete zero result explains
    that a scan is in progress. Cache I/O failures never prevent a fresh scan.
    """
    import github_dashboard as dashboard
    _repository(repository)
    base = Path(os.path.abspath(base))
    path, lock_path = _paths(base, repository)
    cached, _ = _read_cache(path, repository)
    if not force and _fresh(cached):
        return cached
    try:
        _checked_path(lock_path)
        with dashboard.lock(lock_path, timeout=LOCK_TIMEOUT):
            cached, revision = _read_cache(path, repository)
            if not force and _fresh(cached):
                return cached
            result = _scan(base, repository)
            try:
                # Invalidation during a long scan must not restore its cache.
                with dashboard.lock(_write_lock(path), timeout=2):
                    if _read_cache(path, repository)[1] == revision:
                        _write_cache(path, repository, result)
            except (OSError, ValueError):
                pass
            return result
    except dashboard.LockBusy:
        cached, _ = _read_cache(path, repository)
        if cached is not None:
            return {**cached, "stale": True}
        result = _empty()
        _skip(result, repository, "Size scan in progress; refresh shortly")
        result["pending"] = True
        return result
    except (OSError, ValueError):
        return _scan(base, repository)


def invalidate_size(base, repository):
    """Invalidate only this repository's size cache; never touch synced files.

    A tombstone also prevents an already running scan from publishing its old
    result. Failure to write leaves the existing cache subject to its 60 s TTL.
    """
    import github_dashboard as dashboard
    _repository(repository)
    path, _ = _paths(Path(os.path.abspath(base)), repository)
    try:
        # This lock covers only cache writes, so invalidation does not wait for
        # a potentially large source/release traversal in another process.
        with dashboard.lock(_write_lock(path), timeout=2):
            _checked_path(path)
            dashboard.atomic_write(path, json.dumps({"invalidated": uuid.uuid4().hex}))
        return True
    except (OSError, ValueError):
        return False
