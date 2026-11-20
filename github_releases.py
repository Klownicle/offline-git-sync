"""Shared release-channel selection for downloads and on-demand previews."""

import fnmatch
import re


_TYPE_ID = re.compile(r"(?:stable|prerelease|(?:stable|prerelease|custom):[a-z][a-z0-9_-]{0,63})\Z")
_FAMILIES = {"nightly": "Nightly", "beta": "Beta", "rc": "Release candidate", "alpha": "Alpha",
             "canary": "Canary", "dev": "Development", "preview": "Preview",
             "experimental": "Experimental", "snapshot": "Snapshot", "unstable": "Unstable",
             "test": "Test", "rolling": "Rolling", "lts": "LTS"}
_ALIASES = {"release-candidate": "rc", "candidate": "rc", "development": "dev", "develop": "dev",
            "long-term-support": "lts"}
_QUALIFIER = re.compile(r"(?:^|[._/\-])(" + "|".join([*_ALIASES, *_FAMILIES]) +
                        r")(?:[._\-]?\d+)?(?=$|[._/\-])", re.I)


def default_release_policy():
    return {"version": 1, "mode": "all", "selected_types": [], "limit_mode": "joint",
            "limits": {}, "type_rules": []}


def normalize_release_policy(policy):
    """Validate explicit policies without interpreting a missing legacy policy."""
    if not isinstance(policy, dict) or set(policy) != set(default_release_policy()):
        raise ValueError("Release policy must contain version, mode, selected_types, limit_mode, limits, and type_rules")
    if type(policy["version"]) is not int or policy["version"] != 1:
        raise ValueError("Unsupported release policy version")
    if policy["mode"] not in ("all", "selected") or policy["limit_mode"] not in ("joint", "per_type"):
        raise ValueError("Invalid release selection or limit mode")
    selected = policy["selected_types"]
    if (not isinstance(selected, list) or len(selected) > 1000
            or any(not isinstance(item, str) or not _TYPE_ID.fullmatch(item) for item in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError("Selected release types must be unique valid type IDs")
    limits = policy["limits"]
    if (not isinstance(limits, dict) or len(limits) > 1000 or any(
            not isinstance(key, str) or not _TYPE_ID.fullmatch(key)
            or type(value) is not int or not 1 <= value <= 100 for key, value in limits.items())):
        raise ValueError("Each release type limit must be 1–100")
    rules = policy["type_rules"]
    if not isinstance(rules, list) or len(rules) > 50:
        raise ValueError("At most 50 custom release type rules are supported")
    normalized_rules, identifiers = [], set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"id", "label", "patterns"}:
            raise ValueError("Custom release types need id, label, and patterns")
        identifier, label, patterns = rule["id"], rule["label"], rule["patterns"]
        if (not isinstance(identifier, str) or not identifier.startswith("custom:")
                or not _TYPE_ID.fullmatch(identifier) or identifier in identifiers):
            raise ValueError("Custom release type IDs must be unique custom:<slug> values")
        if (not isinstance(label, str) or not label.strip() or len(label) > 80
                or any(ord(char) < 32 or ord(char) == 127 for char in label)):
            raise ValueError("Custom release type labels must be 1–80 printable characters")
        if (not isinstance(patterns, list) or not 1 <= len(patterns) <= 50
                or any(not isinstance(pattern, str) or not pattern or len(pattern) > 160
                       or any(ord(char) < 32 or ord(char) == 127 for char in pattern) for pattern in patterns)):
            raise ValueError("Custom release types need 1–50 tag patterns, each at most 160 printable characters")
        identifiers.add(identifier)
        normalized_rules.append({"id": identifier, "label": label.strip(), "patterns": list(dict.fromkeys(patterns))})
    return {"version": 1, "mode": policy["mode"], "selected_types": list(selected),
            "limit_mode": policy["limit_mode"], "limits": dict(limits), "type_rules": normalized_rules}


def resolve_release_policy(job=None, override=None, *, legacy=False):
    """New syncs include every type; absence on existing jobs retains legacy rules."""
    if override is not None:
        return normalize_release_policy(override)
    if legacy:
        return None
    if job is None:
        return default_release_policy()
    return normalize_release_policy(job["release_policy"]) if "release_policy" in job else None


def _automatic_type(tag, prerelease):
    qualifier = _QUALIFIER.search(tag)
    if qualifier:
        family = qualifier[1].lower()
        family = _ALIASES.get(family, family)
        return (f"prerelease:{family}", _FAMILIES[family], "tag") if prerelease else (
            f"stable:{family}", f"{_FAMILIES[family]} (stable)", "tag")
    if not prerelease:
        return "stable", "Stable", "publisher"
    # Discover a meaningful semver qualifier or version-bearing prefix. Avoid
    # turning hashes, dates, build numbers, or whole unique tags into new types.
    qualifier = re.match(r"^v?\d+(?:\.\d+){1,3}-([a-z][a-z_-]{1,31}?)(?=[.\d+]|$)", tag, re.I)
    prefix = re.match(r"^([a-z][a-z_-]{2,31}?)[-_/](?=v?\d|[0-9a-f]{7,}(?:$|[-_.]))", tag, re.I)
    match = qualifier or prefix
    family = match[1].lower().strip("-_") if match else ""
    if (len(family) >= 3 and family not in {"release", "releases", "version", "build", "tag"}
            and not re.fullmatch(r"[a-f]+", family)):
        return f"prerelease:{family}", family.replace("-", " ").replace("_", " ").title(), "tag"
    return "prerelease", "Prerelease", "publisher"


def classify_releases(releases, type_rules=None):
    """Describe every published type, independently of the selected subset."""
    policy = default_release_policy()
    policy["type_rules"] = [] if type_rules is None else type_rules
    rules = normalize_release_policy(policy)["type_rules"]
    typed = []
    descriptions = {rule["id"]: {"id": rule["id"], "label": rule["label"], "source": "custom",
                                      "count": 0, "examples": []} for rule in rules}
    for release in releases:
        if not isinstance(release, dict):
            raise ValueError("The repository host returned an invalid release")
        draft, prerelease = release.get("draft", False), release.get("prerelease", False)
        if type(draft) is not bool or type(prerelease) is not bool:
            raise ValueError("The repository host returned invalid release-channel flags")
        published = release.get("published_at")
        if published is not None and not isinstance(published, str):
            raise ValueError("The repository host returned an invalid release publication date")
        if draft:
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not tag:
            raise ValueError("The repository host returned an invalid release tag")
        identifier, label, source = _automatic_type(tag, prerelease)
        for rule in rules:
            if any(fnmatch.fnmatchcase(tag.casefold(), pattern.casefold()) for pattern in rule["patterns"]):
                identifier, label, source = rule["id"], rule["label"], "custom"
                break
        item = {**release, "type_id": identifier, "type_label": label, "type_source": source}
        typed.append(item)
        description = descriptions.setdefault(identifier, {"id": identifier, "label": label,
            "source": source, "count": 0, "examples": []})
        description["count"] += 1
        if tag not in description["examples"] and len(description["examples"]) < 5:
            description["examples"].append(tag)
    return {"releases": typed, "release_types": sorted(descriptions.values(), key=lambda item: item["id"])}


def resolve_include_prereleases(job=None, override=None):
    """New syncs are stable-only; older saved settings retain both channels."""
    value = (job.get("include_prereleases", True) if job is not None else False) \
        if override is None else override
    if type(value) is not bool:
        raise ValueError("Include prereleases must be true or false")
    return value


def resolve_release_limits(job=None, *, last, separate_release_limits=None,
                           stable_last=None, prerelease_last=None):
    """Resolve omitted options from saved settings, with joint limits by default."""
    if type(last) is not int or not 1 <= last <= 100:
        raise ValueError("Release count must be 1–100")
    saved = job if job is not None else {}
    separate = (saved.get("separate_release_limits", False)
                if separate_release_limits is None else separate_release_limits)
    if type(separate) is not bool:
        raise ValueError("Separate release limits must be true or false")
    result = {"separate_release_limits": separate}
    for key, override in (("stable_last", stable_last), ("prerelease_last", prerelease_last)):
        value = saved.get(key, last) if override is None else override
        if type(value) is not int or not 1 <= value <= 100:
            raise ValueError("Stable and prerelease counts must each be 1–100")
        result[key] = value
    return result


def select_releases(releases, last, include_prereleases=False, *, separate_release_limits=False,
                    stable_last=None, prerelease_last=None, release_policy=None):
    """Apply a repository's type policy, or preserve its legacy channel counts.

    Tag qualifiers refine the discovered type without changing publisher flags.
    Legacy stable/prerelease filtering continues to use those flags directly.
    """
    if type(separate_release_limits) is not bool:
        raise ValueError("Separate release limits must be true or false")
    limits = resolve_release_limits(last=last, separate_release_limits=separate_release_limits,
                                    stable_last=stable_last, prerelease_last=prerelease_last)
    if type(include_prereleases) is not bool:
        raise ValueError("Include prereleases must be true or false")
    policy = normalize_release_policy(release_policy) if release_policy is not None else None
    typed = classify_releases(releases, policy["type_rules"] if policy else None)["releases"]
    if policy is not None:
        selected_ids = set(policy["selected_types"])
        ordered = sorted((release for release in typed if policy["mode"] == "all"
                          or release["type_id"] in selected_ids),
                         key=lambda item: item.get("published_at") or "", reverse=True)
        if policy["limit_mode"] == "joint":
            return ordered[:last]
        remaining, selected = {}, []
        for release in ordered:
            identifier = release["type_id"]
            remaining.setdefault(identifier, policy["limits"].get(identifier, last))
            if remaining[identifier]:
                selected.append(release)
                remaining[identifier] -= 1
        return selected
    eligible = [release for release in typed if include_prereleases or not release.get("prerelease", False)]
    ordered = sorted(eligible, key=lambda item: item.get("published_at") or "", reverse=True)
    if not separate_release_limits:
        return ordered[:last]
    remaining = {False: limits["stable_last"], True: limits["prerelease_last"]}
    selected = []
    for release in ordered:
        channel = release.get("prerelease", False)
        if remaining[channel]:
            selected.append(release)
            remaining[channel] -= 1
    return selected
