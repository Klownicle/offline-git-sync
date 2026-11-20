"""Public Forgejo/Gitea discovery and HTTPS with pinned public DNS addresses."""

import http.client
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit

from github_https import ssl_context
from github_sources import Repository, parse_repository, validate_hostname


MAX_REDIRECTS = 5
DETECTION_SECONDS = 30
MAX_REPOSITORY_BYTES = 2 * 1024 * 1024
MAX_VERSION_BYTES = 16 * 1024


def validate_public_url(value, *, expected_host=None):
    """Validate URL syntax; the HTTPS handler separately validates and pins DNS."""
    if (not isinstance(value, str) or not value or "\\" in value or "#" in value
            or any(ord(character) <= 32 or ord(character) == 127 for character in value)):
        raise ValueError("Use a public HTTPS URL without credentials, ports, or fragments")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
    except ValueError:
        raise ValueError("Invalid public HTTPS URL") from None
    if parsed.scheme != "https" or not host or parsed.netloc.lower() != host:
        raise ValueError("Use a public HTTPS URL without credentials or an explicit port")
    validate_hostname(host)
    if expected_host is not None and host != expected_host:
        raise ValueError("The repository API redirected to a different host")
    return host


def _public_addresses(host):
    """Reject the entire DNS answer if any destination is non-public."""
    validate_hostname(host)
    try:
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as error:
        raise RuntimeError(f"Could not resolve the public repository host {host}: {error}") from None
    if not records or len(records) > 64:
        raise ValueError("The repository host did not resolve to a bounded public address list")
    addresses, seen = [], set()
    for family, kind, protocol, _, address in records:
        if family not in {socket.AF_INET, socket.AF_INET6} or kind != socket.SOCK_STREAM:
            raise ValueError("The repository host returned an unsupported DNS address")
        try:
            ip = ipaddress.ip_address(address[0])
        except ValueError:
            raise ValueError("The repository host returned an invalid DNS address") from None
        mapped = getattr(ip, "ipv4_mapped", None)
        targets = (ip, mapped) if mapped is not None else (ip,)
        if (any(not target.is_global or target.is_multicast or target.is_reserved
                or target.is_loopback or target.is_link_local or target.is_unspecified
                or getattr(target, "is_site_local", False) for target in targets)
                or address[1] != 443 or (family == socket.AF_INET6 and address[3])):
            raise ValueError("The repository host resolves to a non-public network address; access was blocked")
        signature = (family, kind, protocol, address)
        if signature not in seen:
            seen.add(signature)
            addresses.append((family, kind, protocol, address))
    return addresses


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, addresses, **kwargs):
        super().__init__(host, **kwargs)
        self._public_addresses = addresses
        self._public_host = self.host
        self._create_connection = self._connect_public

    def _connect_public(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        if address != (self._public_host, 443) or self._tunnel_host:
            raise ValueError("Unexpected repository connection destination")
        deadline = time.monotonic() + timeout
        last_error = None
        for family, kind, protocol, destination in self._public_addresses:
            connection = socket.socket(family, kind, protocol)
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Repository connection timed out")
                connection.settimeout(remaining)
                if source_address:
                    connection.bind(source_address)
                # This is the already validated numeric sockaddr, not the DNS
                # hostname. TLS still verifies self.host in HTTPSConnection.
                connection.connect(destination)
                return connection
            except OSError as error:
                last_error = error
                connection.close()
        if last_error:
            raise last_error
        raise OSError("No public repository address was available")


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        host = validate_public_url(request.full_url)
        addresses = _public_addresses(host)
        def connection_factory(destination, **kwargs):
            if destination.lower() != host:
                raise ValueError("Unexpected repository connection host")
            return _PublicHTTPSConnection(destination, addresses=addresses, **kwargs)
        return self.do_open(connection_factory, request, context=self._context,
                            check_hostname=self._check_hostname)


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_repeats = MAX_REDIRECTS
    max_redirections = MAX_REDIRECTS

    def __init__(self, initial_host, allow_cross_host):
        self.initial_host = initial_host
        self.allow_cross_host = allow_cross_host

    def redirect_request(self, request, response, code, message, headers, new_url):
        host = validate_public_url(new_url, expected_host=None if self.allow_cross_host else self.initial_host)
        redirected = super().redirect_request(request, response, code, message, headers, new_url)
        if redirected is not None and request.get_method() == "HEAD":
            redirected.method = "HEAD"
        if redirected is not None and host != validate_public_url(request.full_url):
            for name in list(redirected.headers) + list(redirected.unredirected_hdrs):
                if name.lower() in {"authorization", "cookie", "proxy-authorization", "host"}:
                    redirected.remove_header(name)
        return redirected

    def http_error_302(self, request, response, code, message, headers):
        location = headers.get("Location") or headers.get("URI")
        if not location:
            return None
        try:
            redirected = self.redirect_request(request, response, code, message, headers,
                                               urljoin(request.full_url, location))
            if redirected is None:
                return None
            count = getattr(request, "_public_redirect_count", 0) + 1
            if count > MAX_REDIRECTS:
                raise urllib.error.HTTPError(request.full_url, code, "Too many repository redirects", headers, None)
            redirected._public_redirect_count = count
        finally:
            # urllib's default redirect handler reads the entire discarded body.
            # Close it instead so a redirect cannot evade metadata size limits.
            response.close()
        return self.parent.open(redirected, timeout=request.timeout)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def urlopen(request, *, timeout, allow_cross_host_redirects=False):
    """Read public HTTPS only, checking every redirect and every DNS connection.

    API requests remain on their original host. Asset/archive callers may allow
    public HTTPS CDN redirects; credentials are stripped on cross-host hops.
    Proxy environment settings are intentionally excluded from this direct
    public-host policy. TLS uses the existing operating-system trust store.
    """
    if type(timeout) not in {int, float} or not 0 < timeout <= 300:
        raise ValueError("Repository request timeout must be positive and bounded")
    request = urllib.request.Request(request) if isinstance(request, str) else request
    if not isinstance(request, urllib.request.Request) or request.get_method() not in {"GET", "HEAD"}:
        raise ValueError("Repository requests support GET or HEAD only")
    host = validate_public_url(request.full_url)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _PublicHTTPSHandler(context=ssl_context()),
        _PublicRedirectHandler(host, allow_cross_host_redirects))
    return opener.open(request, timeout=timeout)


def _read_json(reference, url, deadline, limit):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("Repository compatibility check timed out; try again")
    request = urllib.request.Request(url, headers=reference.headers)
    try:
        with urlopen(request, timeout=min(10, remaining)) as response:
            chunks, length = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Repository compatibility check timed out; try again")
                chunk = response.read(min(65536, limit + 1 - length))
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
                if length > limit:
                    raise ValueError("Repository compatibility metadata exceeds the supported size")
        value = json.loads(b"".join(chunks))
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise RuntimeError(f"{reference.host} could not verify a public Forgejo/Gitea repository "
                           f"(HTTP {status}); check the URL and try again") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError(f"Could not reach {reference.host} to verify Forgejo/Gitea compatibility; try again") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError(f"{reference.host} did not return valid Forgejo/Gitea API metadata") from None
    if not isinstance(value, dict):
        raise ValueError(f"{reference.host} did not return valid Forgejo/Gitea API metadata")
    return value


def detect_repository(reference):
    """Validate the public /api/v1 family and exact repository before syncing."""
    reference = reference if isinstance(reference, Repository) else parse_repository(reference)
    if reference.provider != "forgejo":
        raise ValueError("Forgejo/Gitea detection is only for non-GitHub repositories")
    deadline = time.monotonic() + DETECTION_SECONDS
    version_info = _read_json(reference, f"https://{reference.host}/api/v1/version", deadline, MAX_VERSION_BYTES)
    version = version_info.get("version")
    if (not isinstance(version, str) or len(version) > 128
            or not re.fullmatch(r"v?\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?", version)):
        raise ValueError(f"{reference.host} does not expose a supported Forgejo/Gitea version API")
    metadata = _read_json(reference, reference.api_url, deadline, MAX_REPOSITORY_BYTES)
    owner = metadata.get("owner")
    login = (owner.get("login") or owner.get("username")) if isinstance(owner, dict) else None
    if (type(metadata.get("id")) is not int or metadata["id"] <= 0
            or metadata.get("private") is not False
            or not isinstance(metadata.get("name"), str) or metadata["name"].casefold() != reference.name.casefold()
            or not isinstance(metadata.get("full_name"), str) or metadata["full_name"].casefold() != reference.slug.casefold()
            or not isinstance(login, str) or login.casefold() != reference.owner.casefold()):
        raise ValueError(f"{reference.host} did not identify the requested repository as public Forgejo/Gitea content")
    clone_url = metadata.get("clone_url")
    try:
        validate_public_url(clone_url, expected_host=reference.host)
        if urlsplit(clone_url).path.casefold() != ("/" + reference.slug + ".git").casefold() or urlsplit(clone_url).query:
            raise ValueError("Unexpected repository clone URL")
    except ValueError:
        raise ValueError(f"{reference.host} returned a clone URL that does not match this repository") from None
    return {**metadata, "provider": "forgejo", "compatible_family": "forgejo-gitea", "server_version": version}
