"""The real source settings from config.example.toml applied to structure-mirroring fixtures."""

from pathlib import Path

import pytest

from price_monitor.config import load_config
from price_monitor.extract import detail_text, extract_offers, next_page_url
from price_monitor.models import Category
from price_monitor.specs import parse_specs
from tests.conftest import fixture_text

CONFIG = load_config(Path(__file__).resolve().parent.parent / "config.example.toml")
SOURCES = {s.name: s for s in CONFIG.sources}


def offers_for(name, fixture, page_url, category=Category.LAPTOP):
    source = SOURCES[name]
    return extract_offers(fixture_text(fixture), page_url, name, category, source.selectors, source.key_query_params)


def test_all_three_sites_enabled_with_all_categories():
    assert set(SOURCES) == {"hintaseuranta", "hintaopas", "dna"}
    for source in SOURCES.values():
        assert source.enabled
        assert {p.category for p in source.pages} == set(Category)


def test_hintaopas_cards():
    offers = offers_for("hintaopas", "hintaopas_style_listing.html", "https://hintaopas.fi/c/kannettavat-tietokoneet")
    by_id = {o.product_key: o for o in offers}
    # The product id lives in ?p=, so it must survive URL normalisation.
    assert set(by_id) == {"https://hintaopas.fi/product.php?p=900001", "https://hintaopas.fi/product.php?p=900002"}
    laptop = by_id["https://hintaopas.fi/product.php?p=900001"]
    assert laptop.price_eur == 749.0             # not the "25%" badge
    assert laptop.title.startswith("Examplebook Pro 14")
    assert "32 GB" in laptop.description
    assert parse_specs(f"{laptop.title}\n{laptop.description}", Category.LAPTOP).ram_gb == 32
    assert by_id["https://hintaopas.fi/product.php?p=900002"].price_eur == 1249.0


def test_hintaopas_offset_pagination():
    page = SOURCES["hintaopas"].pages[0]
    assert page.page_url(1) == "https://hintaopas.fi/c/kannettavat-tietokoneet?offset=0"
    assert page.page_url(3) == "https://hintaopas.fi/c/kannettavat-tietokoneet?offset=88"


def test_hintaopas_spec_rows_only():
    text = detail_text(fixture_text("hintaopas_style_product.html"), SOURCES["hintaopas"].detail_selector)
    assert "Paneelityyppi: VA" in text
    assert "Muut katsoivat" not in text      # related products are not part of the spec table
    specs = parse_specs(text, Category.MONITOR)
    assert (specs.screen_inches, specs.panel_ips, specs.refresh_hz, specs.usb_c_pd_watts) == (34.0, False, 120, 65)


def test_dna_uses_one_off_price_not_monthly_or_30_day_lowest():
    offers = offers_for("dna", "dna_style_listing.html", "https://kauppa.dna.fi/laitteet/kannettavat")
    by_title = {o.title: o for o in offers}
    laptop = by_title['Examplebook Go 15 15,6" Ryzen 5, 32 Gt RAM']
    assert laptop.price_eur == 699.0
    assert laptop.product_key == "https://kauppa.dna.fi/tuote/p/examplebook-go-15-32-gt-ram"
    assert by_title["Examplephone X, 128 Gt"].price_eur == 1049.0
    assert by_title["Examplephone X, 128 Gt"].url == "https://kauppa.dna.fi/tuote/p/examplephone-x-128-gt"


def test_dna_internal_next_link_is_ignored():
    page_url = "https://kauppa.dna.fi/laitteet/kannettavat"
    assert next_page_url(fixture_text("dna_style_listing.html"), page_url) is None
    assert SOURCES["dna"].pages[0].page_url(2).endswith("/kannettavat-tietokoneet?page=2")


@pytest.mark.parametrize("name", ["hintaopas", "dna"])
def test_selector_sites_have_detail_selectors(name):
    assert SOURCES[name].detail_selector
