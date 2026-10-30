"""Polite HTTP client: robots.txt, per-host rate limit, timeouts, limited retries.

Access controls are never worked around: a robots.txt disallow, HTTP 401/403
or a CAPTCHA/login page ends the attempt for that URL.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional
from urllib.parse import urlsplit

import requests
from protego import Protego

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


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
    ):
        self.user_agent = user_agent
        self.timeout = (min(10.0, timeout_seconds), timeout_seconds)  # (connect, read)
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.min_interval_seconds = min_interval_seconds
        self.max_retry_after_seconds = max_retry_after_seconds
        self.session = session or requests.Session()
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
            resp = self._request(robots_url, host)
            parsed = Protego.parse(resp.text)
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
        if not self.allowed(url):
            raise AccessDenied(f"robots.txt disallows {url}")
        return self._request(url, urlsplit(url).netloc).text

    def _request(self, url: str, host: str) -> requests.Response:
        attempt = 0
        while True:
            if self.time_left() <= 0:
                raise BudgetExceeded("run time budget exhausted")
            self._throttle(host)
            try:
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            except (requests.ConnectionError, requests.Timeout) as exc:
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


def _retry_after_seconds(resp: requests.Response) -> Optional[float]:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form: fall back to normal backoff
