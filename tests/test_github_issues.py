"""GitHub issue alerts against a fake GitHub API - no network access."""

import pytest

from price_monitor.config import CategoryPage, Config, MonitorSettings, NotificationsConfig, RulesConfig, SourceConfig
from price_monitor.github_issues import GitHubIssueNotifier, GitHubSettings, deal_issue
from price_monitor.models import Alert, Category, Offer, RuleResult, SourceProblem, Verdict
from price_monitor.monitor import run_once
from price_monitor.notify import NotifyError
from price_monitor.state import State
from tests.conftest import fixture_text
from tests.test_monitor import FakeFetcher

REPO = "limitlessco/price-monitor"


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class FakeGitHub:
    """Records calls; labels exist only after they are created; issue creation can fail after N."""

    def __init__(self, existing_labels=(), fail_issue_after=None, status=500):
        self.headers = {}
        self.calls = []
        self.labels = set(existing_labels)
        self.issues = []
        self.fail_issue_after = fail_issue_after
        self.status = status

    def request(self, method, url, json=None, timeout=None):
        assert timeout, "every API call needs a timeout"
        self.calls.append((method, url.replace("https://api.github.com", "")))
        path = url.replace(f"https://api.github.com/repos/{REPO}", "")
        if method == "GET" and path.startswith("/labels/"):
            return FakeResponse(200 if path.split("/")[-1] in self.labels else 404)
        if method == "POST" and path == "/labels":
            self.labels.add(json["name"])
            return FakeResponse(201)
        if method == "POST" and path == "/issues":
            if self.fail_issue_after is not None and len(self.issues) >= self.fail_issue_after:
                return FakeResponse(self.status)
            self.issues.append(json)
            return FakeResponse(201, {"html_url": f"https://github.com/{REPO}/issues/{len(self.issues)}"})
        raise AssertionError(f"unexpected call {method} {url}")


def settings(mention="limitlessco"):
    return GitHubSettings(REPO, "ghs_secret-token-value", mention)


def make_alert(title="Examplebook Pro 14 32 GB", price=749.0, key="laptop|shop|https://shop.example/p/1"):
    offer = Offer("hintaopas", Category.LAPTOP, key.split("|")[-1], title, "https://shop.example/p/1", price)
    return Alert(offer, RuleResult("laptop", Verdict.MATCH, ["price ok", "RAM 32 GB >= 32 GB"]), key)


def test_settings_from_actions_environment():
    env = {"GITHUB_TOKEN": "ghs_x", "GITHUB_REPOSITORY": REPO, "GITHUB_REPOSITORY_OWNER": "limitlessco"}
    s = GitHubSettings.from_env(None, None, env)
    assert (s.repository, s.mention) == (REPO, "limitlessco")
    assert "ghs_x" not in repr(s)


def test_settings_missing_token_is_a_clear_error():
    with pytest.raises(NotifyError, match="GITHUB_TOKEN"):
        GitHubSettings.from_env(REPO, None, {})
    with pytest.raises(NotifyError, match="username"):
        GitHubSettings.from_env(REPO, "bad name!", {"GITHUB_TOKEN": "x"})


def test_one_issue_per_deal_and_labels_created_once():
    api = FakeGitHub()
    notifier = GitHubIssueNotifier(settings(), session=api, sleep=lambda s: None)
    notifier([make_alert(), make_alert(key="laptop|shop|https://shop.example/p/2")],
             [SourceProblem("dna", 6, "HTTP 503")])
    titles = [i["title"] for i in api.issues]
    assert titles[0] == "Laptop: 749.00 € - Examplebook Pro 14 32 GB"
    assert titles[2] == "Source problem: dna failed 6 runs in a row"
    assert [i["labels"] for i in api.issues] == [["price-alert"], ["price-alert"], ["source-problem"]]
    assert api.labels == {"price-alert", "source-problem"}
    assert api.headers["Authorization"] == "Bearer ghs_secret-token-value"

    api.calls.clear()
    notifier([make_alert()], [])
    assert ("POST", f"/repos/{REPO}/labels") not in api.calls  # label already exists now


def test_issue_body_mentions_owner_and_neutralises_page_text():
    title = "Evil @someone [click](https://evil.example) `code` <b>x</b>"
    body = deal_issue(make_alert(title=title), "limitlessco")["body"]
    assert body.startswith("@limitlessco a new deal")
    assert "`Evil @someone [click](https://evil.example) 'code' <b>x</b>`" in body  # inside a code span
    assert "- **Link:** <https://shop.example/p/1>" in body
    assert "Shipping:** unknown" in body and "Nothing was purchased" in body


def test_partial_failure_reports_what_was_delivered():
    api = FakeGitHub(fail_issue_after=1)
    notifier = GitHubIssueNotifier(settings(), session=api, sleep=lambda s: None)
    with pytest.raises(NotifyError) as exc:
        notifier([make_alert(key="a"), make_alert(key="b")], [])
    assert exc.value.delivered_keys == {"a"}
    assert "ghs_secret-token-value" not in str(exc.value)


def test_permission_error_is_not_retried():
    api = FakeGitHub(fail_issue_after=0, status=401)
    sleeps = []
    notifier = GitHubIssueNotifier(settings(), session=api, sleep=sleeps.append)
    with pytest.raises(NotifyError, match="issues: write"):
        notifier([make_alert()], [])
    assert sleeps == []


def test_server_error_is_retried_once():
    api = FakeGitHub(fail_issue_after=0, status=502)
    sleeps = []
    notifier = GitHubIssueNotifier(settings(), session=api, sleep=sleeps.append)
    with pytest.raises(NotifyError):
        notifier([make_alert()], [])
    assert sleeps == [5]
    assert len([c for c in api.calls if c == ("POST", f"/repos/{REPO}/issues")]) == 2


def test_monitor_marks_only_delivered_alerts(now):
    laptops = "https://shop.example/kannettavat"
    config = Config(MonitorSettings(user_agent="t"), RulesConfig(),
                    [SourceConfig("shop", True, [CategoryPage(Category.LAPTOP, laptops)])],
                    NotificationsConfig())

    # Make a second laptop in the fixture match too, then let the second issue fail.
    html = fixture_text("listing_jsonld.html").replace('"lowPrice": 1199.0', '"lowPrice": 799.0')
    fetcher = FakeFetcher({laptops: html})
    api = FakeGitHub(fail_issue_after=1)
    notifier = GitHubIssueNotifier(settings(), session=api, sleep=lambda s: None)
    state = State()

    report = run_once(config, state, fetcher, now, notifier)
    assert len(report.alerts) == 2 and report.notify_failed
    assert len(state.alerts) == 1  # the delivered one is not sent again next run

    api.fail_issue_after = None
    report = run_once(config, state, fetcher, now, notifier)
    assert len(report.alerts) == 1 and len(api.issues) == 2
