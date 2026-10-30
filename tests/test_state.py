from datetime import timedelta

import pytest

from price_monitor.models import Specs
from price_monitor.state import State, StateError


def test_history_is_run_length_encoded(now):
    state = State()
    for hour, price in enumerate([500, 500, 500, 450, 500]):
        state.record_price("src", "key", "Title", price, now + timedelta(hours=hour))
    history = state.history("src", "key")
    assert [(p.price, p.count) for p in history] == [(500, 3), (450, 1), (500, 1)]
    assert history[0].last_seen == (now + timedelta(hours=2)).isoformat()


def test_save_and_load_roundtrip(tmp_path, now):
    path = tmp_path / "state" / "state.json"
    state = State()
    state.record_price("src", "key", "Title", 279.0, now)
    state.mark_alerted("phone|src|key", 279.0, now)
    state.store_specs("src", "key", Specs(ram_gb=32), now)
    state.save(path)

    loaded = State.load(path)
    assert loaded.history("src", "key")[0].price == 279.0
    assert loaded.alert_record("phone|src|key").price == 279.0
    assert loaded.cached_specs("src", "key", 30, now) == Specs(ram_gb=32)
    assert not list(path.parent.glob("*.tmp"))


def test_missing_state_file_starts_empty(tmp_path):
    assert State.load(tmp_path / "nope.json").products == {}


def test_corrupted_state_fails_loudly(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ truncated", encoding="utf-8")
    with pytest.raises(StateError):
        State.load(path)


def test_spec_cache_expires(now):
    state = State()
    state.store_specs("src", "key", Specs(ram_gb=32), now)
    assert state.cached_specs("src", "key", 30, now + timedelta(days=29)) is not None
    assert state.cached_specs("src", "key", 30, now + timedelta(days=31)) is None


def test_source_failure_notice_once(now):
    state = State()
    for _ in range(3):
        state.record_source_result("src", ok=False, error="HTTP 500")
    assert state.source_needs_notice("src", threshold=3)
    state.mark_source_notified("src")
    state.record_source_result("src", ok=False, error="HTTP 500")
    assert not state.source_needs_notice("src", threshold=3)   # already told the user
    state.record_source_result("src", ok=True)
    assert state.sources["src"] == {"failures": 0, "notified": False, "last_error": ""}


def test_prune_forgets_stale_products_and_alerts(now):
    state = State()
    state.record_price("src", "old", "Old", 100, now - timedelta(days=400))
    state.mark_alerted("laptop|src|old", 100, now - timedelta(days=400))
    state.record_price("src", "new", "New", 100, now)
    state.prune(now, retention_days=365)
    assert list(state.products) == ["src|new"]
    assert state.alerts == {}
