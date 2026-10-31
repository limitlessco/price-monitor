"""One monitoring run: discover -> parse specs -> evaluate -> record -> alert."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional, Protocol
from urllib.parse import urlsplit

from .alerts import alert_key, should_realert
from .config import Config, SourceConfig
from .extract import detail_text, extract_offers, next_page_url
from .fetcher import BudgetExceeded, FetchError
from .models import Alert, Offer, SourceProblem, Verdict
from .notify import NotifyError
from .rules import evaluate_all
from .specs import parse_specs
from .state import State

log = logging.getLogger(__name__)

Notifier = Callable[[list[Alert], list[SourceProblem]], None]


class PageFetcher(Protocol):
    def get(self, url: str) -> str: ...


@dataclass
class RunReport:
    offers_seen: int = 0
    verdicts: dict[str, int] = field(default_factory=lambda: {v.value: 0 for v in Verdict})
    alerts: list[Alert] = field(default_factory=list)
    source_errors: dict[str, str] = field(default_factory=dict)
    budget_exhausted: bool = False
    notify_failed: bool = False


def _annotate(level: str, message: str) -> None:
    """Surface problems as GitHub Actions annotations on the run summary page."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{message}", flush=True)


def _page_numbers(page: CategoryPage, cursors: dict[str, int]) -> list[int]:
    """Page 1, then the next pages in rotation (2..max_pages) for long lists."""
    if page.pages_per_run is None or page.pages_per_run >= page.max_pages:
        return list(range(1, page.max_pages + 1))
    start = cursors.get(page.url, 2)
    if not 2 <= start <= page.max_pages:
        start = 2
    rotating = range(2, page.max_pages + 1)
    i = start - 2
    return [1] + [rotating[(i + k) % len(rotating)] for k in range(page.pages_per_run - 1)]


def discover(source: SourceConfig, fetcher: PageFetcher,
             cursors: Optional[dict[str, int]] = None) -> tuple[list[Offer], list[str]]:
    """Read the configured category pages of one source (with pagination).

    ``cursors`` (from the state) remembers where rotating lists continue next run.
    """
    cursors = {} if cursors is None else cursors
    offers: dict[str, Offer] = {}
    errors: list[str] = []
    for page in source.pages:
        url: Optional[str] = page.url
        rotating = page.pages_per_run is not None and page.pages_per_run < page.max_pages
        numbers = _page_numbers(page, cursors)
        repeats = 0
        for number in numbers:
            if number > 1:
                url = page.page_url(number) or next_page_url(html, url)  # the configured template wins
                if url is None:
                    break
            if rotating and number > 1:
                cursors[page.url] = number + 1  # continue after this page next run
            try:
                html = fetcher.get(url)
            except BudgetExceeded:
                raise
            except FetchError as exc:
                errors.append(str(exc))
                log.warning("[%s] %s", source.name, exc)
                break
            found = extract_offers(html, url, source.name, page.category, source.selectors, source.key_query_params)
            log.info("[%s] %s: %d offers", source.name, url, len(found))
            if not found:
                if rotating and number > 1:
                    cursors[page.url] = 2  # past the end of the list: start over
                break
            new = [o for o in found if o.product_key not in offers]
            for offer in new:
                offers[offer.product_key] = offer
            # Past the end some sites repeat their last page instead of an empty one.
            # One repeated page is not enough: hintaopas sometimes serves a page twice
            # mid-list (its order shifts between requests), so stop after two in a row.
            repeats = repeats + 1 if not new and number > 1 else 0
            if repeats >= 2 and not rotating:
                break
    return list(offers.values()), errors


def run_once(
    config: Config,
    state: State,
    fetcher: PageFetcher,
    now: datetime,
    notifier: Optional[Notifier],
) -> RunReport:
    """Run all enabled sources. ``notifier=None`` is a dry run: nothing is sent or marked."""
    report = RunReport()
    settings = config.monitor
    detail_budget = settings.max_detail_fetches_per_run

    for source in config.sources:
        if not source.enabled:
            continue
        try:
            offers, errors = discover(source, fetcher, state.page_cursors)
        except BudgetExceeded:
            report.budget_exhausted = True
            log.warning("Run time budget used up; remaining sources skipped this run")
            break
        if not offers and not errors:
            errors.append("no offers extracted: no schema.org Product data on the page and no working selectors")
        if errors:
            report.source_errors[source.name] = errors[-1]
            _annotate("warning", f"{source.name}: {errors[-1]}")
        state.record_source_result(source.name, ok=not errors, error=errors[-1] if errors else "")
        source_hosts = {urlsplit(page.url).netloc for page in source.pages}

        for offer in offers:
            report.offers_seen += 1
            offer.specs = parse_specs(f"{offer.title}\n{offer.description}", offer.category)
            cached = state.cached_specs(source.name, offer.product_key, settings.spec_cache_days, now)
            if cached is not None:
                offer.specs = offer.specs.merged_with(cached)
            history = state.history(source.name, offer.product_key)  # before recording today's price
            results = evaluate_all(offer, config.rules, history)

            # Missing specs: look at the product page once, then cache the result.
            # Only on the source's own host - product URLs come from the fetched page.
            needs_specs = not any(r.verdict is Verdict.MATCH for r in results) and any(
                r.verdict is Verdict.UNKNOWN and r.missing_specs for r in results)
            if (needs_specs and source.fetch_details
                    and urlsplit(offer.url).netloc in source_hosts
                    and cached is None and detail_budget > 0 and not report.budget_exhausted):
                detail_budget -= 1
                try:
                    page_text = detail_text(fetcher.get(offer.url), source.detail_selector)
                    detail_specs = parse_specs(page_text, offer.category)
                    state.store_specs(source.name, offer.product_key, detail_specs, now)
                    offer.specs = offer.specs.merged_with(detail_specs)
                    results = evaluate_all(offer, config.rules, history)
                except BudgetExceeded:
                    report.budget_exhausted = True
                except FetchError as exc:
                    log.warning("[%s] detail page failed: %s", source.name, exc)

            state.record_price(source.name, offer.product_key, offer.title, offer.price_eur, now)
            report.verdicts[results[0].verdict.value] += 1  # the fixed-limit rule
            for result in results:
                log.debug("[%s] %s -> %s %s (%s)", source.name, offer.title, result.rule,
                          result.verdict.value, "; ".join(result.reasons))

            # One alert per deal: the first matching rule (fixed limit before price
            # drop) reports it, and a deal already alerted under any matching rule
            # only re-alerts after a further real drop.
            matches = [(r, alert_key(offer, r)) for r in results if r.verdict is Verdict.MATCH]
            if matches:
                previous = [rec for _, key in matches if (rec := state.alert_record(key)) is not None]
                if all(should_realert(rec, offer.price_eur, now) for rec in previous):
                    result, key = matches[0]
                    report.alerts.append(Alert(offer, result, key))

    problems = [
        SourceProblem(name, entry["failures"], entry["last_error"])
        for name, entry in state.sources.items()
        if state.source_needs_notice(name, settings.source_failure_alert_threshold)
    ]

    if report.alerts or problems:
        if notifier is None:
            for alert in report.alerts:
                log.info("DRY RUN alert: %s %.2f € %s", alert.offer.title, alert.offer.price_eur, alert.offer.url)
        else:
            try:
                notifier(report.alerts, problems)
            except NotifyError as exc:
                # Undelivered alerts stay unmarked, so the next run tries again.
                report.notify_failed = True
                log.error("%s", exc)
                _annotate("error", str(exc))
                delivered_keys, delivered_sources = exc.delivered_keys, exc.delivered_sources
            else:
                delivered_keys = {alert.key for alert in report.alerts}
                delivered_sources = {problem.source for problem in problems}
            for alert in report.alerts:
                if alert.key in delivered_keys:
                    state.mark_alerted(alert.key, alert.offer.price_eur, now)
            for source_name in delivered_sources:
                state.mark_source_notified(source_name)

    state.prune(now, settings.history_retention_days)
    log.info("Run finished: %d offers, verdicts %s, %d new alert(s)",
             report.offers_seen, report.verdicts, len(report.alerts))
    return report
