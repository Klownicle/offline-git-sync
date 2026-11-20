"""Verify old release downloads using the sync log's recorded SHA256 digests."""

from collections import OrderedDict
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import stat
import threading


MAX_LOG_BYTES = 32 * 1024 * 1024
MAX_CACHE_ENTRIES = 2048
_LINE = re.compile(r"^(?:(\d{4}-\d{2}-\d{2}T\S+) )?Downloaded (.+): SHA256 ([a-fA-F0-9]{64})$")
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)
_verified_cache = OrderedDict()
_cache_lock = threading.Lock()


def _key(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _candidate(base, value):
    """Only direct files in a release folder beneath this installation qualify.

    The caller also validates the selected repository and release.json. This
    helper independently rejects absolute-path aliases and escapes from base.
    """
    text = os.fspath(value)
    if not isinstance(text, str) or any(ord(char) < 32 for char in text):
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        return None
    # Do not normalize traversal or Windows filename aliases into eligibility.
    components = re.split(r"[/\\]", text)
    if candidate.drive:
        components = components[1:]
    for component in filter(None, components):
        if (component in {".", ".."} or component.endswith((".", " "))
                or re.search(r'[<>:"|?*]', component) or _RESERVED.fullmatch(component)):
            return None
    base_path = Path(os.path.abspath(base))
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(base_path)
    except ValueError:
        return None
    # Source checkouts and arbitrary files directly below base are never assets.
    if len(relative.parts) < 4 or not candidate.parent.parent.name.endswith("-releases"):
        return None
    return candidate


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _ordinary(info, *, file=False):
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        return False
    return not file or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)


def _ancestry(path):
    """Record ancestor identities so a replaced directory cannot hide a link."""
    result = []
    for ancestor in reversed(path.parents):
        info = ancestor.lstat()
        if not _ordinary(info) or not stat.S_ISDIR(info.st_mode):
            return None
        result.append((_key(ancestor), info.st_dev, info.st_ino))
    return tuple(result)


def legacy_asset_records(base):
    """Read at most the last 32 MiB, returning exact absolute paths and digests.

    Both original unprefixed lines and current ISO timestamp-prefixed lines are
    recognized. An unreadable/unsafe log supplies no provenance. No log entry by
    itself authorizes deletion: verified_legacy_asset must also match the bytes.
    """
    records = {}
    log = Path(os.path.abspath(base)) / "sync.log"
    try:
        if _ancestry(log) is None or not _ordinary(log.lstat(), file=True):
            return records
        with log.open("rb") as source:
            if not _ordinary(os.fstat(source.fileno()), file=True):
                return records
            size = os.fstat(source.fileno()).st_size
            offset = max(0, size - MAX_LOG_BYTES)
            source.seek(offset)
            data = source.read(MAX_LOG_BYTES)
        if offset:
            # The bounded tail may start midway through a filename or digest.
            data = data.partition(b"\n")[2]
        for raw in data.splitlines():
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            match = _LINE.fullmatch(line)
            if not match:
                continue
            timestamp, filename, digest = match.groups()
            if timestamp:
                try:
                    datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError:
                    continue
            candidate = _candidate(base, filename)
            if candidate is not None:
                records[_key(candidate)] = digest.lower()
    except (OSError, ValueError, TypeError):
        return {}
    return records


def verified_legacy_asset(base, path, records):
    """Check provenance and unchanged bytes, without following filesystem links.

    The caller only supplies nonmatching, otherwise unrecognized direct files
    from an already validated release directory. Source ZIPs, tag markers,
    metadata, temporary files, and ambiguous filenames remain caller-protected.
    """
    try:
        candidate = _candidate(base, path)
        if candidate is None:
            return False
        filename = _key(candidate)
        expected = records.get(filename)
        if not isinstance(expected, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected):
            return False
        expected = expected.lower()
        ancestry = _ancestry(candidate)
        before = candidate.lstat()
        if ancestry is None or not _ordinary(before, file=True):
            return False
        identity = _identity(before)
        cache_key = (filename, *identity)
        with _cache_lock:
            cached = _verified_cache.get(cache_key)
            if cached == expected:
                _verified_cache.move_to_end(cache_key)
        if cached == expected:
            after = candidate.lstat()
            return (_ordinary(after, file=True) and _identity(after) == identity
                    and _ancestry(candidate) == ancestry)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        handle = os.open(candidate, flags)
        with os.fdopen(handle, "rb") as source:
            opened = os.fstat(source.fileno())
            if not _ordinary(opened, file=True) or _identity(opened) != identity:
                return False
            digest = hashlib.sha256()
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
            completed = os.fstat(source.fileno())
            if not _ordinary(completed, file=True) or _identity(completed) != identity:
                return False
        after = candidate.lstat()
        if (not _ordinary(after, file=True) or _identity(after) != identity
                or _ancestry(candidate) != ancestry or digest.hexdigest() != expected):
            return False
        with _cache_lock:
            _verified_cache[cache_key] = expected
            _verified_cache.move_to_end(cache_key)
            while len(_verified_cache) > MAX_CACHE_ENTRIES:
                _verified_cache.popitem(last=False)
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False
