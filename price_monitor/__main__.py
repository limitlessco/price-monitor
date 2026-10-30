"""Command line entry point: ``python -m price_monitor --config config.toml --state state/state.json``."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import ConfigError, load_config
from .fetcher import Fetcher
from .github_issues import GitHubIssueNotifier, GitHubSettings
from .monitor import Notifier, run_once
from .notify import NotifyError, SmtpSettings, build_message, send_email
from .state import State, StateError

EXIT_OK, EXIT_SETUP_ERROR, EXIT_NOTIFY_FAILED = 0, 1, 2


def make_notifier(config) -> Notifier:
    """The configured alert channel. Raises NotifyError if its settings are missing."""
    n = config.notifications
    if n.channel == "github_issues":
        return GitHubIssueNotifier(GitHubSettings.from_env(n.github_repository, n.mention))
    smtp = SmtpSettings.from_env()

    def email(alerts, problems):
        send_email(build_message(alerts, problems, smtp.sender, smtp.recipients), smtp)

    return email


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="price_monitor", description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--state", type=Path, default=Path("state/state.json"))
    parser.add_argument("--dry-run", action="store_true", help="log matches; send no alerts, save no state")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every product verdict")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    log = logging.getLogger("price_monitor")

    try:
        config = load_config(args.config)
        state = State.load(args.state)
        notifier = None if args.dry_run else make_notifier(config)
    except (ConfigError, StateError, NotifyError) as exc:
        log.error("Setup failed: %s", exc)
        return EXIT_SETUP_ERROR

    m = config.monitor
    fetcher = Fetcher(
        user_agent=m.user_agent,
        timeout_seconds=m.request_timeout_seconds,
        max_retries=m.max_retries,
        min_interval_seconds=m.min_request_interval_seconds,
        budget_seconds=m.max_run_seconds,
    )
    report = run_once(config, state, fetcher, datetime.now(timezone.utc), notifier)

    if args.dry_run:
        log.info("Dry run: state not saved")
    else:
        state.save(args.state)
    return EXIT_NOTIFY_FAILED if report.notify_failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
