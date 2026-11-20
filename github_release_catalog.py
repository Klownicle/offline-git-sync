"""Remember discovered release types without changing a sync's selection rules."""

from datetime import datetime
import json
from pathlib import Path
import re
import stat

import github_dashboard as dashboard
from github_sources import parse_repository


MAX_TYPES = 1000
MAX_BYTES = 2 * 1024 * 1024


def _checked_path(path):
    for entry in [*reversed(path.parents), path]:
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Release type history cannot use linked or redirected paths")
        if entry != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError("Release type history has an invalid parent directory")
        if entry == path and not stat.S_ISREG(info.st_mode):
            raise ValueError("Release type history is not a regular file")
    return path


def _path(base, repository):
    repository = parse_repository(repository, allow_url=False).key
    path = Path(base).absolute() / ".sync-state" / (dashboard.key(repository) + ".release-types.json")
    return _checked_path(path)


def _text(value, maximum, name):
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(character) < 32 for character in value)):
        raise ValueError(f"Invalid release type {name}")
    return value


def _descriptors(values):
    if not isinstance(values, list) or len(values) > MAX_TYPES:
        raise ValueError("Too many or invalid release types")
    result, seen = [], set()
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("Invalid release type description")
        identifier = _text(value.get("id"), 100, "identifier")
        if not re.fullmatch(r"[a-z][a-z0-9_.:-]*", identifier) or identifier in seen:
            raise ValueError("Invalid or duplicate release type identifier")
        seen.add(identifier)
        label = _text(value.get("label"), 80, "label")
        source = value.get("source")
        if source not in {"publisher", "tag", "custom"}:
            raise ValueError("Invalid release type source")
        count = value.get("count")
        if type(count) is not int or not 0 <= count <= 1_000_000:
            raise ValueError("Invalid release type count")
        examples = value.get("examples", [])
        if not isinstance(examples, list) or len(examples) > 100:
            raise ValueError("Invalid release type examples")
        examples = list(dict.fromkeys(_text(tag, 512, "example") for tag in examples))[:5]
        result.append({"id": identifier, "label": label, "source": source,
                       "count": count, "examples": examples})
    return result


def _date(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Release type history needs timezone-aware timestamps")
    return value


def read(base, repository):
    """A missing/unreadable cache must not make the dashboard unavailable."""
    empty = {"version": 1, "repository": repository, "updated_at": None, "types": []}
    try:
        path = _path(base, repository)
        if not path.exists():
            return empty
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("Release type history is too large")
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("version") != 1
                or data.get("repository") != repository):
            raise ValueError("Release type history does not match this repository")
        descriptions = _descriptors(data.get("types"))
        result = []
        for description, stored in zip(descriptions, data["types"]):
            current = stored.get("current_count")
            available = stored.get("available")
            if type(current) is not int or current != description["count"]:
                raise ValueError("Invalid release type history count")
            if type(available) is not bool or available != (current > 0):
                raise ValueError("Invalid release type availability")
            result.append({**description, "current_count": current, "available": available,
                           "first_seen": _date(stored.get("first_seen")),
                           "last_seen": _date(stored.get("last_seen"))})
        return {**empty, "updated_at": _date(data.get("updated_at")), "types": result}
    except (OSError, ValueError, TypeError, KeyError) as error:
        return {**empty, "error": str(error)}


def update(base, repository, descriptions):
    """Merge discovery history. This never reads or writes sync-config.json."""
    incoming = _descriptors(descriptions)
    path = _path(base, repository)
    lock_path = _checked_path(path.with_suffix(".lock"))
    with dashboard.lock(lock_path, timeout=2):
        previous = read(base, repository)
        if previous.get("error"):
            raise ValueError(f"Could not update release type history: {previous['error']}")
        now = dashboard.now()
        known = {item["id"]: {**item, "count": 0, "current_count": 0, "available": False}
                 for item in previous["types"]}
        for description in incoming:
            old = known.get(description["id"], {})
            examples = list(dict.fromkeys(description["examples"] + old.get("examples", [])))[:5]
            known[description["id"]] = {
                **description, "examples": examples, "current_count": description["count"],
                "available": description["count"] > 0,
                "first_seen": old.get("first_seen", now), "last_seen": now,
            }
        if len(known) > MAX_TYPES:
            raise ValueError("Release type history exceeds the supported type count")
        result = {"version": 1, "repository": repository, "updated_at": now,
                  "types": sorted(known.values(), key=lambda item: (item["id"] != "stable",
                                                                    item["label"].casefold(), item["id"]))}
        content = json.dumps(result, ensure_ascii=False, indent=2)
        if len(content.encode("utf-8")) > MAX_BYTES:
            raise ValueError("Release type history exceeds the supported size")
        _path(base, repository)  # Recheck links after taking the history lock.
        dashboard.atomic_write(path, content)
        return result
