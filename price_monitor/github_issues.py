"""Alerts as GitHub issues in the monitor's own repository.

GitHub then notifies the repository owner (email, web, mobile app) according to their
GitHub notification settings, so no email account or password is needed. In Actions
the token is the automatic, per-run GITHUB_TOKEN.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

import requests

from .models import Alert, SourceProblem
from .notify import NotifyError

log = logging.getLogger(__name__)

API = "https://api.github.com"
DEAL_LABEL = ("price-alert", "0e8a16", "A product matched a price rule")
PROBLEM_LABEL = ("source-problem", "d93f0b", "A source keeps failing; deals may be missed")


@dataclass
class GitHubSettings:
    repository: str  # "owner/name"
    token: str
    mention: Optional[str] = None  # GitHub username to @mention in each issue

    @classmethod
    def from_env(cls, repository: Optional[str], mention: Optional[str],
                 env: Mapping[str, str] = os.environ) -> "GitHubSettings":
        repository = repository or env.get("GITHUB_REPOSITORY")
        token = env.get("GITHUB_TOKEN")
        missing = [n for n, v in (("GITHUB_TOKEN", token), ("repository", repository)) if not v]
        if missing:
            raise NotifyError(f"GitHub issue alerts need: {', '.join(missing)} "
                              "(both are automatic in GitHub Actions)")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise NotifyError(f"repository must look like owner/name, got {repository!r}")
        mention = mention or env.get("GITHUB_REPOSITORY_OWNER") or None
        if mention and not re.fullmatch(r"[A-Za-z0-9-]+", mention):
            raise NotifyError(f"mention must be a GitHub username, got {mention!r}")
        return cls(repository, token, mention)

    def __repr__(self) -> str:  # keep the token out of logs and tracebacks
        return f"GitHubSettings(repository={self.repository!r}, mention={self.mention!r}, token='***')"


def _inline(text: str, limit: int = 200) -> str:
    """Untrusted page text as an inline code span: no @mentions, links or markdown."""
    text = " ".join(str(text).split()).replace("`", "'")[:limit]
    return f"`{text}`"


def _title(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _safe_link(url: str) -> str:
    return f"<{url}>" if re.fullmatch(r"https?://[^\s<>]+", url) else _inline(url)


def _rule_label(rule: str) -> str:
    """'laptop' -> 'Laptop', 'laptop_drop' -> 'Laptop price drop'."""
    category, _, kind = rule.partition("_")
    return category.capitalize() + (" price drop" if kind == "drop" else "")


def deal_issue(alert: Alert, mention: Optional[str]) -> dict:
    o, r = alert.offer, alert.result
    label = _rule_label(r.rule)
    lines = [
        f"@{mention} a new deal matches your **{label}** rule." if mention else f"A new deal matches your **{label}** rule.",
        "",
        f"- **Product:** {_inline(o.title)}",
        f"- **Source:** {o.source}",
        f"- **Price:** {o.price_eur:.2f} €",
    ]
    if o.shipping_eur is None:
        lines.append("- **Shipping:** unknown - the total may be higher")
    else:
        lines.append(f"- **Shipping:** {o.shipping_eur:.2f} € (total {o.total_price_eur:.2f} €)")
    if r.rule == "phone" or r.rule.endswith("_drop"):
        ref = "unknown" if r.reference_price_eur is None else f"{r.reference_price_eur:.2f} € (own observed history)"
        lines.append(f"- **{'Usual' if r.rule.endswith('_drop') else 'Reference'} price:** {ref}")
    lines += [
        f"- **Why:** {_inline('; '.join(r.reasons), 400)}",
        f"- **Link:** {_safe_link(o.url)}",
        "",
        "Nothing was purchased. Check price, seller, variant and stock on the shop page before buying. "
        "Close this issue when you are done with it.",
        "",
        f"<sub>alert key: {_inline(alert.key, 300)}</sub>",
    ]
    return {
        "title": _title(f"{label}: {o.price_eur:.2f} € - {o.title}"),
        "body": "\n".join(lines),
        "labels": [DEAL_LABEL[0]],
    }


def problem_issue(problem: SourceProblem, mention: Optional[str]) -> dict:
    who = f"@{mention} " if mention else ""
    return {
        "title": f"Source problem: {problem.source} failed {problem.consecutive_failures} runs in a row",
        "body": (
            f"{who}the monitor may be missing deals from **{problem.source}**.\n\n"
            f"Last error: {_inline(problem.last_error, 500)}\n\n"
            "See the Troubleshooting table in the README. You will not get another issue for this "
            "source until it has worked again and then failed again."
        ),
        "labels": [PROBLEM_LABEL[0]],
    }


class GitHubIssueNotifier:
    """Notifier for monitor.run_once: one issue per new deal and per failing source."""

    def __init__(self, settings: GitHubSettings, session: Optional[requests.Session] = None,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 30.0):
        self.settings = settings
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {settings.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "personal-price-monitor",
        })
        self._sleep = sleep
        self.timeout = timeout

    def __call__(self, alerts: list[Alert], problems: list[SourceProblem]) -> None:
        delivered_keys: set[str] = set()
        delivered_sources: set[str] = set()
        try:
            if alerts:
                self._ensure_label(*DEAL_LABEL)
            if problems:
                self._ensure_label(*PROBLEM_LABEL)
            for alert in alerts:
                self._create_issue(deal_issue(alert, self.settings.mention))
                delivered_keys.add(alert.key)
            for problem in problems:
                self._create_issue(problem_issue(problem, self.settings.mention))
                delivered_sources.add(problem.source)
        except NotifyError as exc:
            raise NotifyError(str(exc), delivered_keys, delivered_sources) from exc
        log.info("Opened %d GitHub issue(s) in %s", len(delivered_keys) + len(delivered_sources),
                 self.settings.repository)

    # -- API calls --------------------------------------------------------------

    def _ensure_label(self, name: str, color: str, description: str) -> None:
        resp = self._call("GET", f"/repos/{self.settings.repository}/labels/{name}", ok=(200, 404))
        if resp.status_code == 404:
            # 422 = someone created it in the meantime; fine.
            self._call("POST", f"/repos/{self.settings.repository}/labels",
                       json={"name": name, "color": color, "description": description}, ok=(201, 422))

    def _create_issue(self, issue: dict) -> None:
        resp = self._call("POST", f"/repos/{self.settings.repository}/issues", json=issue, ok=(201,))
        log.info("Opened issue %s", resp.json().get("html_url", ""))

    def _call(self, method: str, path: str, ok: tuple[int, ...], json: Optional[dict] = None,
              attempts: int = 2) -> requests.Response:
        """One retry on network errors, 5xx and rate limiting; anything else fails at once."""
        error = ""
        for attempt in range(attempts):
            try:
                resp = self.session.request(method, API + path, json=json, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                error = type(exc).__name__
            else:
                if resp.status_code in ok:
                    return resp
                error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code in (401, 404) and 404 not in ok:
                    raise NotifyError(f"GitHub {method} {path} failed ({error}); "
                                      "check the token and the workflow's 'issues: write' permission")
                if resp.status_code not in (403, 429) and resp.status_code < 500:
                    break
            if attempt + 1 < attempts:
                self._sleep(5)
        raise NotifyError(f"GitHub {method} {path} failed: {error}")
