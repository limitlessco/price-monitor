from datetime import datetime, timezone
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def now():
    return datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
