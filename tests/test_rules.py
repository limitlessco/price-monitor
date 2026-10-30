from datetime import timedelta

from price_monitor.config import LaptopRule, MonitorRule, PhoneRule, RulesConfig
from price_monitor.models import Category, Offer, Specs, Verdict
from price_monitor.rules import check_price, evaluate, reference_price
from price_monitor.state import PricePoint


def offer(category, price, shipping=None, **specs):
    return Offer("test", category, "https://shop.example/p/1", "Example", "https://shop.example/p/1",
                 price, shipping, specs=Specs(**specs))


def point(price, start, hours, count):
    return PricePoint(price, start.isoformat(), (start + timedelta(hours=hours)).isoformat(), count)


# -- price / shipping ---------------------------------------------------------

def test_price_check_uses_total_when_shipping_known():
    assert check_price(offer(Category.LAPTOP, 795, shipping=9.9), 800, True)[0] is Verdict.NO_MATCH
    assert check_price(offer(Category.LAPTOP, 780, shipping=9.9), 800, True)[0] is Verdict.MATCH


def test_price_check_unknown_shipping_is_configurable():
    o = offer(Category.LAPTOP, 780)
    assert check_price(o, 800, allow_unknown_shipping=True)[0] is Verdict.MATCH
    assert check_price(o, 800, allow_unknown_shipping=False)[0] is Verdict.UNKNOWN
    assert check_price(offer(Category.LAPTOP, 800), 800, True)[0] is Verdict.NO_MATCH  # "below" is strict


# -- laptops -----------------------------------------------------------------------

def test_laptop_rules():
    rules = RulesConfig()
    assert evaluate(offer(Category.LAPTOP, 749, ram_gb=32), rules, []).verdict is Verdict.MATCH
    assert evaluate(offer(Category.LAPTOP, 749, ram_gb=64), rules, []).verdict is Verdict.MATCH
    assert evaluate(offer(Category.LAPTOP, 749, ram_gb=16), rules, []).verdict is Verdict.NO_MATCH
    assert evaluate(offer(Category.LAPTOP, 999, ram_gb=32), rules, []).verdict is Verdict.NO_MATCH
    unknown = evaluate(offer(Category.LAPTOP, 749), rules, [])
    assert unknown.verdict is Verdict.UNKNOWN and unknown.missing_specs == ["ram_gb"]


def test_unknown_spec_does_not_hide_a_definite_no_match():
    # Too expensive is decisive even though RAM is unknown.
    assert evaluate(offer(Category.LAPTOP, 999), RulesConfig(), []).verdict is Verdict.NO_MATCH


# -- monitors ----------------------------------------------------------------------

GOOD_MONITOR = dict(screen_inches=34.0, panel_ips=True, refresh_hz=120, usb_c_pd_watts=90)


def test_monitor_all_specs_match():
    assert evaluate(offer(Category.MONITOR, 289, **GOOD_MONITOR), RulesConfig(), []).verdict is Verdict.MATCH


def test_monitor_each_failing_spec():
    rules = RulesConfig()
    for field, bad in [("screen_inches", 32.0), ("panel_ips", False), ("refresh_hz", 75), ("usb_c_pd_watts", 45)]:
        specs = {**GOOD_MONITOR, field: bad}
        assert evaluate(offer(Category.MONITOR, 289, **specs), rules, []).verdict is Verdict.NO_MATCH, field


def test_monitor_missing_spec_is_unknown():
    specs = {**GOOD_MONITOR, "usb_c_pd_watts": None, "panel_ips": None}
    result = evaluate(offer(Category.MONITOR, 289, **specs), RulesConfig(), [])
    assert result.verdict is Verdict.UNKNOWN
    assert set(result.missing_specs) == {"usb_c_pd_watts", "panel_ips"}


def test_monitor_ips_requirement_can_be_disabled():
    rules = RulesConfig(monitor=MonitorRule(require_ips=False))
    specs = {**GOOD_MONITOR, "panel_ips": None}
    assert evaluate(offer(Category.MONITOR, 289, **specs), rules, []).verdict is Verdict.MATCH


# -- phones: reference price from own history only ------------------------------

def test_reference_price_unknown_without_enough_history(now):
    assert reference_price([], 3, 48) is None
    assert reference_price([point(750, now, 1, 2)], 3, 48) is None        # too few observations
    assert reference_price([point(750, now, 10, 10)], 3, 48) is None      # span too short


def test_reference_price_is_weighted_median(now):
    history = [point(749, now, 72, 60), point(999, now + timedelta(hours=72), 1, 1)]  # a one-run spike
    assert reference_price(history, 3, 48) == 749


def test_phone_rule(now):
    rules = RulesConfig()
    long_high = [point(749, now - timedelta(days=10), 200, 200)]
    long_low = [point(450, now - timedelta(days=10), 200, 200)]
    assert evaluate(offer(Category.PHONE, 279), rules, long_high).verdict is Verdict.MATCH
    assert evaluate(offer(Category.PHONE, 279), rules, long_low).verdict is Verdict.NO_MATCH
    assert evaluate(offer(Category.PHONE, 349), rules, long_high).verdict is Verdict.NO_MATCH

    unknown = evaluate(offer(Category.PHONE, 279), rules, [])
    assert unknown.verdict is Verdict.UNKNOWN
    assert unknown.reference_price_eur is None
    assert any("reference price unknown" in r for r in unknown.reasons)


def test_phone_rule_thresholds_from_config(now):
    rules = RulesConfig(phone=PhoneRule(min_reference_price_eur=400))
    history = [point(450, now - timedelta(days=10), 200, 200)]
    assert evaluate(offer(Category.PHONE, 279), rules, history).verdict is Verdict.MATCH


def test_laptop_threshold_from_config():
    rules = RulesConfig(laptop=LaptopRule(max_price_eur=900, min_ram_gb=16))
    assert evaluate(offer(Category.LAPTOP, 850, ram_gb=16), rules, []).verdict is Verdict.MATCH
