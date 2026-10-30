"""Product rules. Each check gives MATCH, NO_MATCH or UNKNOWN; only MATCH alerts.

Combination: any NO_MATCH -> NO_MATCH, otherwise any UNKNOWN -> UNKNOWN,
otherwise MATCH.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from .config import PhoneRule, LaptopRule, MonitorRule, RulesConfig
from .models import Category, Offer, RuleResult, Verdict
from .state import PricePoint

Check = tuple[Verdict, str]


def _combine(rule: str, checks: list[Check], missing: list[str], reference: Optional[float] = None) -> RuleResult:
    verdicts = {v for v, _ in checks}
    if Verdict.NO_MATCH in verdicts:
        verdict = Verdict.NO_MATCH
    elif Verdict.UNKNOWN in verdicts:
        verdict = Verdict.UNKNOWN
    else:
        verdict = Verdict.MATCH
    return RuleResult(rule, verdict, [r for _, r in checks], missing, reference)


def check_price(offer: Offer, limit: float, allow_unknown_shipping: bool) -> Check:
    if offer.price_eur >= limit:
        return Verdict.NO_MATCH, f"price {offer.price_eur:.2f} € is not below {limit:.0f} €"
    total = offer.total_price_eur
    if total is not None:
        if total < limit:
            return Verdict.MATCH, f"total {total:.2f} € (incl. shipping) < {limit:.0f} €"
        return Verdict.NO_MATCH, f"total {total:.2f} € (incl. shipping) is not below {limit:.0f} €"
    reason = f"price {offer.price_eur:.2f} € < {limit:.0f} €, shipping unknown"
    return (Verdict.MATCH if allow_unknown_shipping else Verdict.UNKNOWN), reason


def _at_least(value, minimum, label: str, unit: str, missing: list[str], field: str) -> Check:
    if value is None:
        missing.append(field)
        return Verdict.UNKNOWN, f"{label} unknown"
    if value >= minimum:
        return Verdict.MATCH, f"{label} {value}{unit} >= {minimum}{unit}"
    return Verdict.NO_MATCH, f"{label} {value}{unit} < {minimum}{unit}"


def evaluate_laptop(offer: Offer, rule: LaptopRule, allow_unknown_shipping: bool) -> RuleResult:
    missing: list[str] = []
    checks = [
        check_price(offer, rule.max_price_eur, allow_unknown_shipping),
        _at_least(offer.specs.ram_gb, rule.min_ram_gb, "RAM", " GB", missing, "ram_gb"),
    ]
    return _combine("laptop", checks, missing)


def evaluate_monitor(offer: Offer, rule: MonitorRule, allow_unknown_shipping: bool) -> RuleResult:
    missing: list[str] = []
    s = offer.specs
    checks = [
        check_price(offer, rule.max_price_eur, allow_unknown_shipping),
        _at_least(s.screen_inches, rule.min_screen_inches, "screen", '"', missing, "screen_inches"),
        _at_least(s.refresh_hz, rule.min_refresh_hz, "refresh", " Hz", missing, "refresh_hz"),
        _at_least(s.usb_c_pd_watts, rule.min_usb_c_pd_watts, "USB-C PD", " W", missing, "usb_c_pd_watts"),
    ]
    if rule.require_ips:
        if s.panel_ips is None:
            missing.append("panel_ips")
            checks.append((Verdict.UNKNOWN, "panel type unknown"))
        else:
            checks.append((Verdict.MATCH, "IPS panel") if s.panel_ips else (Verdict.NO_MATCH, "not an IPS panel"))
    return _combine("monitor", checks, missing)


def reference_price(history: list[PricePoint], min_observations: int, min_span_hours: float) -> Optional[float]:
    """Typical price of this exact product from the monitor's *own* past observations.

    Uses only prices this tool recorded on earlier runs - never a crossed-out
    "was" price or a site's highPrice. Returns None (unknown) when there are
    too few observations or they cover too short a period.
    """
    observations = sum(p.count for p in history)
    if not history or observations < min_observations:
        return None
    first = min(datetime.fromisoformat(p.first_seen) for p in history)
    last = max(datetime.fromisoformat(p.last_seen) for p in history)
    if (last - first).total_seconds() / 3600 < min_span_hours:
        return None
    # Observation-weighted median: one-off spikes or dips do not set the reference.
    cumulative = 0
    for point in sorted(history, key=lambda p: p.price):
        cumulative += point.count
        if cumulative * 2 >= observations:
            return point.price
    return None  # unreachable


def evaluate_phone(
    offer: Offer, rule: PhoneRule, allow_unknown_shipping: bool, history: list[PricePoint]
) -> RuleResult:
    checks = [check_price(offer, rule.max_price_eur, allow_unknown_shipping)]
    ref = reference_price(history, rule.min_history_observations, rule.min_history_span_hours)
    if ref is None:
        checks.append((Verdict.UNKNOWN, "reference price unknown (insufficient own price history)"))
    elif ref > rule.min_reference_price_eur:
        checks.append((Verdict.MATCH, f"reference price {ref:.2f} € > {rule.min_reference_price_eur:.0f} €"))
    else:
        checks.append((Verdict.NO_MATCH, f"reference price {ref:.2f} € is not above {rule.min_reference_price_eur:.0f} €"))
    return _combine("phone", checks, [], ref)


def evaluate(offer: Offer, rules: RulesConfig, history: list[PricePoint]) -> RuleResult:
    if offer.category is Category.LAPTOP:
        return evaluate_laptop(offer, rules.laptop, rules.allow_unknown_shipping)
    if offer.category is Category.MONITOR:
        return evaluate_monitor(offer, rules.monitor, rules.allow_unknown_shipping)
    return evaluate_phone(offer, rules.phone, rules.allow_unknown_shipping, history)
