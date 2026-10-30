"""Turn fetched HTML into offers.

Two strategies, tried in order:

1. schema.org JSON-LD (``<script type="application/ld+json">``): a public
   standard, so no site-specific guessing is needed.
2. CSS selectors from the config, each one checked against the real site's
   markup (see config.example.toml for when and how they were verified).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from .models import Category, Offer

log = logging.getLogger(__name__)

# schema.org price types that describe a reference/advertised price, not what you pay now.
_IGNORED_PRICE_TYPES = ("ListPrice", "StrikethroughPrice", "MSRP", "MinimumAdvertisedPrice", "SRP")
_UNAVAILABLE = ("OutOfStock", "SoldOut", "Discontinued")
_STRIKE_TAGS = ("del", "s", "strike")


@dataclass
class Selectors:
    item: str  # one element per product card
    title: str  # inside the card
    price: str  # inside the card: the current one-off price
    link: Optional[str] = None  # inside the card; default: the card itself if it is a link, else its first link
    shipping: Optional[str] = None
    description: Optional[str] = None  # extra spec text on the card, used only for spec parsing


def canonical_url(url: str, keep_params: Iterable[str] = ()) -> str:
    """Product identity: scheme + host + path (+ only the query params that identify the product)."""
    parts = urlsplit(url)
    keep = set(keep_params)
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if k in keep))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


def parse_price(value: Any) -> Optional[float]:
    """Parse JSON numbers and Finnish-formatted strings like "1 299,90 €"."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[\s  €]|EUR", "", str(value), flags=re.IGNORECASE)
    text = text.rstrip(",-")  # "799,-"
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        price = float(text)
    except ValueError:
        return None
    return price if price >= 0 else None


# --- JSON-LD -------------------------------------------------------------------

def _json_ld_blocks(soup: BeautifulSoup) -> Iterator[Any]:
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        try:
            yield json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.debug("Skipping unparsable JSON-LD block")


def _walk(node: Any) -> Iterator[dict]:
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from _walk(value)


def _has_type(node: dict, name: str) -> bool:
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    return any(isinstance(t, str) and t.rsplit("/", 1)[-1] == name for t in types)


def _enum_name(value: Any) -> str:
    return str(value or "").rsplit("/", 1)[-1]


def _offer_price(offer: dict) -> Optional[float]:
    """Current selling price of one schema.org Offer/AggregateOffer in EUR, else None."""
    currency = offer.get("priceCurrency")
    if _has_type(offer, "AggregateOffer"):
        # lowPrice = cheapest current offer. highPrice is NOT a historical price.
        price = parse_price(offer.get("lowPrice"))
    else:
        price = parse_price(offer.get("price"))
    if price is None:
        specs = offer.get("priceSpecification")
        for spec in specs if isinstance(specs, list) else [specs]:
            if not isinstance(spec, dict) or _enum_name(spec.get("priceType")) in _IGNORED_PRICE_TYPES:
                continue
            price = parse_price(spec.get("price"))
            currency = spec.get("priceCurrency", currency)
            if price is not None:
                break
    if price is None or (currency and str(currency).upper() != "EUR"):
        return None
    return price


def _offer_shipping(offer: dict) -> Optional[float]:
    details = offer.get("shippingDetails")
    for detail in details if isinstance(details, list) else [details]:
        if isinstance(detail, dict) and isinstance(detail.get("shippingRate"), dict):
            rate = detail["shippingRate"]
            if str(rate.get("currency", "EUR")).upper() == "EUR":
                return parse_price(rate.get("value"))
    return None


def _property_text(product: dict) -> str:
    """Flatten description and additionalProperty (name: value) into plain text."""
    parts = []
    if isinstance(product.get("description"), str):
        parts.append(product["description"])
    props = product.get("additionalProperty")
    for prop in props if isinstance(props, list) else [props]:
        if isinstance(prop, dict) and prop.get("name") is not None:
            unit = prop.get("unitText") or ""
            parts.append(f"{prop['name']}: {prop.get('value', '')} {unit}".strip())
    return "\n".join(parts)


def _offers_from_product(
    product: dict, page_url: str, source: str, category: Category, keep_params: Iterable[str]
) -> list[Offer]:
    name = product.get("name")
    url = product.get("url") or page_url
    if not isinstance(name, str) or not isinstance(url, str):
        return []
    url = urljoin(page_url, url)
    raw_offers = product.get("offers")
    best: Optional[tuple[float, Optional[float]]] = None
    for offer in raw_offers if isinstance(raw_offers, list) else [raw_offers]:
        if not isinstance(offer, dict):
            continue
        if _enum_name(offer.get("availability")) in _UNAVAILABLE:
            continue
        price = _offer_price(offer)
        if price is not None and (best is None or price < best[0]):
            best = (price, _offer_shipping(offer))
    if best is None:
        return []
    return [
        Offer(
            source=source,
            category=category,
            product_key=canonical_url(url, keep_params),
            title=" ".join(name.split()),
            url=url,
            price_eur=best[0],
            shipping_eur=best[1],
            description=_property_text(product),
        )
    ]


def offers_from_json_ld(
    html: str, page_url: str, source: str, category: Category, keep_params: Iterable[str] = ()
) -> list[Offer]:
    soup = BeautifulSoup(html, "html.parser")
    offers: dict[str, Offer] = {}
    for block in _json_ld_blocks(soup):
        for node in _walk(block):
            if _has_type(node, "Product"):
                for offer in _offers_from_product(node, page_url, source, category, keep_params):
                    offers.setdefault(offer.product_key, offer)
    return list(offers.values())


# --- CSS selectors -----------------------------------------------------------

def _inside_strikethrough(tag: Tag) -> bool:
    return tag.name in _STRIKE_TAGS or tag.find_parent(_STRIKE_TAGS) is not None


def _link_of(item: Tag, selector: Optional[str]) -> Optional[Tag]:
    if selector:
        return item.select_one(selector)
    if item.name == "a" and item.get("href"):
        return item
    return item.find("a", href=True)


def _text(el: Tag) -> str:
    return " ".join(el.get_text(" ").split())


def offers_from_selectors(
    html: str, page_url: str, source: str, category: Category, selectors: Selectors,
    keep_params: Iterable[str] = (),
) -> list[Offer]:
    soup = BeautifulSoup(html, "html.parser")
    offers: dict[str, Offer] = {}
    for item in soup.select(selectors.item):
        title_el = item.select_one(selectors.title)
        link_el = _link_of(item, selectors.link)
        # A crossed-out price is never accepted, even if the selector matches it.
        price_els = [el for el in item.select(selectors.price) if not _inside_strikethrough(el)]
        if not (title_el and link_el and link_el.get("href") and price_els):
            continue
        price = parse_price(_text(price_els[0]))
        if price is None:
            continue
        shipping = None
        if selectors.shipping:
            ship_el = item.select_one(selectors.shipping)
            shipping = parse_price(_text(ship_el)) if ship_el else None
        description = ""
        if selectors.description:
            description = "\n".join(_text(el) for el in item.select(selectors.description))
        url = urljoin(page_url, link_el["href"])
        offer = Offer(
            source=source,
            category=category,
            product_key=canonical_url(url, keep_params),
            title=_text(title_el),
            url=url,
            price_eur=price,
            shipping_eur=shipping,
            description=description,
        )
        offers.setdefault(offer.product_key, offer)
    return list(offers.values())


def extract_offers(
    html: str, page_url: str, source: str, category: Category,
    selectors: Optional[Selectors] = None, keep_params: Iterable[str] = (),
) -> list[Offer]:
    offers = offers_from_json_ld(html, page_url, source, category, keep_params)
    if not offers and selectors is not None:
        offers = offers_from_selectors(html, page_url, source, category, selectors, keep_params)
    return offers


def next_page_url(html: str, page_url: str) -> Optional[str]:
    """Standard ``<link rel="next">`` / ``<a rel="next">`` pagination on the same host, if declared."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["link", "a"], href=True):
        rel = tag.get("rel") or []
        if "next" in (rel if isinstance(rel, list) else [rel]):
            url = urljoin(page_url, tag["href"])
            # Some sites leak internal hostnames here; never follow those.
            if urlsplit(url).netloc == urlsplit(page_url).netloc:
                return url
    return None


def detail_text(html: str, row_selector: Optional[str] = None) -> str:
    """Spec text from a product page.

    Always: JSON-LD Product name/description/additionalProperty. With
    ``row_selector``: one line per matched spec row, its cells joined by ": ",
    so "Paneelityyppi" + "VA" becomes "Paneelityyppi: VA".
    """
    soup = BeautifulSoup(html, "html.parser")
    parts = []
    for block in _json_ld_blocks(soup):
        for node in _walk(block):
            if _has_type(node, "Product"):
                if isinstance(node.get("name"), str):
                    parts.append(node["name"])
                parts.append(_property_text(node))
    if row_selector:
        parts.extend(": ".join(row.stripped_strings) for row in soup.select(row_selector))
    return "\n".join(p for p in parts if p)
