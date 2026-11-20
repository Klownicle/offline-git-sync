"""Read-only, on-demand release metadata for the dashboard's asset preview."""

from datetime import datetime, timezone
import json
import socket
import time
import urllib.error
import urllib.request

from github_https import urlopen
from github_management import normalize_repository, _safe_name
from github_sources import parse_repository, normalize_release
from github_releases import (classify_releases, default_release_policy, normalize_release_policy,
                             resolve_release_limits, select_releases)
import github_forgejo as forgejo


_NEW_POLICY = object()


# A preview must never silently present a partial set of releases or assets.
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_ASSETS = 5000
MAX_RELEASE_PAGES = 99
MAX_SECONDS = 90
HEADERS = {"User-Agent": "Windows-GitHub-Sync", "Accept": "application/vnd.github+json"}


def _text(value, field, *, optional=False, service="GitHub"):
    if value is None and optional:
        return ""
    if not isinstance(value, str) or len(value) > 2048:
        raise RuntimeError(f"{service} returned an invalid {field}; no partial preview was shown")
    return value


class _MetadataReader:
    def __init__(self):
        self.deadline = time.monotonic() + MAX_SECONDS
        self.bytes_read = 0

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Asset preview took too long to load; try again later")
        return remaining

    def page(self, repository, page):
        # Construct the URL from the validated repository. Never follow a URL
        # supplied in the dashboard form or a release's metadata.
        reference = parse_repository(repository, allow_url=False)
        service = "GitHub" if reference.provider == "github" else reference.host
        request = urllib.request.Request(reference.release_page_url(page), headers=reference.headers)
        try:
            opener = urlopen if reference.provider == "github" else forgejo.urlopen
            with opener(request, timeout=min(10, self._remaining())) as response:
                chunks, size = [], 0
                while True:
                    self._remaining()
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    self.bytes_read += len(chunk)
                    if size > MAX_PAGE_BYTES or self.bytes_read > MAX_TOTAL_BYTES:
                        raise RuntimeError(f"{service} release metadata is too large for an asset preview; "
                                           "no partial preview was shown")
                    chunks.append(chunk)
            self._remaining()
            result = json.loads(b"".join(chunks))
        except urllib.error.HTTPError as error:
            status = error.code
            remaining = error.headers.get("X-RateLimit-Remaining") if error.headers else None
            error.close()
            if status == 404:
                raise RuntimeError("Repository was not found or is private; asset preview "
                                   f"supports public repositories on {service}") from None
            if status == 429 or (status == 403 and remaining == "0"):
                raise RuntimeError(f"{service}'s API rate limit was reached; wait before refreshing "
                                   "the asset preview") from None
            if status in {401, 403}:
                raise RuntimeError(f"{service} denied access to this repository's release metadata; "
                                   "try again later") from None
            raise RuntimeError(f"{service} could not load the asset preview (HTTP {status}); "
                               "try again later") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
            raise RuntimeError(f"Could not reach {service} for the asset preview; "
                               "check the connection and try again") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise RuntimeError(f"{service} returned invalid release metadata; try again later") from None
        if not isinstance(result, list) or len(result) > 100:
            raise RuntimeError(f"{service} returned invalid release metadata; no partial preview was shown")
        return result


def preview(repository, last, source_only=False, include_prereleases=False, *,
            separate_release_limits=False, stable_last=None, prerelease_last=None,
            release_policy=_NEW_POLICY, types_only=False):
    """Return release names/assets only; pattern matching happens locally in the UI.

    This uses the same published-at ordering and embedded assets as the actual
    sync. In particular, channel selection precedes the limit and source ZIPs are independent
    of the configured asset patterns. No download URLs are returned to the UI.
    """
    repository = normalize_repository(repository)
    reference = parse_repository(repository, allow_url=False)
    service = "GitHub" if reference.provider == "github" else reference.host
    if type(last) is not int or not 1 <= last <= 100:
        raise ValueError("Release count must be 1–100")
    if type(source_only) is not bool:
        raise ValueError("Source-only must be true or false")
    if type(include_prereleases) is not bool:
        raise ValueError("Include prereleases must be true or false")
    if type(separate_release_limits) is not bool:
        raise ValueError("Separate release limits must be true or false")
    limits = resolve_release_limits(last=last, separate_release_limits=separate_release_limits,
                                    stable_last=stable_last, prerelease_last=prerelease_last)
    policy = (default_release_policy() if release_policy is _NEW_POLICY else
              normalize_release_policy(release_policy) if release_policy is not None else None)
    if reference.provider != "github":
        forgejo.detect_repository(reference)
    result = {"repository": repository, "last": last, "source_only": source_only,
              "include_prereleases": include_prereleases, **limits,
              "release_policy": policy, "effective_policy": policy,
              "legacy_selection": ({"include_prereleases": include_prereleases, **limits}
                                   if policy is None else None), "release_types": [],
              "releases": [], "fetched_at": datetime.now(timezone.utc).isoformat(), "message": ""}
    if source_only:
        result["message"] = "Source-only syncs update the source checkout without downloading release assets or source ZIPs."
        return result

    reader, published = _MetadataReader(), []
    for page in range(1, MAX_RELEASE_PAGES + 1):
        releases = reader.page(repository, page)
        for release in releases:
            if not isinstance(release, dict):
                raise RuntimeError(f"{service} returned an invalid release; no partial preview was shown")
            try:
                release = normalize_release(reference, release)
            except ValueError as error:
                raise RuntimeError(f"{service} returned invalid release metadata ({error}); "
                                   "no partial preview was shown") from None
            _text(release.get("published_at"), "publication date", optional=True, service=service)
            published.append(release)
        if not releases or (reference.provider == "github" and len(releases) < 100):
            break
    else:
        raise RuntimeError("This repository has too many release pages for an asset preview; "
                           "no partial preview was shown")

    try:
        discovered = classify_releases(published, policy["type_rules"] if policy else None)
        result["release_types"] = discovered["release_types"]
        selected = select_releases(published, last, include_prereleases, release_policy=policy, **limits)
    except ValueError as error:
        raise RuntimeError(f"{service} returned invalid release metadata ({error}); "
                           "no partial preview was shown") from None
    if types_only:
        return result
    total_assets = 0
    for release in selected:
        tag = _text(release.get("tag_name"), "release tag", service=service)
        try:
            archive_name = f"Source-code-{_safe_name(tag)}.zip"
        except ValueError:
            raise RuntimeError(f"{service} returned an unsafe release tag; no partial preview was shown") from None
        raw_assets = release.get("assets")
        if not isinstance(raw_assets, list):
            raise RuntimeError(f"{service} returned an invalid asset list; no partial preview was shown")
        total_assets += len(raw_assets)
        if total_assets > MAX_ASSETS:
            raise RuntimeError("These releases have too many assets for an asset preview; "
                               "select fewer releases and try again")
        assets = []
        for asset in raw_assets:
            if (not isinstance(asset, dict) or type(asset.get("size")) is not int
                    or asset["size"] < 0 or type(asset.get("id")) is not int):
                raise RuntimeError(f"{service} returned invalid asset details; no partial preview was shown")
            assets.append({"id": asset["id"], "name": _text(asset.get("name"), "asset name", service=service),
                           "size": asset["size"]})
        result["releases"].append({
            "tag": tag, "name": _text(release.get("name"), "release name", optional=True, service=service) or tag,
            "published_at": release.get("published_at"), "prerelease": bool(release.get("prerelease")),
            "type_id": release["type_id"], "type_label": release["type_label"],
            "type_source": release["type_source"],
            "assets": assets, "source_archive": {"name": archive_name, "size": None}})
    if not selected:
        channel = ("matching" if policy is not None else "published" if include_prereleases else "stable")
        result["message"] = (f"No {channel} releases are available. The source checkout will still sync; "
                             "existing release downloads are retained.")
    return result


def discover(repository, release_policy=_NEW_POLICY):
    """Fetch all published type descriptions without downloading release assets."""
    return preview(repository, 100, release_policy=release_policy, types_only=True)
