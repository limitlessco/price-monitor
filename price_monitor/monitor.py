"""One monitoring run: discover -> parse specs -> evaluate -> record -> alert."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional, Protocol

from .alerts import alert_key, should_realert
from .config import Config, SourceConfig
from .extract import detail_text, extract_offers, next_page_url
from .fetcher import BudgetExceeded, FetchError
from .models import Alert, Offer, SourceProblem, Verdict
from .notify import NotifyError
from .rules import evaluate
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
    email_failed: bool = False


def _annotate(level: str, message: str) -> None:
    """Surface problems as GitHub Actions annotations on the run summary page."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{message}", flush=True)


def discover(source: SourceConfig, fetcher: PageFetcher) -> tuple[list[Offer], list[str]]:
    """Read every configured category page of one source (with pagination)."""
    offers: dict[str, Offer] = {}
    errors: list[str] = []
    for page in source.pages:
        url: Optional[str] = page.url
        for number in range(1, page.max_pages + 1):
            if number > 1:
                url = next_page_url(html, url) or page.page_url(number)
                if url is None:
                    break
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
                break
            for offer in found:
                offers.setdefault(offer.product_key, offer)
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
            offers, errors = discover(source, fetcher)
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

        for offer in offers:
            report.offers_seen += 1
            offer.specs = parse_specs(f"{offer.title}\n{offer.description}", offer.category)
            cached = state.cached_specs(source.name, offer.product_key, settings.spec_cache_days, now)
            if cached is not None:
                offer.specs = offer.specs.merged_with(cached)
            history = state.history(source.name, offer.product_key)  # before recording today's price
            result = evaluate(offer, config.rules, history)

            # Missing specs: look at the product page once, then cache the result.
            if (result.verdict is Verdict.UNKNOWN and result.missing_specs and source.fetch_details
                    and cached is None and detail_budget > 0 and not report.budget_exhausted):
                detail_budget -= 1
                try:
                    page_text = detail_text(fetcher.get(offer.url), source.detail_selector)
                    detail_specs = parse_specs(page_text, offer.category)
                    state.store_specs(source.name, offer.product_key, detail_specs, now)
                    offer.specs = offer.specs.merged_with(detail_specs)
                    result = evaluate(offer, config.rules, history)
                except BudgetExceeded:
                    report.budget_exhausted = True
                except FetchError as exc:
                    log.warning("[%s] detail page failed: %s", source.name, exc)

            state.record_price(source.name, offer.product_key, offer.title, offer.price_eur, now)
            report.verdicts[result.verdict.value] += 1
            log.debug("[%s] %s -> %s (%s)", source.name, offer.title, result.verdict.value, "; ".join(result.reasons))

            if result.verdict is Verdict.MATCH:
                key = alert_key(offer, result)
                previous = state.alert_record(key)
                if previous is None or should_realert(previous, offer.price_eur, now):
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
                # Alerts stay unmarked, so the next run tries again.
                report.email_failed = True
                log.error("%s", exc)
                _annotate("error", str(exc))
            else:
                for alert in report.alerts:
                    state.mark_alerted(alert.key, alert.offer.price_eur, now)
                for problem in problems:
                    state.mark_source_notified(problem.source)

    state.prune(now, settings.history_retention_days)
    log.info("Run finished: %d offers, verdicts %s, %d new alert(s)",
             report.offers_seen, report.verdicts, len(report.alerts))
    return report
