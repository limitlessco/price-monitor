"""Core data types shared by extraction, rules, state and notification."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Optional


class Category(str, Enum):
    LAPTOP = "laptop"
    PHONE = "phone"
    MONITOR = "monitor"


@dataclass
class Specs:
    """Product specifications. ``None`` always means "unknown" - never a guess."""

    ram_gb: Optional[int] = None
    screen_inches: Optional[float] = None
    panel_ips: Optional[bool] = None
    refresh_hz: Optional[int] = None
    usb_c_pd_watts: Optional[int] = None

    def merged_with(self, other: "Specs") -> "Specs":
        """Return a copy where unknown fields are filled from ``other``."""
        values = {}
        for f in fields(self):
            mine = getattr(self, f.name)
            values[f.name] = mine if mine is not None else getattr(other, f.name)
        return Specs(**values)

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: dict) -> "Specs":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Offer:
    """One product as seen on one source during one run."""

    source: str
    category: Category
    product_key: str  # stable identity within a source (canonical product URL)
    title: str
    url: str
    price_eur: float  # the actual current selling price - never a crossed-out price
    shipping_eur: Optional[float] = None  # None = shipping cost unknown
    description: str = ""  # extra text (e.g. spec tables) used only for spec parsing
    specs: Specs = field(default_factory=Specs)

    @property
    def total_price_eur(self) -> Optional[float]:
        if self.shipping_eur is None:
            return None
        return round(self.price_eur + self.shipping_eur, 2)


class Verdict(str, Enum):
    MATCH = "match"
    NO_MATCH = "no_match"
    UNKNOWN = "unknown"


@dataclass
class RuleResult:
    rule: str
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)
    missing_specs: list[str] = field(default_factory=list)  # spec fields that were unknown
    reference_price_eur: Optional[float] = None  # phones only; None = unknown


@dataclass
class Alert:
    offer: Offer
    result: RuleResult
    key: str


@dataclass
class SourceProblem:
    source: str
    consecutive_failures: int
    last_error: str
