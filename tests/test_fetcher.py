"""Fetcher tests with a fake session - no network access."""

import pytest
import requests

from price_monitor.fetcher import AccessDenied, BudgetExceeded, Fetcher, FetchError
from tests.conftest import fixture_text

ROBOTS = "https://shop.example/robots.txt"
PAGE = "https://shop.example/c/laptops"


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class FakeSession:
    """Returns queued responses (or raises queued exceptions) per URL."""

    def __init__(self, routes):
        self.routes = {url: list(items) for url, items in routes.items()}
        self.headers = {}
        self.calls = []

    def get(self, url, timeout=None, allow_redirects=True):
        self.calls.append((url, timeout))
        queue = self.routes.get(url)
        if not queue:
            return FakeResponse(404)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_fetcher(routes, clock=None, **kwargs):
    clock = clock or FakeClock()
    session = FakeSession(routes)
    kwargs.setdefault("min_interval_seconds", 0)
    fetcher = Fetcher("test-monitor/1.0", session=session, sleep=clock.sleep, clock=clock.time, **kwargs)
    return fetcher, session, clock


def ok_robots():
    return [FakeResponse(200, "User-agent: *\nDisallow:\n")]


def test_sends_user_agent_and_timeout():
    fetcher, session, _ = make_fetcher({ROBOTS: ok_robots(), PAGE: [FakeResponse(200, "<html>ok</html>")]},
                                       timeout_seconds=15)
    assert fetcher.get(PAGE) == "<html>ok</html>"
    assert session.headers["User-Agent"] == "test-monitor/1.0"
    assert all(timeout == (10.0, 15) for _, timeout in session.calls)


def test_retries_transient_errors_then_succeeds():
    fetcher, session, clock = make_fetcher({
        ROBOTS: ok_robots(),
        PAGE: [FakeResponse(503), requests.Timeout("slow"), FakeResponse(200, "done")],
    }, max_retries=2, backoff_seconds=1)
    assert fetcher.get(PAGE) == "done"
    assert [u for u, _ in session.calls].count(PAGE) == 3
    assert clock.sleeps == [1, 2]  # exponential backoff


def test_gives_up_after_limited_retries():
    fetcher, session, _ = make_fetcher({ROBOTS: ok_robots(), PAGE: [FakeResponse(500)]}, max_retries=2)
    with pytest.raises(FetchError, match="after 3 attempts"):
        fetcher.get(PAGE)
    assert [u for u, _ in session.calls].count(PAGE) == 3


def test_forbidden_is_not_retried():
    fetcher, session, _ = make_fetcher({ROBOTS: ok_robots(), PAGE: [FakeResponse(403)]})
    with pytest.raises(AccessDenied):
        fetcher.get(PAGE)
    assert [u for u, _ in session.calls].count(PAGE) == 1


def test_retry_after_is_honoured_and_capped():
    fetcher, _, clock = make_fetcher({
        ROBOTS: ok_robots(),
        PAGE: [FakeResponse(429, headers={"Retry-After": "20"}), FakeResponse(200, "ok")],
    }, backoff_seconds=1)
    assert fetcher.get(PAGE) == "ok"
    assert clock.sleeps == [20]

    fetcher, _, _ = make_fetcher({ROBOTS: ok_robots(), PAGE: [FakeResponse(429, headers={"Retry-After": "3600"})]})
    with pytest.raises(FetchError, match="asked to wait"):
        fetcher.get(PAGE)


def test_robots_wildcards_are_respected():
    fetcher, session, _ = make_fetcher({ROBOTS: [FakeResponse(200, fixture_text("robots_wildcards.txt"))]})
    assert fetcher.allowed("https://shop.example/kannettavat/123")
    assert not fetcher.allowed("https://shop.example/haku/?q=laptop")
    assert not fetcher.allowed("https://shop.example/tuote/abc/arvostele")
    assert not fetcher.allowed("https://shop.example/tuote.aspx")
    assert fetcher.allowed("https://shop.example/tuote.aspx/123")
    with pytest.raises(AccessDenied):
        fetcher.get("https://shop.example/haku/?q=laptop")
    assert [u for u, _ in session.calls] == [ROBOTS]  # robots.txt fetched once, page never


def test_robots_crawl_delay_and_min_interval():
    clock = FakeClock()
    fetcher, _, _ = make_fetcher({
        ROBOTS: [FakeResponse(200, fixture_text("robots_wildcards.txt"))],
        PAGE: [FakeResponse(200, "ok")],
    }, clock=clock, min_interval_seconds=5)
    fetcher.get(PAGE)
    fetcher.get(PAGE)
    assert clock.sleeps == [7, 7]  # Crawl-delay 7 > min interval 5, between all three requests


def test_missing_robots_allows_but_unreachable_robots_blocks():
    fetcher, _, _ = make_fetcher({PAGE: [FakeResponse(200, "ok")]})  # robots.txt -> 404
    assert fetcher.get(PAGE) == "ok"

    fetcher, _, _ = make_fetcher({ROBOTS: [FakeResponse(500)], PAGE: [FakeResponse(200, "ok")]})
    with pytest.raises(AccessDenied):
        fetcher.get(PAGE)


def test_time_budget_stops_fetching():
    clock = FakeClock()
    fetcher, _, _ = make_fetcher({ROBOTS: ok_robots(), PAGE: [FakeResponse(200, "ok")]},
                                 clock=clock, budget_seconds=10)
    fetcher.get(PAGE)
    clock.now = 11
    with pytest.raises(BudgetExceeded):
        fetcher.get(PAGE)
