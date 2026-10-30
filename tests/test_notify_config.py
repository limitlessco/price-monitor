from pathlib import Path

import pytest

from price_monitor.config import ConfigError, load_config
from price_monitor.models import Alert, Category, Offer, RuleResult, SourceProblem, Verdict
from price_monitor.notify import NotifyError, SmtpSettings, build_message

ROOT = Path(__file__).resolve().parent.parent

ENV = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_USERNAME": "bot@example.com",
    "SMTP_PASSWORD": "s3cret-value",
    "ALERT_EMAIL_TO": "me@example.com, other@example.com",
}


def test_smtp_settings_from_env():
    s = SmtpSettings.from_env(ENV)
    assert (s.port, s.sender, s.recipients) == (587, "bot@example.com", ["me@example.com", "other@example.com"])
    assert "s3cret-value" not in repr(s)


def test_smtp_settings_report_missing_names_not_values():
    with pytest.raises(NotifyError) as exc:
        SmtpSettings.from_env({**ENV, "SMTP_PASSWORD": ""})
    assert "SMTP_PASSWORD" in str(exc.value)


def test_email_body():
    offer = Offer("hintaseuranta", Category.PHONE, "k", "Examplephone 12", "https://shop.example/p", 279.0)
    result = RuleResult("phone", Verdict.MATCH, ["price ok", "reference ok"], reference_price_eur=749.0)
    msg = build_message([Alert(offer, result, "phone|hintaseuranta|k")],
                        [SourceProblem("dna", 6, "HTTP 503")], "bot@example.com", ["me@example.com"])
    body = msg.get_content()
    assert msg["Subject"] == "Price monitor: 1 new deal(s)"
    assert "Shipping: unknown" in body
    assert "749.00 € (own observed history)" in body
    assert "Nothing was purchased" in body
    assert "dna: failed 6 runs in a row" in body


def test_example_config_loads():
    config = load_config(ROOT / "config.example.toml")
    names = {s.name: s for s in config.sources}
    assert names["hintaseuranta"].selectors is None  # JSON-LD site
    assert names["dna"].selectors.link is None       # the card itself is the link
    assert config.rules.laptop.min_ram_gb == 32
    assert config.rules.monitor.min_usb_c_pd_watts == 65
    assert config.notifications.channel == "github_issues"
    assert config.notifications.mention is None  # empty string in the file = default


def test_unknown_notification_channel_is_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[monitor]\nuser_agent = "x"\n[notifications]\nchannel = "sms"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="channel"):
        load_config(path)


def test_config_errors(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[monitor]\nuser_agent = ""\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="user_agent"):
        load_config(path)
    path.write_text('[monitor]\nuser_agent = "x"\n[[sources]]\nname = "s"\n[[sources.pages]]\n'
                    'category = "tablet"\nurl = "https://x"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="category"):
        load_config(path)
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.toml")
