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


def fake_dns(host):
    """No real DNS in tests: one name points inside, everything else is public."""
    return ["10.1.2.3"] if host == "intranet.example" else ["93.184.215.14", "2606:2800:21f:cb07::1"]


def make_fetcher(routes, clock=None, **kwargs):
    clock = clock or FakeClock()
    session = FakeSession(routes)
    kwargs.setdefault("min_interval_seconds", 0)
    kwargs.setdefault("resolve", fake_dns)
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


def redirect(location, status=301):
    return FakeResponse(status, headers={"Location": location})


def test_redirects_are_followed_and_rechecked_against_robots():
    moved = "https://shop.example/c/laptops-new"
    fetcher, session, _ = make_fetcher({ROBOTS: ok_robots(), PAGE: [redirect("/c/laptops-new")],
                                        moved: [FakeResponse(200, "moved ok")]})
    assert fetcher.get(PAGE) == "moved ok"
    assert all(timeout for _, timeout in session.calls)

    disallowed = "https://shop.example/haku/?q=x"
    fetcher, session, _ = make_fetcher({ROBOTS: [FakeResponse(200, fixture_text("robots_wildcards.txt"))],
                                        PAGE: [redirect(disallowed)]})
    with pytest.raises(AccessDenied, match="robots"):
        fetcher.get(PAGE)
    assert disallowed not in [u for u, _ in session.calls]


@pytest.mark.parametrize("target", [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:8080/admin",
    "http://[::1]/",
    "http://localhost/",
    "http://service.cluster.local/",
    "http://localhost./",              # trailing dot
    "http://2130706433/",              # 127.0.0.1 as one number
    "http://0x7f.1/",
    "http://127.1/",
    "http://intranet.example/",        # a name that resolves to a private address
    "file:///etc/passwd",
])
def test_redirects_to_internal_addresses_are_refused(target):
    fetcher, session, _ = make_fetcher({ROBOTS: ok_robots(), PAGE: [redirect(target, 302)]})
    with pytest.raises(AccessDenied):
        fetcher.get(PAGE)
    assert [u for u, _ in session.calls] == [ROBOTS, PAGE]


def test_internal_url_is_refused_before_any_request():
    fetcher, session, _ = make_fetcher({})
    with pytest.raises(AccessDenied):
        fetcher.get("http://10.0.0.5/product")
    assert session.calls == []


def test_robots_txt_redirect_is_followed():
    fetcher, _, _ = make_fetcher({
        "http://shop.example/robots.txt": [redirect("https://shop.example/robots.txt")],
        ROBOTS: [FakeResponse(200, fixture_text("robots_wildcards.txt"))],
    })
    assert not fetcher.allowed("http://shop.example/haku/x")


def test_userinfo_and_backslash_authorities_are_refused():
    fetcher, session, _ = make_fetcher({})
    for url in ("https://shop.example@10.0.0.1/", "https://shop.example\\@10.0.0.1/"):
        with pytest.raises(AccessDenied):
            fetcher.get(url)
    assert session.calls == []


def test_connection_guard_blocks_private_peer_even_if_dns_check_was_fooled(monkeypatch, caplog):
    """DNS-rebinding stand-in: the early check is told the host is public, but the socket
    really goes to 127.0.0.1. The guarded adapter must refuse before any request is sent."""
    import http.server
    import threading

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"User-agent: *\nDisallow:\n")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        monkeypatch.setattr("price_monitor.fetcher._require_public_http_url", lambda url, resolve: None)
        fetcher = Fetcher("test-monitor/1.0", max_retries=0, min_interval_seconds=0)  # real, guarded session
        with pytest.raises(AccessDenied):
            fetcher.get(f"http://127.0.0.1:{port}/page")
        assert "connected address is not public" in caplog.text  # refused at the socket
        assert hits == []  # not even robots.txt reached the server
    finally:
        server.shutdown()


def test_redirect_loop_gives_up():
    fetcher, _, _ = make_fetcher({ROBOTS: ok_robots(), PAGE: [redirect(PAGE)]})
    with pytest.raises(FetchError, match="redirects"):
        fetcher.get(PAGE)


def test_time_budget_stops_fetching():
    clock = FakeClock()
    fetcher, _, _ = make_fetcher({ROBOTS: ok_robots(), PAGE: [FakeResponse(200, "ok")]},
                                 clock=clock, budget_seconds=10)
    fetcher.get(PAGE)
    clock.now = 11
    with pytest.raises(BudgetExceeded):
        fetcher.get(PAGE)
