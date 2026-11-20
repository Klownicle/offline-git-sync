"""Offline repository identities for GitHub and public Forgejo/Gitea hosts."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from urllib.parse import quote, urlsplit


GITHUB_HOST = "github.com"
_PART = re.compile(r"[A-Za-z0-9_.-]+\Z")
_GITHUB_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_FORGEJO_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}
_LOCAL_SUFFIXES = {"localhost", "local", "localdomain", "internal", "intranet", "lan",
                   "home", "home.arpa", "test", "invalid", "example", "onion"}


def validate_hostname(host):
    """Validate canonical DNS syntax offline; network access separately checks DNS."""
    if (not isinstance(host, str) or not host.isascii() or host != host.lower()
            or len(host) > 253 or host.endswith(".") or "." not in host):
        raise ValueError("Repository host must be a public DNS hostname")
    labels = host.split(".")
    if (any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
            or not re.fullmatch(r"(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})", labels[-1])
            or labels[0].upper() in _RESERVED
            or any(host == suffix or host.endswith("." + suffix) for suffix in _LOCAL_SUFFIXES)):
        raise ValueError("Repository host must be a public DNS hostname safe for a Windows folder")
    return host


def _validate_parts(host, owner, name):
    validate_hostname(host)
    for part in (owner, name):
        if (not isinstance(part, str) or not _PART.fullmatch(part) or part in {".", ".."}
                or len(part) > 100 or part.endswith(".") or part.split(".", 1)[0].upper() in _RESERVED):
            raise ValueError("Repository contains a name that is unsafe for a Windows folder")
    if host == GITHUB_HOST and not _GITHUB_OWNER.fullmatch(owner):
        raise ValueError("GitHub owner must use 1–39 letters, numbers, or internal hyphens")
    if host != GITHUB_HOST and not _FORGEJO_OWNER.fullmatch(owner):
        raise ValueError("Repository owner must start with a letter or number and use letters, numbers, dots, hyphens, or underscores")


@dataclass(frozen=True)
class Repository:
    host: str
    owner: str
    name: str

    def __post_init__(self):
        _validate_parts(self.host, self.owner, self.name)

    @property
    def provider(self):
        return "github" if self.host == GITHUB_HOST else "forgejo"

    @property
    def slug(self):
        return f"{self.owner}/{self.name}"

    @property
    def key(self):
        return self.slug if self.host == GITHUB_HOST else f"{self.host}/{self.slug}"

    @property
    def web_url(self):
        return f"https://{self.host}/{self.slug}"

    @property
    def clone_url(self):
        return self.web_url + ".git"

    @property
    def api_url(self):
        if self.host == GITHUB_HOST:
            return f"https://api.github.com/repos/{self.slug}"
        return f"https://{self.host}/api/v1/repos/{self.slug}"

    @property
    def headers(self):
        return {"User-Agent": "Windows-GitHub-Sync", "Accept":
                "application/vnd.github+json" if self.provider == "github" else "application/json"}

    def release_page_url(self, page, limit=100):
        if type(page) is not int or page < 1:
            raise ValueError("Release page must be a positive integer")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Release page size must be 1–100")
        size_key = "per_page" if self.provider == "github" else "limit"
        return f"{self.api_url}/releases?{size_key}={limit}&page={page}"

    def source_archive_url(self, tag):
        if (not isinstance(tag, str) or not tag or ".." in tag
                or any(ord(character) < 32 or ord(character) == 127 for character in tag)):
            raise ValueError("Release tag is invalid")
        encoded = quote(tag, safe="")
        if self.provider == "github":
            return f"{self.api_url}/zipball/{encoded}"
        return f"{self.api_url}/archive/{encoded}.zip"

    def source_path(self, base):
        return Path(base).joinpath(*self.key.split("/"))

    def releases_path(self, base):
        source = self.source_path(base)
        return source.with_name(source.name + "-releases")

    @property
    def task_name(self):
        prefix = "" if self.provider == "github" else self.host + "-"
        return f"Git Sync - {prefix}{self.owner}-{self.name}"


def parse_repository(value, *, allow_url=True):
    """Parse a UI repository or a canonical config key without URL guessing.

    Existing GitHub keys remain owner/repository. Other providers include a host,
    keeping both providers' checkouts, state identities, and scheduled task names
    distinct even when an owner and repository happen to have the same names.
    """
    if not isinstance(value, str):
        raise ValueError("Repository must be owner/repository or a supported HTTPS repository URL")
    if not allow_url and value != value.strip():
        raise ValueError("Saved repository keys cannot contain surrounding whitespace")
    value = value.strip()
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Repository is empty or contains invalid characters")
    if "\\" in value or "?" in value or "#" in value:
        raise ValueError("Repository cannot contain a query, fragment, or backslash")
    if "://" in value:
        if not allow_url:
            raise ValueError("Saved repository keys must not be URLs")
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
        except ValueError:
            raise ValueError("Repository URL is invalid") from None
        if (parsed.scheme != "https" or not host
                or parsed.netloc.lower() != host or parsed.query or parsed.fragment):
            raise ValueError("Use a public HTTPS repository URL without credentials or a port")
        path = parsed.path.removeprefix("/").removesuffix("/").removesuffix(".git")
        parts = path.split("/")
        if len(parts) != 2:
            raise ValueError("Repository URL must contain only its owner and repository name")
        return Repository(host, *parts)
    parts = value.split("/")
    if len(parts) == 2:
        return Repository(GITHUB_HOST, *parts)
    if len(parts) == 3:
        return Repository(parts[0].lower() if allow_url else parts[0], *parts[1:])
    raise ValueError("Use owner/repository for GitHub or an https://host/owner/repository URL for Forgejo/Gitea")


def normalize_repository(value):
    return parse_repository(value).key


def is_repository_key(value):
    try:
        return parse_repository(value, allow_url=False).key == value
    except (TypeError, ValueError):
        return False


def _forgejo_asset_url(repository, value):
    if (not isinstance(value, str) or not value or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ValueError(f"{repository.host} returned an invalid release asset URL")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
    except ValueError:
        raise ValueError(f"{repository.host} returned an invalid release asset URL") from None
    if (parsed.scheme != "https" or host != repository.host
            or parsed.netloc.lower() != repository.host or not parsed.path.startswith("/")
            or parsed.fragment):
        raise ValueError(f"Release assets must use public HTTPS on {repository.host} without credentials, ports, or fragments")
    return value


def _utc_timestamp(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("The repository host returned an invalid release publication date")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError):
        raise ValueError("The repository host returned an invalid release publication date") from None


def normalize_release(repository, raw):
    """Keep original metadata, supplying the shared sync fields for Forgejo.

    Forgejo can cap a requested page below its limit; callers must fetch until
    an empty page rather than treating a short page as the end of the listing.
    """
    repository = repository if isinstance(repository, Repository) else parse_repository(repository)
    if not isinstance(raw, dict):
        raise ValueError("The repository host returned an invalid release")
    result = dict(raw)
    if repository.provider == "github":
        return result
    tag = raw.get("tag_name")
    result["zipball_url"] = repository.source_archive_url(tag)
    result["published_at"] = _utc_timestamp(raw.get("published_at") or raw.get("created_at"))
    assets = raw.get("assets", [])
    if not isinstance(assets, list):
        raise ValueError(f"{repository.host} returned an invalid release asset list")
    result["assets"] = []
    for asset in assets:
        if (not isinstance(asset, dict) or type(asset.get("id")) is not int
                or not isinstance(asset.get("name"), str) or not asset["name"]
                or type(asset.get("size")) is not int or asset["size"] < 0):
            raise ValueError(f"{repository.host} returned invalid release asset details")
        normalized = dict(asset)
        normalized["browser_download_url"] = _forgejo_asset_url(repository, asset.get("browser_download_url"))
        result["assets"].append(normalized)
    return result
