"""Duplicate-alert prevention."""

from __future__ import annotations

from datetime import datetime

from .models import Offer, RuleResult
from .state import AlertRecord

# A repeat email needs a drop that is meaningful both relatively and in euros,
# so 749.00 -> 748.90 stays quiet but 749 -> 699 is worth knowing about.
REALERT_MIN_DROP_FRACTION = 0.05
REALERT_MIN_DROP_EUR = 10.0


def alert_key(offer: Offer, result: RuleResult) -> str:
    """One alert identity per rule + source + product (the exact listing/variant)."""
    return f"{result.rule}|{offer.source}|{offer.product_key}"


def should_realert(previous: AlertRecord, current_price: float, now: datetime) -> bool:
    """Decide whether a product that was already alerted deserves another email.

    Called only when the product matches its rule again on a later run.
    ``previous.price`` is the price in the last email, ``previous.sent_at``
    is an ISO 8601 timestamp of that email.

    Policy: only a further price drop of at least 5 % *and* 10 € since the
    last email. The same or a higher price never re-alerts, however much time
    has passed, so a long-running deal is reported once.
    """
    drop = previous.price - current_price
    return drop >= REALERT_MIN_DROP_EUR and drop >= previous.price * REALERT_MIN_DROP_FRACTION
