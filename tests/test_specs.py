import pytest

from price_monitor.models import Category
from price_monitor.specs import (
    parse_panel_ips,
    parse_ram_gb,
    parse_refresh_hz,
    parse_screen_inches,
    parse_specs,
    parse_usb_c_pd_watts,
)


@pytest.mark.parametrize("text, expected", [
    ("Laptop 14 32 GB RAM 1 TB SSD", 32),
    ("Laptop 14, 32GB DDR5, 1TB SSD", 32),
    ("RAM: 16 GB, SSD 512 GB", 16),
    ("keskusmuisti 32 Gt", 32),
    ("16 GB RAM (max. 64 GB RAM)", 16),           # expandable capacity is not installed RAM
    ("Laptop 14 1 TB SSD", None),                  # not stated
    ("16 GB RAM or 32 GB RAM configurations", None),  # conflicting
])
def test_parse_ram_explicit(text, expected):
    assert parse_ram_gb(text) == expected


def test_parse_ram_spec_list_only_when_allowed():
    title = 'Examplebook (Ryzen 7, 32 GB, 1 TB SSD, 14", Win 11), kannettava'
    assert parse_ram_gb(title) is None
    assert parse_ram_gb(title, allow_spec_list=True) == 32


def test_parse_ram_spec_list_ambiguous_is_unknown():
    assert parse_ram_gb("Bundle (16 GB, 32 GB, 512 GB SSD)", allow_spec_list=True) is None


@pytest.mark.parametrize("text, expected", [
    ('Monitor 34" IPS', 34.0),
    ("34,5 tuuman näyttö", 34.5),
    ("34-tuumainen näyttö", 34.0),
    ("27 inch monitor", 27.0),
    ("Monitor 2-in-1 stand", None),
    ("no size given", None),
    ('Bundle 24" + 27" monitors', None),
    ('Gaming 27" QHD\n26.5 tuumaa', 26.5),  # marketing vs exact size: the smaller one
])
def test_parse_screen_inches(text, expected):
    assert parse_screen_inches(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("34 IPS 120 Hz", True),
    ("Nano IPS panel", True),
    ("34 VA 165 Hz", False),
    ("QD-OLED 175 Hz", False),
    ("Tavallinen näyttö", None),  # lowercase 'va'/'tn' inside words must not count
    ("IPS or VA variants", None),
])
def test_parse_panel_ips(text, expected):
    assert parse_panel_ips(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("120 Hz", 120),
    ("60 Hz (HDMI), 144 Hz (DP)", 144),
    ("30 kHz - 160 kHz horizontal", None),
    ("no refresh rate", None),
])
def test_parse_refresh_hz(text, expected):
    assert parse_refresh_hz(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("USB-C: DisplayPort Alt Mode, Power Delivery 90 W", 90),
    ("USB-C (65 W PD)", 65),
    ("USB Type-C 96W charging", 96),
    ("USB-C; 2 x 10 W speakers", None),        # watts belong to speakers, different clause
    ("Power Delivery 90 W via HDMI", None),    # no USB-C mentioned
    ("USB-C DisplayPort", None),
])
def test_parse_usb_c_pd_watts(text, expected):
    assert parse_usb_c_pd_watts(text) == expected


def test_parse_specs_phone_has_no_guessed_specs():
    specs = parse_specs("Examplephone 12 8/256 GB 6,1\" OLED 120 Hz", Category.PHONE)
    assert specs.to_dict() == {k: None for k in specs.to_dict()}
