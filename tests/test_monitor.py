"""End-to-end runs with a fake fetcher serving fixture pages."""

from datetime import timedelta

import pytest

from price_monitor.config import CategoryPage, Config, MonitorSettings, RulesConfig, SourceConfig
from price_monitor.fetcher import FetchError
from price_monitor.models import Category
from price_monitor.monitor import run_once
from price_monitor.notify import NotifyError
from price_monitor.state import State
from tests.conftest import fixture_text

LAPTOPS = "https://shop.example/kannettavat"
MONITORS = "https://shop.example/naytot"


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        if url not in self.pages:
            raise FetchError(f"HTTP 404 for {url}")
        return self.pages[url]


class RecordingNotifier:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, alerts, problems):
        self.calls.append((alerts, problems))
        if self.fail:
            raise NotifyError("SMTP down")


def make_config(*pages, threshold=2):
    return Config(
        monitor=MonitorSettings(user_agent="test", source_failure_alert_threshold=threshold),
        rules=RulesConfig(),
        sources=[SourceConfig("shop", True, [CategoryPage(cat, url) for cat, url in pages])],
    )


def test_laptop_alert_once_then_deduplicated(now):
    config = make_config((Category.LAPTOP, LAPTOPS))
    fetcher = FakeFetcher({LAPTOPS: fixture_text("listing_jsonld.html")})
    state, notifier = State(), RecordingNotifier()

    report = run_once(config, state, fetcher, now, notifier)
    assert [a.offer.title.split(" (")[0] for a in report.alerts] == ["Examplebook Pro 14"]
    assert report.verdicts == {"match": 1, "no_match": 2, "unknown": 1}
    assert len(notifier.calls) == 1

    report = run_once(config, state, fetcher, now + timedelta(hours=1), notifier)
    assert report.alerts == []
    assert len(notifier.calls) == 1  # no duplicate email
    assert state.history("shop", "https://shop.example/kannettavat/examplebook-pro-14/1001")[0].count == 2


def test_failed_email_keeps_alert_pending(now):
    config = make_config((Category.LAPTOP, LAPTOPS))
    fetcher = FakeFetcher({LAPTOPS: fixture_text("listing_jsonld.html")})
    state = State()

    report = run_once(config, state, fetcher, now, RecordingNotifier(fail=True))
    assert report.email_failed and state.alerts == {}

    retry = RecordingNotifier()
    report = run_once(config, state, fetcher, now + timedelta(hours=1), retry)
    assert len(report.alerts) == 1 and len(retry.calls) == 1


def test_dry_run_sends_and_marks_nothing(now):
    config = make_config((Category.LAPTOP, LAPTOPS))
    state = State()
    report = run_once(config, state, FakeFetcher({LAPTOPS: fixture_text("listing_jsonld.html")}), now, None)
    assert len(report.alerts) == 1 and state.alerts == {}


def test_monitor_specs_looked_up_on_product_page_and_cached(now):
    detail_url = "https://shop.example/naytot/examplevision-34uw/3001"
    config = make_config((Category.MONITOR, MONITORS))
    fetcher = FakeFetcher({MONITORS: fixture_text("monitor_listing.html"), detail_url: fixture_text("monitor_detail.html")})
    state = State()

    report = run_once(config, state, fetcher, now, RecordingNotifier())
    assert [a.offer.url for a in report.alerts] == [detail_url]
    # The VA monitor is a definite NO_MATCH, so its product page is never opened.
    assert fetcher.requested == [MONITORS, detail_url]

    fetcher.requested.clear()
    run_once(config, state, fetcher, now + timedelta(hours=1), RecordingNotifier())
    assert fetcher.requested == [MONITORS]  # specs came from the cache


def test_product_pages_on_other_hosts_are_never_fetched(now):
    listing = """<script type="application/ld+json">{"@type": "ItemList", "itemListElement": [
      {"@type": "ListItem", "item": {"@type": "Product", "name": "Example Laptop",
       "url": "http://169.254.169.254/latest/meta-data/",
       "offers": {"@type": "Offer", "price": 500, "priceCurrency": "EUR"}}}]}</script>"""
    fetcher = FakeFetcher({LAPTOPS: listing})
    report = run_once(make_config((Category.LAPTOP, LAPTOPS)), State(), fetcher, now, None)
    assert report.verdicts["unknown"] == 1      # RAM unknown, but no detail fetch
    assert fetcher.requested == [LAPTOPS]


def test_phone_alert_needs_own_history(now):
    phones = "https://shop.example/puhelimet"
    config = make_config((Category.PHONE, phones))
    key = "https://shop.example/puhelimet/examplephone-12-256gb/2001"
    state = State()
    fetcher = FakeFetcher({phones: fixture_text("offer_types.html")})

    # No history yet: the 999 € strikethrough price is NOT accepted as proof.
    report = run_once(config, state, fetcher, now, RecordingNotifier())
    assert report.alerts == [] and report.verdicts["unknown"] == 2

    # After the monitor itself has seen the phone at 749 € for days, the drop alerts.
    fresh = State()
    for hour in range(0, 96, 8):
        fresh.record_price("shop", key, "Examplephone 12 256 GB", 749.0, now - timedelta(days=5, hours=-hour))
    report = run_once(config, fresh, fetcher, now, RecordingNotifier())
    assert [a.offer.product_key for a in report.alerts] == [key]
    assert report.alerts[0].result.reference_price_eur == 749.0


def test_broken_source_reported_once_after_threshold(now):
    config = make_config((Category.LAPTOP, LAPTOPS), threshold=2)
    fetcher = FakeFetcher({})  # every request fails
    state, notifier = State(), RecordingNotifier()

    run_once(config, state, fetcher, now, notifier)
    assert notifier.calls == []
    run_once(config, state, fetcher, now + timedelta(hours=1), notifier)
    assert len(notifier.calls) == 1 and notifier.calls[0][1][0].consecutive_failures == 2
    run_once(config, state, fetcher, now + timedelta(hours=2), notifier)
    assert len(notifier.calls) == 1  # not repeated every hour


def test_page_without_product_data_counts_as_failure(now):
    config = make_config((Category.LAPTOP, LAPTOPS))
    state = State()
    report = run_once(config, state, FakeFetcher({LAPTOPS: "<html><body>no data</body></html>"}), now, None)
    assert "no offers extracted" in report.source_errors["shop"]
    assert state.sources["shop"]["failures"] == 1


@pytest.mark.parametrize("enabled", [False])
def test_disabled_sources_are_not_fetched(now, enabled):
    config = make_config((Category.LAPTOP, LAPTOPS))
    config.sources[0].enabled = enabled
    fetcher = FakeFetcher({})
    run_once(config, State(), fetcher, now, None)
    assert fetcher.requested == []
