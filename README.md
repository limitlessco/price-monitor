# Price monitor

A Python price monitor that runs hourly on GitHub Actions. It reads **category pages**
of Finnish price-comparison sites and shops, checks every product against your rules,
and opens a **GitHub issue** for each new match. GitHub then notifies you by email (and in
the GitHub app), so no email account or password is stored anywhere. It never buys anything.

| Rule    | Alerts when                                                                                      |
|---------|--------------------------------------------------------------------------------------------------|
| Laptop  | installed RAM ≥ 32 GB **and** price < 800 €                                                     |
| Phone   | price < 300 € **and** the monitor's *own* price history for that exact product page is > 700 € |
| Monitor | ≥ 34", IPS, ≥ 100 Hz, USB-C Power Delivery ≥ 65 W **and** price < 300 €                         |

Price-drop rules (on top of the ones above). The "usual price" is the median of the monitor's
*own* earlier observations of the same product page (at least 3 sightings over 48 hours), so
these also need a few days of history before they can alert:

| Rule               | Alerts when                                                                        |
|--------------------|------------------------------------------------------------------------------------|
| Phone price drop   | usual price > 500 € **and** price ≥ 40 % below it                                  |
| Laptop price drop  | RAM ≥ 16 GB, usual price > 750 € **and** price ≥ 40 % below it                     |
| Monitor price drop | ≥ 32", IPS, ≥ 100 Hz, USB-C PD ≥ 65 W **and** price ≥ 30 % below the usual price   |

A product matching several rules gets one issue, not one per rule.

Each check gives **MATCH**, **NO_MATCH** or **UNKNOWN**. Only MATCH sends an alert.
Any spec that is missing stays unknown and is never guessed.

## How it works

```
category page ──► JSON-LD (schema.org) or your CSS selectors ──► offers
offers ──► spec parsing (title, then product page if needed) ──► rules ──► dedupe ──► GitHub issue
                                     │
               state.json (price history, spec cache, sent alerts) ◄── "monitor-state" branch
```

* **Discovery is category-wide.** You list category URLs per source. Every product on those
  pages is evaluated, not only products you name.
* **Extraction** tries schema.org JSON-LD first. JSON-LD is a public standard, so no
  site-specific guessing is needed. Sites without JSON-LD use CSS selectors from
  `config.toml`, each checked against the live site (see Sources below).
* **Specs** come from the product title. When a spec is still unknown and the price already
  qualifies, the monitor opens the product page once (at most `max_detail_fetches_per_run`
  per run) and caches the result for `spec_cache_days`.
* **Phone reference prices** come only from prices this monitor recorded on earlier runs.
  It uses the observation-weighted median and needs at least `min_history_observations` over
  `min_history_span_hours`. Crossed-out "was" prices, `ListPrice`/`StrikethroughPrice`
  and `AggregateOffer.highPrice` are never used. **The phone rule therefore stays UNKNOWN
  until the monitor has watched a phone for a while.**
* **Total price:** if a source states shipping, it is included. If shipping is unknown, the
  default `allow_unknown_shipping = true` alerts on the item price and the alert says
  "Shipping: unknown". Set it to `false` to treat unknown shipping as UNKNOWN instead.

## Sources (checked 2026-10-30)

All three sites are enabled, each with laptop, phone and monitor category pages. A live
dry run on 2026-10-30 read 336 products from the nine pages in about 80 seconds.

| Source | How products are read | Notes |
|--------|------------------------|-------|
| hintaseuranta.fi | schema.org `ItemList` (JSON-LD) on category pages | robots.txt disallows `/haku/` (search), `/API/` and `/ajax/`. |
| hintaopas.fi | CSS selectors on the site's own `data-test` attributes; spec table rows on product pages | Products are identified by `?p=<id>`. Pages use `?offset=44, 88, …`. robots.txt disallows `/search?` and `/c/…` URLs with `sort=`, `stores=` and several filter params. |
| kauppa.dna.fi | CSS selectors on the site's own `data-testid` attributes | Uses only the one-off price, never the monthly instalment or "30 pv alin hinta". The page's own next-page link points at an internal hostname, so a URL template is used instead. Laptop titles rarely state RAM, so RAM is looked up on product pages, a few per run. |

Sites change their markup. If a source stops yielding products, the monitor opens one
`source-problem` issue, and the selectors in `config.toml` need updating.

The site owners' terms of use are your responsibility. Price-comparison sites often restrict
automated collection.

## Respecting sites

* robots.txt is fetched and obeyed for every URL, using an RFC 9309 parser (`protego`) that
  supports `*` and `$` wildcards. If robots.txt is missing (404), everything is allowed. If it
  can't be reached, the whole site is treated as disallowed.
* There is at least `min_request_interval_seconds` between requests to the same host, or the
  robots.txt `Crawl-delay` if that is larger.
* Retries happen only on timeouts, connection errors, 429 and 5xx. There are at most
  `max_retries` retries, with exponential backoff and `Retry-After` honoured. A `Retry-After`
  longer than 60 s skips that URL for the run.
* **401/403 are never retried or worked around.** There's no proxy rotation, no headless
  browser and no CAPTCHA solving. The User-Agent identifies the bot honestly.
* `max_run_seconds` limits each run, which also keeps Actions minutes predictable.

## Run it on your own computer (Windows)

1. **Install Python** (one time only). Download Python 3.11 or newer from
   https://www.python.org/downloads/. On the first installer screen, tick
   **"Add python.exe to PATH"**.
2. **Double-click `run-local.bat`** in the project folder.
   - The first run takes about a minute to set things up. Later runs are faster.
   - It checks all nine category pages, which takes 1–2 minutes because it waits politely
     between requests.
   - Matches show up as lines starting with `DRY RUN alert`, with the price and a link.
   - Nothing is sent and nothing is saved. The alerts themselves come from the hourly
     GitHub Actions run, so a local check never duplicates them.

To change the rules (prices, RAM, screen size and so on), edit `config.toml` in Notepad
and run again.

**macOS / Linux:** run the same steps by hand:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m price_monitor --config config.toml --state state/local-state.json --dry-run
```

**Running the tests:**

```bash
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
```

The tests use only the synthetic fixtures in `tests/fixtures/` and make no network requests.

## Configuration

`config.toml` is the file the monitor reads, and it's committed (it contains no secrets).
`config.example.toml` is the same file kept as a reference.

* To narrow a category, open it in your browser, apply the site's own filters (for example
  RAM 32 GB), and paste the resulting URL, as long as robots.txt allows that URL. The
  monitor checks robots.txt and skips disallowed URLs, logging a warning.
* Pagination: `<link rel="next">` on the same site is followed automatically. Otherwise
  `page_url_template` is used with `{page}` (1, 2, …) or `{offset}` (0, n, 2n, …, which
  needs `items_per_page`). Raise `max_pages` to read more pages, but each page adds
  about 5 seconds per run.
* Every run reads each list to its end; `max_pages` is only a safety cap. A full run
  takes about 7-8 minutes, which is fine because public repos get unlimited Actions
  minutes. For a **private** repo, add `pages_per_run = N` to a page: each run then
  reads page 1 plus N-1 further pages in turn and continues there next run (the
  position is kept in `state.json`), so a run stays under ~100 s and the free
  2,000 minutes a month.
* hintaopas's order shifts slightly between requests, so a page sometimes repeats the
  previous one and a few products are missed in a given run. They are picked up in the
  next runs.
* With hintaopas filters, put `offset` **last** in `page_url_template`: its robots.txt
  blocks three parameters unless the last one is `offset`.

### Fixing CSS selectors after a site redesign

Open the category page, right-click a product card and choose **Inspect**. Prefer
attributes meant for tests (`data-test`, `data-testid`) over generated class names.

* `item`: one element per product card.
* `title`: inside the card.
* `price`: inside the card, the **current** one-off price. Anything inside `<del>`, `<s>`
  or `<strike>` is always ignored.
* `link` (optional): the product link. By default this is the card itself if it's a link,
  otherwise its first link.
* `description`, `shipping` (optional).
* `detail_selector` (per source): spec rows on the product page.

Check with View Source, not Inspect. If the prices only appear after JavaScript runs,
selectors can't read them.

## Run it on GitHub Actions (hourly, from the cloud)

### 1. How alerts reach you

Each new deal becomes an issue in this repository, labelled `price-alert`, with the price,
why it matched and the shop link. The issue @mentions `mention` from `[notifications]` in
`config.toml`, so GitHub notifies you:

* **Email:** GitHub sends it to your account's email address. Check
  GitHub → Settings → Notifications: *Participating, @mentions and custom* should have
  **Email** ticked (it is by default).
* **Phone:** the GitHub mobile app shows it as a push notification, if you use the app.

Close an issue when you are done with it. All open deals are at
**Issues → label: price-alert**.

The workflow uses the automatic `GITHUB_TOKEN` that GitHub creates for each run and
revokes afterwards. Its `issues: write` permission is granted in the workflow file. There
are no secrets to add.

Prefer plain email? Set `channel = "email"` in `[notifications]` and add the secrets
`SMTP_HOST`, `SMTP_PORT` (default 587), `SMTP_USERNAME`, `SMTP_PASSWORD`, `ALERT_EMAIL_TO`
and optionally `ALERT_EMAIL_FROM` under **Settings → Secrets and variables → Actions**.
A separate Gmail account with an app password works.

### 2. Run it

Go to **Actions → Price monitor → Run workflow**. Tick *dry run* for the first try. After
that it runs hourly.

## GitHub Free usage

* **Public repository:** standard GitHub-hosted runners are free with no minute limit.
  Your config and the state branch (price history) are public. Secrets stay secret.
* **Private repository:** GitHub Free includes 2,000 minutes per month, and each job is
  rounded **up** to a whole minute. Hourly is about 744 runs per month. With all three
  sites a run takes about 2 minutes (setup plus about 80 seconds of polite fetching), so
  about 1,500 minutes per month, which fits. `timeout-minutes: 4` caps a hung job. If you
  add more pages, check usage under Settings → Billing, or change the cron to every
  2 hours (`17 */2 * * *`).

Check the current limits on GitHub's billing documentation, because they can change. In
public repositories, scheduled workflows are disabled automatically after 60 days without
repository activity. Re-enable the workflow from the Actions tab if that happens.

## State and duplicate alerts

* `state.json` lives on the `monitor-state` branch. That branch is a single commit
  force-pushed by each run, so the repository doesn't grow.
* An alert's identity is `rule | source | product URL`. An alert is recorded only after its
  issue is **successfully** created. If creating one fails, the others that did get through
  are still recorded, and the next run retries only the missing ones.
* An already-alerted product gets a new issue only if its price has dropped by at least
  **5 % and 10 €** since the last alert. The same or a higher price never re-alerts. The
  thresholds are the `REALERT_*` constants in `price_monitor/alerts.py`.
* If a source fails `source_failure_alert_threshold` runs in a row (HTTP errors, or a page
  with zero extracted products after a layout change), you get one `source-problem` issue.
  Another one only comes after the source has recovered and then broken again.
* To reset everything, delete the `monitor-state` branch.

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `GitHub issue alerts need: GITHUB_TOKEN …` | Running outside Actions without `--dry-run`. Use `run-local.bat` (always a dry run) or the Actions workflow. |
| `… check the token and the workflow's 'issues: write' permission` | The `permissions:` block in `.github/workflows/price-monitor.yml` must include `issues: write`. |
| `Setup failed: missing environment variables: SMTP_…` | Only with `channel = "email"`: a secret is not set or is misspelled. |
| `config file not found: config.toml` | Copy `config.example.toml` to `config.toml` and commit it. |
| `no offers extracted` warning | The page has no schema.org Product data, or your selectors no longer match. Compare View Source with your selectors. |
| `robots.txt disallows …` | That URL is off-limits. Pick another category URL. Don't work around it. |
| `HTTP 403` / `AccessDenied` | The site is refusing automated access. Disable that source. |
| `… HTTP(S)_PROXY setting applies to this URL …` | The monitor only connects directly, so it can check every connection goes to a public address. Run it on a network without a mandatory proxy, or on GitHub Actions. |
| `giving up on … after 3 attempts` | The site is temporarily down or rate-limiting. It's retried next hour. |
| Laptops are always UNKNOWN | RAM isn't in the title or on the product page as `32 GB RAM` / `RAM: 32 GB`. Run with `-v` to see each verdict's reasons. |
| Phones never alert | Expected at first. The reference price needs this monitor's own history (see `[rules.phone]`). |
| `… is corrupted` | `state.json` is broken. Delete the `monitor-state` branch to start fresh (alerts may repeat once). |
| Issue opened but no email | GitHub → Settings → Notifications: tick Email for *Participating, @mentions and custom*, check that `mention` in `config.toml` is your username, and check your spam folder. |
| Scheduled runs late or missing | GitHub delays or drops scheduled runs under load. Use *Run workflow* to test. |

## Project layout

```
price_monitor/
  __main__.py   CLI entry point (exit 0 ok, 1 setup error, 2 alert delivery failed)
  config.py     config.toml loading and validation
  fetcher.py    robots.txt, rate limit, timeouts, retries, time budget
  extract.py    JSON-LD / CSS-selector extraction, price parsing
  specs.py      conservative spec parsing (unknown = None)
  rules.py      laptop / phone / monitor rules, reference price
  state.py      persistent JSON state
  alerts.py     duplicate-alert policy
  github_issues.py  alerts as GitHub issues (default channel)
  notify.py     SMTP email (optional channel)
  monitor.py    one run, wiring everything together
tests/          pytest suite + synthetic fixtures (no live requests)
```
