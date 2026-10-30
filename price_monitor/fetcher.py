"""Polite HTTP client: robots.txt, per-host rate limit, timeouts, limited retries.

Access controls are never worked around: a robots.txt disallow, HTTP 401/403
or a CAPTCHA/login page ends the attempt for that URL.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from typing import Callable, Optional
from urllib.parse import urljoin, urlsplit

import requests
from protego import Protego
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5


class FetchError(Exception):
    pass


class AccessDenied(FetchError):
    """robots.txt or the server said no. Not retried."""


class BudgetExceeded(FetchError):
    """The run's time budget is used up."""


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        timeout_seconds: float = 20.0,
        max_retries: int = 2,
        backoff_seconds: float = 2.0,
        min_interval_seconds: float = 5.0,
        max_retry_after_seconds: float = 60.0,
        budget_seconds: float = 120.0,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        resolve: Optional[Callable[[str], list[str]]] = None,
    ):
        self.user_agent = user_agent
        self._resolve = resolve or _resolve_host
        self.timeout = (min(10.0, timeout_seconds), timeout_seconds)  # (connect, read)
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.min_interval_seconds = min_interval_seconds
        self.max_retry_after_seconds = max_retry_after_seconds
        if session is None:
            session = requests.Session()
            session.mount("http://", PublicOnlyAdapter())
            session.mount("https://", PublicOnlyAdapter())
        self.session = session
        self.session.headers.update({"User-Agent": user_agent, "Accept-Language": "fi,en;q=0.8"})
        self._sleep = sleep
        self._clock = clock
        self._deadline = clock() + budget_seconds
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, Optional[Protego]] = {}

    # -- budget / rate limiting ---------------------------------------------

    def time_left(self) -> float:
        return self._deadline - self._clock()

    def _wait(self, seconds: float) -> None:
        if seconds > 0:
            if seconds > self.time_left():
                raise BudgetExceeded("run time budget exhausted")
            self._sleep(seconds)

    def _throttle(self, host: str) -> None:
        interval = self.min_interval_seconds
        robots = self._robots.get(host)
        if robots is not None:
            delay = robots.crawl_delay(self.user_agent)
            if delay:
                interval = max(interval, float(delay))
        last = self._last_request.get(host)
        if last is not None:
            self._wait(last + interval - self._clock())
        self._last_request[host] = self._clock()

    # -- robots.txt -------------------------------------------------------------

    def _robots_for(self, scheme: str, host: str) -> Optional[Protego]:
        """Parsed robots.txt, or None meaning "disallow everything" (RFC 9309 unreachable case)."""
        if host in self._robots:
            return self._robots[host]
        robots_url = f"{scheme}://{host}/robots.txt"
        parsed: Optional[Protego]
        try:
            # RFC 9309: follow up to five redirects for robots.txt itself.
            parsed = Protego.parse(self._follow(robots_url, check_robots=False).text)
        except AccessDenied as exc:
            log.warning("robots.txt for %s refused (%s); treating site as disallowed", host, exc)
            parsed = None
        except _NotFound:
            parsed = Protego.parse("")  # no robots.txt: everything allowed
        except FetchError as exc:
            log.warning("robots.txt for %s unavailable (%s); treating site as disallowed", host, exc)
            parsed = None
        self._robots[host] = parsed
        return parsed

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        robots = self._robots_for(parts.scheme, parts.netloc)
        return robots is not None and robots.can_fetch(url, self.user_agent)

    # -- requests -----------------------------------------------------------------

    def get(self, url: str) -> str:
        """GET a page allowed by robots.txt and return its text."""
        return self._follow(url, check_robots=True).text

    def _follow(self, url: str, check_robots: bool) -> requests.Response:
        """Request ``url``, following redirects by hand so every hop is checked
        (public http(s) address and, for pages, robots.txt of the hop's host)."""
        for _ in range(MAX_REDIRECTS + 1):
            _require_public_http_url(url, self._resolve)
            if check_robots and not self.allowed(url):
                raise AccessDenied(f"robots.txt disallows {url}")
            resp = self._request(url, urlsplit(url).netloc)
            if resp.status_code not in REDIRECT_STATUSES:
                return resp
            location = resp.headers.get("Location")
            if not location:
                raise FetchError(f"HTTP {resp.status_code} without Location for {url}")
            url = urljoin(url, location)
        raise FetchError(f"more than {MAX_REDIRECTS} redirects")

    def _request(self, url: str, host: str) -> requests.Response:
        attempt = 0
        while True:
            if self.time_left() <= 0:
                raise BudgetExceeded("run time budget exhausted")
            self._throttle(host)
            try:
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=False)
            except requests.ConnectionError as exc:
                if _caused_by_private_peer(exc):
                    raise AccessDenied(f"refusing {url}: connected address is not public") from exc
                error = f"{type(exc).__name__}: {exc}"
                retry_after = None
            except requests.Timeout as exc:
                error: str = f"{type(exc).__name__}: {exc}"
                retry_after = None
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code in (401, 403):
                    raise AccessDenied(f"HTTP {resp.status_code} for {url}")
                if resp.status_code in (404, 410):
                    raise _NotFound(f"HTTP {resp.status_code} for {url}")
                if resp.status_code not in RETRY_STATUSES:
                    raise FetchError(f"HTTP {resp.status_code} for {url}")
                error = f"HTTP {resp.status_code}"
                retry_after = _retry_after_seconds(resp)

            if attempt >= self.max_retries:
                raise FetchError(f"giving up on {url} after {attempt + 1} attempts: {error}")
            delay = self.backoff_seconds * (2 ** attempt)
            if retry_after is not None:
                if retry_after > self.max_retry_after_seconds:
                    raise FetchError(f"{url}: server asked to wait {retry_after:.0f}s; skipping this run")
                delay = max(delay, retry_after)
            attempt += 1
            log.info("Retry %d/%d for %s in %.1fs (%s)", attempt, self.max_retries, url, delay, error)
            self._wait(delay)


class _NotFound(FetchError):
    pass


# -- connection-level guard -------------------------------------------------------
#
# The URL checks below look at the URL text. This guard looks at the socket the HTTP
# library actually opened, after DNS and before any request bytes are sent, so URL
# parser differences, IDNA conversion and DNS rebinding cannot reach a private address.

class PrivatePeerError(OSError):
    pass


def _check_peer(sock) -> None:
    address = sock.getpeername()[0]
    if not ipaddress.ip_address(address.split("%", 1)[0]).is_global:
        sock.close()
        raise PrivatePeerError(f"connected to non-public address {address}")


class _GuardedHTTPConnection(HTTPConnection):
    def _new_conn(self):
        sock = super()._new_conn()
        _check_peer(sock)
        return sock


class _GuardedHTTPSConnection(HTTPSConnection):
    def _new_conn(self):  # HTTPSConnection.connect() wraps this socket in TLS afterwards
        sock = super()._new_conn()
        _check_peer(sock)
        return sock


class _GuardedHTTPPool(HTTPConnectionPool):
    ConnectionCls = _GuardedHTTPConnection


class _GuardedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _GuardedHTTPSConnection


class PublicOnlyAdapter(HTTPAdapter):
    """requests adapter whose direct connections may only reach public IP addresses."""

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _GuardedHTTPPool, "https": _GuardedHTTPSPool}


def _caused_by_private_peer(exc: BaseException) -> bool:
    """requests/urllib3 wrap socket errors several times; look through the wrappers."""
    seen, stack = set(), [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, PrivatePeerError):
            return True
        stack.extend(a for a in getattr(current, "args", ()) if isinstance(a, BaseException))
        stack.extend(e for e in (current.__cause__, current.__context__, getattr(current, "reason", None))
                     if isinstance(e, BaseException))
    return False


def _resolve_host(host: str) -> list[str]:
    try:
        return [info[4][0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]
    except socket.gaierror as exc:
        raise FetchError(f"cannot resolve {host}: {exc}") from exc


def _require_public_http_url(url: str, resolve: Callable[[str], list[str]]) -> None:
    """URLs come from fetched pages, so refuse anything that is not a public web address.

    The host must be a normal name or a canonical IP, and every address it resolves to
    must be public. This is the early, readable check; PublicOnlyAdapter re-checks the
    address actually connected to.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("http", "https") or not host:
        raise AccessDenied(f"refusing non-http(s) URL {url!r}")
    if "@" in parts.netloc or "\\" in url.split("?", 1)[0]:
        raise AccessDenied(f"refusing ambiguous URL authority in {url!r}")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise AccessDenied(f"refusing internal host {host!r}")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
        # "2130706433", "0x7f.1", "127.1": resolvers read these as IP addresses.
        # Same test as the WHATWG URL standard: the last label is a number.
        if re.fullmatch(r"\d+|0x[0-9a-f]*", host.rsplit(".", 1)[-1]):
            raise AccessDenied(f"refusing non-canonical numeric host {host!r}")
    addresses = [str(literal)] if literal else resolve(host)
    for address in addresses:
        if not ipaddress.ip_address(address.split("%", 1)[0]).is_global:
            raise AccessDenied(f"refusing {host!r}: resolves to non-public address {address}")


def _retry_after_seconds(resp: requests.Response) -> Optional[float]:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form: fall back to normal backoff
