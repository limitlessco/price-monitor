"""Email alerts over SMTP. All credentials come from environment variables."""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Mapping

from .models import Alert, SourceProblem

log = logging.getLogger(__name__)

REQUIRED_ENV = ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "ALERT_EMAIL_TO")


class NotifyError(Exception):
    pass


@dataclass
class SmtpSettings:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipients: list[str]

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "SmtpSettings":
        missing = [name for name in REQUIRED_ENV if not env.get(name)]
        if missing:
            raise NotifyError(f"missing environment variables: {', '.join(missing)}")
        try:
            port = int(env.get("SMTP_PORT") or 587)
        except ValueError:
            raise NotifyError("SMTP_PORT must be a number")
        return cls(
            host=env["SMTP_HOST"],
            port=port,
            username=env["SMTP_USERNAME"],
            password=env["SMTP_PASSWORD"],
            sender=env.get("ALERT_EMAIL_FROM") or env["SMTP_USERNAME"],
            recipients=[r.strip() for r in env["ALERT_EMAIL_TO"].split(",") if r.strip()],
        )

    def __repr__(self) -> str:  # keep the password out of logs and tracebacks
        return f"SmtpSettings(host={self.host!r}, port={self.port}, username={self.username!r}, password='***')"


def _format_alert(alert: Alert) -> str:
    o, r = alert.offer, alert.result
    lines = [f"[{r.rule}] {o.title}", f"  Source: {o.source}", f"  Price: {o.price_eur:.2f} €"]
    if o.shipping_eur is None:
        lines.append("  Shipping: unknown - the total may be higher")
    else:
        lines.append(f"  Shipping: {o.shipping_eur:.2f} €   Total: {o.total_price_eur:.2f} €")
    if r.rule == "phone":
        ref = "unknown" if r.reference_price_eur is None else f"{r.reference_price_eur:.2f} € (own observed history)"
        lines.append(f"  Reference price: {ref}")
    lines += [f"  Why: {'; '.join(r.reasons)}", f"  Link: {o.url}"]
    return "\n".join(lines)


def build_message(alerts: list[Alert], problems: list[SourceProblem], sender: str, recipients: list[str]) -> EmailMessage:
    parts = []
    if alerts:
        parts.append(f"{len(alerts)} new deal(s) matched your rules:\n")
        parts += [_format_alert(a) + "\n" for a in alerts]
        parts.append("Nothing was purchased. Check the shop page: price, seller, variant and stock before buying.\n")
    if problems:
        parts.append("Source problems (the monitor may be missing deals):")
        parts += [f"  - {p.source}: failed {p.consecutive_failures} runs in a row. Last error: {p.last_error}"
                  for p in problems]
    subject = f"Price monitor: {len(alerts)} new deal(s)" if alerts else "Price monitor: source problems"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content("\n".join(parts))
    return msg


def send_email(msg: EmailMessage, settings: SmtpSettings, timeout: float = 30.0) -> None:
    context = ssl.create_default_context()
    try:
        if settings.port == 465:
            with smtplib.SMTP_SSL(settings.host, settings.port, timeout=timeout, context=context) as smtp:
                smtp.login(settings.username, settings.password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(settings.host, settings.port, timeout=timeout) as smtp:
                smtp.starttls(context=context)
                smtp.login(settings.username, settings.password)
                smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        # The exception text from smtplib never includes the password.
        raise NotifyError(f"sending email via {settings.host}:{settings.port} failed: {exc}") from exc
    log.info("Sent alert email to %d recipient(s)", len(settings.recipients))
