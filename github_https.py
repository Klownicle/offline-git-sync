"""Verified HTTPS using the operating system's certificate-chain validation."""

from functools import lru_cache
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


@lru_cache(maxsize=1)
def ssl_context():
    # Keep this tool's dependency separate from the system Python installation.
    dependencies = Path(__file__).resolve().parent / ".tools" / "python"
    if dependencies.is_dir() and str(dependencies) not in sys.path:
        sys.path.insert(0, str(dependencies))
    try:
        import truststore
    except ImportError as error:
        raise RuntimeError(
            "Git sync requires truststore. Restore .tools/python or install "
            "truststore==0.10.5 there using this Python's pip --target option."
        ) from error
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    return context


def urlopen(request, *, timeout):
    # Explicit context also applies to HTTPS redirects, including release assets.
    # Do not alter ssl defaults or weaken verification for other Python programs.
    # Retry only temporary server failures on these read-only GET requests.
    # Missing repositories, authentication failures, and TLS failures stay errors.
    method = request.get_method() if isinstance(request, urllib.request.Request) else "GET"
    for attempt in range(4):
        try:
            return urllib.request.urlopen(request, timeout=timeout, context=ssl_context())
        except urllib.error.HTTPError as error:
            if method != "GET" or error.code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise
            delay = 2 ** (attempt + 1)
            retry_after = error.headers.get("Retry-After") if error.headers else None
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    try:
                        retry_date = parsedate_to_datetime(retry_after)
                        delay = max(delay, (retry_date - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError, OverflowError):
                        pass
            # Do not violate a longer server-requested cooldown or block a task
            # for an unbounded wait: leave that request to the next scheduled run.
            if delay > 30:
                raise
            url = request.full_url if isinstance(request, urllib.request.Request) else request
            host = urllib.parse.urlsplit(url).hostname
            message = (f"{datetime.now(timezone.utc).isoformat()} Temporary HTTP {error.code} "
                       f"from {host}; retry {attempt + 1}/3 in {delay:g}s")
            try:
                with (Path(__file__).resolve().parent / "sync.log").open("a", encoding="utf-8") as output:
                    output.write(message + "\n")
            except OSError:
                pass
            if sys.stderr is not None:
                print(message, file=sys.stderr, flush=True)
            error.close()
            time.sleep(delay)
