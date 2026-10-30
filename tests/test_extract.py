import pytest

from price_monitor.extract import (
    Selectors,
    canonical_url,
    detail_text,
    extract_offers,
    next_page_url,
    offers_from_json_ld,
    parse_price,
)
from price_monitor.models import Category
from tests.conftest import fixture_text

PAGE = "https://shop.example/kannettavat/list"


@pytest.mark.parametrize("value, expected", [
    (749, 749.0),
    ("599.00", 599.0),
    ("1 299,90 €", 1299.90),
    ("1 249,-", 1249.0),
    ("EUR 89,50", 89.5),
    ("ei hintaa", None),
    (None, None),
    (True, None),
])
def test_parse_price(value, expected):
    assert parse_price(value) == expected


def test_canonical_url_drops_query_and_fragment():
    assert canonical_url("https://Shop.Example/a/b/?ref=x#top") == "https://shop.example/a/b"


def test_canonical_url_keeps_identifying_params():
    url = "https://shop.example/product.php?ref=list&p=123#x"
    assert canonical_url(url, keep_params=["p"]) == "https://shop.example/product.php?p=123"


def test_json_ld_item_list():
    offers = {o.title.split(" (")[0]: o for o in
              offers_from_json_ld(fixture_text("listing_jsonld.html"), PAGE, "test", Category.LAPTOP)}
    # Out-of-stock and non-EUR offers are skipped; broken JSON-LD blocks are ignored.
    assert set(offers) == {"Examplebook Pro 14", "Examplebook Air 13", "Examplebook Max 16",
                           "Examplebook Lite 15 kannettava tietokone"}
    pro = offers["Examplebook Pro 14"]
    assert pro.price_eur == 749.0           # AggregateOffer.lowPrice, never highPrice
    assert pro.shipping_eur is None         # not stated -> unknown
    assert pro.url == "https://shop.example/kannettavat/examplebook-pro-14/1001?ref=list"
    assert pro.product_key == "https://shop.example/kannettavat/examplebook-pro-14/1001"
    assert offers["Examplebook Air 13"].price_eur == 599.0


def test_strikethrough_and_list_prices_are_never_used():
    offers = {o.product_key.rsplit("/", 1)[-1]: o for o in
              offers_from_json_ld(fixture_text("offer_types.html"), PAGE, "test", Category.PHONE)}
    assert offers["2001"].price_eur == 279.0   # StrikethroughPrice 999 ignored
    assert "2003" not in offers                # only a ListPrice -> no current price
    cheapest = offers["2002"]
    assert cheapest.price_eur == 289.90 and cheapest.shipping_eur == 5.9
    assert cheapest.total_price_eur == 295.80


def test_selectors_skip_crossed_out_prices():
    selectors = Selectors(item=".card", title="a.card-link", price=".price", link="a.card-link", shipping=".ship")
    offers = extract_offers(fixture_text("selectors_listing.html"), PAGE, "test", Category.LAPTOP, selectors)
    by_path = {o.product_key.rsplit("/", 1)[-1]: o for o in offers}
    assert set(by_path) == {"examplebook-sale-14", "examplebook-plain-15"}
    assert by_path["examplebook-sale-14"].price_eur == 779.90
    assert by_path["examplebook-sale-14"].shipping_eur == 4.90
    assert by_path["examplebook-plain-15"].price_eur == 1249.0


def test_json_ld_preferred_and_no_selectors_means_nothing_found():
    assert extract_offers(fixture_text("selectors_listing.html"), PAGE, "test", Category.LAPTOP) == []


def test_detail_text_flattens_additional_properties():
    text = detail_text(fixture_text("monitor_detail.html"))
    assert "Paneelityyppi: IPS" in text
    assert "Power Delivery 90 W" in text


def test_next_page_url():
    html = '<html><head><link rel="next" href="?page=2"></head></html>'
    assert next_page_url(html, "https://shop.example/c/laptops") == "https://shop.example/c/laptops?page=2"
    assert next_page_url("<html></html>", "https://shop.example/c/laptops") is None
