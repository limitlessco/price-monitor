from datetime import timedelta

import pytest

from price_monitor.alerts import should_realert
from price_monitor.state import AlertRecord


@pytest.mark.parametrize("previous, current, expected", [
    (749.0, 749.0, False),   # unchanged
    (749.0, 779.0, False),   # price went up
    (749.0, 740.0, False),   # 9 € / 1.2 %: too small
    (749.0, 699.0, True),    # 50 € / 6.7 %
    (150.0, 141.0, False),   # 6 % but only 9 €
    (150.0, 140.0, True),    # exactly 10 € and 6.7 %: thresholds are inclusive
    (2000.0, 1985.0, False), # 15 € but under 5 %
])
def test_realert_only_on_meaningful_further_drop(now, previous, current, expected):
    record = AlertRecord(previous, (now - timedelta(days=1)).isoformat())
    assert should_realert(record, current, now) is expected


def test_same_price_never_realerts_even_after_a_long_time(now):
    record = AlertRecord(749.0, (now - timedelta(days=300)).isoformat())
    assert should_realert(record, 749.0, now) is False
