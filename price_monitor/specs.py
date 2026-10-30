"""Conservative spec parsing from product titles and descriptions.

Every parser returns ``None`` when the text does not state the value
unambiguously. Conflicting values also give ``None``: an unknown spec makes
a rule verdict UNKNOWN, which is safer than a wrong MATCH or NO_MATCH.
"""

from __future__ import annotations

import re
from typing import Optional

from .models import Category, Specs

_GB = r"(?:GB|Gt)"
# Words that turn a number into a maximum/expandable capacity, not installed RAM.
_CAPACITY_HINT = re.compile(
    r"(?:max\.?|maximum|up to|enintään|jopa|laajennettavissa|expandable|supports?)\s*$",
    re.IGNORECASE,
)


def _not_capacity(text: str, start: int) -> bool:
    return not _CAPACITY_HINT.search(text[max(0, start - 25):start])


def parse_ram_gb(text: str, allow_spec_list: bool = False) -> Optional[int]:
    """Installed RAM in GB.

    Explicit forms: "32 GB RAM", "32GB DDR5", "RAM: 32 GB", "muisti 32 Gt".
    With ``allow_spec_list``, also the comma-separated listing convention
    "(Ryzen 7, 16 GB, 512 GB SSD, 16\", Win 11)" where the only bare
    "<n> GB" element is the RAM size.
    """
    explicit: set[int] = set()
    after = re.compile(
        rf"(\d{{1,3}})\s*{_GB}\s*(?:of\s+)?(?:RAM|(?:LP)?DDR\d\w*|keskusmuisti\w*|muisti\w*)\b",
        re.IGNORECASE,
    )
    before = re.compile(
        rf"\b(?:RAM|keskusmuisti\w*|muisti\w*)\s*[:=]?\s*(\d{{1,3}})\s*{_GB}\b",
        re.IGNORECASE,
    )
    for pattern in (after, before):
        for m in pattern.finditer(text):
            if _not_capacity(text, m.start()):
                explicit.add(int(m.group(1)))
    if len(explicit) == 1:
        return explicit.pop()
    if explicit:
        return None  # conflicting statements

    if allow_spec_list:
        bare: set[int] = set()
        for group in re.findall(r"\(([^()]*)\)", text):
            for part in group.split(","):
                m = re.fullmatch(rf"\s*(\d{{1,3}})\s*{_GB}\s*", part, re.IGNORECASE)
                if m:
                    bare.add(int(m.group(1)))
        if len(bare) == 1:
            return bare.pop()
    return None


def parse_screen_inches(text: str) -> Optional[float]:
    pattern = re.compile(
        r"(?<![\d.,])(\d{2}(?:[.,]\d{1,2})?)\s*(?:\"|”|″|''|-?tuum\w*|inch(?:es)?\b|-?in\b)",
        re.IGNORECASE,
    )
    values = {float(m.group(1).replace(",", ".")) for m in pattern.finditer(text)}
    if not values:
        return None
    # Marketing size vs. exact size of one panel ("27\"" and "26,5 tuumaa"): use the
    # smaller, safe for an "at least" rule. A wider spread means several products.
    return min(values) if max(values) - min(values) <= 1.0 else None


def parse_panel_ips(text: str) -> Optional[bool]:
    """True for an IPS-family panel, False for a named non-IPS panel, else None."""
    has_ips = re.search(r"\bIPS\b", text, re.IGNORECASE) is not None
    # Case-sensitive on purpose: lowercase "va"/"tn" occur inside ordinary words.
    has_other = re.search(r"\b(?:VA|TN|OLED|QD-OLED|WOLED|PLS|Mini-LED VA)\b", text) is not None
    if has_ips and not has_other:
        return True
    if has_other and not has_ips:
        return False
    return None


def parse_refresh_hz(text: str) -> Optional[int]:
    """Highest stated refresh rate; ignores kHz/MHz and implausible values."""
    values = [
        int(m.group(1))
        for m in re.finditer(r"(?<![\d.,])(\d{2,3})\s*Hz\b", text)
        if 24 <= int(m.group(1)) <= 1000
    ]
    return max(values) if values else None


_USB_C = re.compile(r"USB[\s-]?C|USB\s+Type[\s-]?C|Thunderbolt", re.IGNORECASE)
_POWER = r"(?:Power\s*Delivery|\bPD\b|lataus\w*|virransyöt\w*|charging)"


def parse_usb_c_pd_watts(text: str) -> Optional[int]:
    """USB-C Power Delivery wattage, only when a clause names USB-C *and* PD/charging."""
    watts: list[int] = []
    for clause in re.split(r"[;\n|•]|\.\s", text):
        if not _USB_C.search(clause):
            continue
        for pattern in (
            rf"{_POWER}\D{{0,25}}?(?<!\d)(\d{{2,3}})\s*W\b",
            rf"(?<!\d)(\d{{2,3}})\s*W\b\s*(?:\w+\s+){{0,2}}?{_POWER}",
        ):
            watts.extend(int(m.group(1)) for m in re.finditer(pattern, clause, re.IGNORECASE))
    return max(watts) if watts else None


def parse_specs(text: str, category: Category) -> Specs:
    if category is Category.LAPTOP:
        return Specs(ram_gb=parse_ram_gb(text, allow_spec_list=True))
    if category is Category.MONITOR:
        return Specs(
            screen_inches=parse_screen_inches(text),
            panel_ips=parse_panel_ips(text),
            refresh_hz=parse_refresh_hz(text),
            usb_c_pd_watts=parse_usb_c_pd_watts(text),
        )
    return Specs()  # phone rules use price history, not specs
