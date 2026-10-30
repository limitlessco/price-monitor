"""Load config.toml. Credentials never live here - see notify.SmtpSettings.from_env."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .extract import Selectors
from .models import Category


class ConfigError(Exception):
    pass


@dataclass
class MonitorSettings:
    user_agent: str
    request_timeout_seconds: float = 20.0
    max_retries: int = 2
    min_request_interval_seconds: float = 5.0
    max_run_seconds: float = 120.0
    max_detail_fetches_per_run: int = 10
    spec_cache_days: int = 30
    history_retention_days: int = 365
    source_failure_alert_threshold: int = 6


@dataclass
class LaptopRule:
    max_price_eur: float = 800
    min_ram_gb: int = 32


@dataclass
class PhoneRule:
    max_price_eur: float = 300
    min_reference_price_eur: float = 700
    min_history_observations: int = 3
    min_history_span_hours: float = 48


@dataclass
class MonitorRule:
    max_price_eur: float = 300
    min_screen_inches: float = 34
    require_ips: bool = True
    min_refresh_hz: int = 100
    min_usb_c_pd_watts: int = 65


@dataclass
class RulesConfig:
    laptop: LaptopRule = field(default_factory=LaptopRule)
    phone: PhoneRule = field(default_factory=PhoneRule)
    monitor: MonitorRule = field(default_factory=MonitorRule)
    allow_unknown_shipping: bool = True


@dataclass
class CategoryPage:
    category: Category
    url: str
    max_pages: int = 1
    # Copied from the real site, with {page} (1, 2, ...) or {offset} (0, n, 2n, ...).
    page_url_template: Optional[str] = None
    items_per_page: Optional[int] = None  # needed for {offset}

    def page_url(self, number: int) -> Optional[str]:
        if not self.page_url_template:
            return None
        return self.page_url_template.format(page=number, offset=(number - 1) * (self.items_per_page or 0))


@dataclass
class SourceConfig:
    name: str
    enabled: bool
    pages: list[CategoryPage]
    selectors: Optional[Selectors] = None
    fetch_details: bool = True
    detail_selector: Optional[str] = None  # spec rows on product pages
    key_query_params: list[str] = field(default_factory=list)  # query params that identify a product


@dataclass
class Config:
    monitor: MonitorSettings
    rules: RulesConfig
    sources: list[SourceConfig]


def _build(cls, data: dict, where: str):
    try:
        return cls(**data)
    except TypeError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def _source(raw: dict[str, Any], index: int) -> SourceConfig:
    where = f"sources[{index}]"
    if "name" not in raw:
        raise ConfigError(f"{where}: missing 'name'")
    pages = []
    for j, page in enumerate(raw.get("pages", [])):
        page = dict(page)
        try:
            page["category"] = Category(page.get("category"))
        except ValueError:
            raise ConfigError(f"{where}.pages[{j}]: category must be one of {[c.value for c in Category]}")
        template = page.get("page_url_template") or None
        page["page_url_template"] = template
        if template is not None:
            if "{page}" not in template and "{offset}" not in template:
                raise ConfigError(f"{where}.pages[{j}]: page_url_template must contain {{page}} or {{offset}}")
            if "{offset}" in template and not page.get("items_per_page"):
                raise ConfigError(f"{where}.pages[{j}]: {{offset}} needs items_per_page")
        pages.append(_build(CategoryPage, page, f"{where}.pages[{j}]"))
    selectors = raw.get("selectors")
    if selectors:
        selectors = {k: (v or None) for k, v in selectors.items()}
        empty = [k for k in ("item", "title", "price") if not selectors.get(k)]
        selectors = None if empty else _build(Selectors, selectors, f"{where}.selectors")
    return SourceConfig(
        name=raw["name"],
        enabled=bool(raw.get("enabled", False)),
        pages=pages,
        selectors=selectors or None,
        fetch_details=bool(raw.get("fetch_details", True)),
        detail_selector=raw.get("detail_selector") or None,
        key_query_params=list(raw.get("key_query_params", [])),
    )


def load_config(path: Path) -> Config:
    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path} (copy config.example.toml to config.toml)")
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc

    monitor_raw = raw.get("monitor", {})
    if not monitor_raw.get("user_agent"):
        raise ConfigError("[monitor] user_agent is required (identify your bot honestly)")
    rules_raw = dict(raw.get("rules", {}))
    rules = _build(RulesConfig, {
        "laptop": _build(LaptopRule, rules_raw.pop("laptop", {}), "rules.laptop"),
        "phone": _build(PhoneRule, rules_raw.pop("phone", {}), "rules.phone"),
        "monitor": _build(MonitorRule, rules_raw.pop("monitor", {}), "rules.monitor"),
        **rules_raw,
    }, "rules")
    return Config(
        monitor=_build(MonitorSettings, monitor_raw, "monitor"),
        rules=rules,
        sources=[_source(s, i) for i, s in enumerate(raw.get("sources", []))],
    )
