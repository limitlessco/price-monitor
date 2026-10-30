"""Persistent JSON state: price history, cached specs, sent alerts, source health.

In GitHub Actions the file lives on a separate ``monitor-state`` branch
(see .github/workflows/price-monitor.yml). Price history is run-length
encoded: a new point is added only when the price changes.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .models import Specs

STATE_VERSION = 1
MAX_POINTS_PER_PRODUCT = 500


class StateError(Exception):
    pass


@dataclass
class PricePoint:
    price: float
    first_seen: str  # ISO 8601 UTC
    last_seen: str
    count: int = 1  # number of runs that observed this price


@dataclass
class AlertRecord:
    price: float
    sent_at: str


def _iso(ts: datetime) -> str:
    return ts.isoformat(timespec="seconds")


class State:
    def __init__(self, data: Optional[dict] = None):
        data = data or {}
        self.products: dict[str, dict] = data.get("products", {})
        self.alerts: dict[str, dict] = data.get("alerts", {})
        self.sources: dict[str, dict] = data.get("sources", {})

    # -- persistence ----------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "State":
        path = Path(path)
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            # Starting from empty would re-send every alert, so fail loudly instead.
            raise StateError(f"{path} is corrupted: {exc}") from exc
        if data.get("version") != STATE_VERSION:
            raise StateError(f"{path}: unsupported state version {data.get('version')!r}")
        return cls(data)

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {"version": STATE_VERSION, "products": self.products, "alerts": self.alerts, "sources": self.sources}
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)  # atomic: a crash never leaves a half-written state file

    # -- price history ----------------------------------------------------------

    @staticmethod
    def product_id(source: str, product_key: str) -> str:
        return f"{source}|{product_key}"

    def history(self, source: str, product_key: str) -> list[PricePoint]:
        entry = self.products.get(self.product_id(source, product_key), {})
        return [PricePoint(**p) for p in entry.get("history", [])]

    def record_price(self, source: str, product_key: str, title: str, price: float, now: datetime) -> None:
        entry = self.products.setdefault(self.product_id(source, product_key), {"history": []})
        entry["title"] = title
        entry["last_seen"] = _iso(now)
        history = entry["history"]
        if history and history[-1]["price"] == price:
            history[-1]["last_seen"] = _iso(now)
            history[-1]["count"] += 1
        else:
            history.append(asdict(PricePoint(price, _iso(now), _iso(now))))
            del history[:-MAX_POINTS_PER_PRODUCT]

    # -- spec cache ---------------------------------------------------------------

    def cached_specs(self, source: str, product_key: str, max_age_days: int, now: datetime) -> Optional[Specs]:
        """Specs from an earlier detail-page fetch, if fetched recently enough."""
        entry = self.products.get(self.product_id(source, product_key), {})
        fetched = entry.get("specs_fetched_at")
        if not fetched or now - datetime.fromisoformat(fetched) > timedelta(days=max_age_days):
            return None
        return Specs.from_dict(entry.get("specs", {}))

    def store_specs(self, source: str, product_key: str, specs: Specs, now: datetime) -> None:
        entry = self.products.setdefault(self.product_id(source, product_key), {"history": []})
        entry["specs"] = specs.to_dict()
        entry["specs_fetched_at"] = _iso(now)

    # -- alerts -------------------------------------------------------------------------

    def alert_record(self, key: str) -> Optional[AlertRecord]:
        record = self.alerts.get(key)
        return AlertRecord(**record) if record else None

    def mark_alerted(self, key: str, price: float, now: datetime) -> None:
        self.alerts[key] = asdict(AlertRecord(price, _iso(now)))

    # -- source health ------------------------------------------------------------------

    def record_source_result(self, source: str, ok: bool, error: str = "") -> int:
        """Track consecutive failed runs per source; returns the current streak."""
        entry = self.sources.setdefault(source, {"failures": 0, "notified": False, "last_error": ""})
        if ok:
            entry.update(failures=0, notified=False, last_error="")
        else:
            entry["failures"] += 1
            entry["last_error"] = error[:500]
        return entry["failures"]

    def source_needs_notice(self, source: str, threshold: int) -> bool:
        entry = self.sources.get(source, {})
        return entry.get("failures", 0) >= threshold and not entry.get("notified", False)

    def mark_source_notified(self, source: str) -> None:
        self.sources.setdefault(source, {"failures": 0, "last_error": ""})["notified"] = True

    # -- housekeeping ------------------------------------------------------------------

    def prune(self, now: datetime, retention_days: int) -> None:
        """Forget products (and their alerts) not seen within the retention period."""
        cutoff = now - timedelta(days=retention_days)
        stale = [pid for pid, e in self.products.items()
                 if datetime.fromisoformat(e.get("last_seen", e.get("specs_fetched_at", _iso(now)))) < cutoff]
        for pid in stale:
            del self.products[pid]
        self.alerts = {k: v for k, v in self.alerts.items() if datetime.fromisoformat(v["sent_at"]) >= cutoff}
