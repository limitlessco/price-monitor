# Price monitor

A Python price monitor that runs hourly on GitHub Actions. It reads **category pages**
of Finnish price-comparison sites and shops, checks every product against your rules,
and emails you about new matches. It never buys anything.

| Rule    | Alerts when                                                                                      |
|---------|--------------------------------------------------------------------------------------------------|
| Laptop  | installed RAM ≥ 32 GB **and** price < 800 €                                                     |
| Phone   | price < 300 € **and** the monitor's *own* price history for that exact product page is > 700 € |
| Monitor | ≥ 34", IPS, ≥ 100 Hz, USB-C Power Delivery ≥ 65 W **and** price < 300 €                         |

Each check gives **MATCH**, **NO_MATCH** or **UNKNOWN**. Only MATCH sends an email.
Any spec that is missing stays unknown and is never guessed.

## How it works

```
category page ──► JSON-LD (schema.org) or your CSS selectors ──► offers
offers ──► spec parsing (title, then product page if needed) ──► rules ──► dedupe ──► email
                                     │
               state.json (price history, spec cache, sent alerts) ◄── "monitor-state" branch
```

* **Discovery is category-wide.** You list category URLs per source. Every product on those
  pages is evaluated, not only products you name.
* **Extraction** tries schema.org JSON-LD first. JSON-LD is a public standard, so no
  site-specific guessing is needed. If a site has no JSON-LD, it can use CSS selectors that
  *you* copy from the live page. The project ships with no selectors.
* **Specs** come from the product title. When a spec is still unknown and the price already
  qualifies, the monitor opens the product page once (at most `max_detail_fetches_per_run`
  per run) and caches the result for `spec_cache_days`.
* **Phone reference prices** come only from prices this monitor recorded on earlier runs.
  It uses the observation-weighted median and needs at least `min_history_observations` over
  `min_history_span_hours`. Crossed-out "was" prices, `ListPrice`/`StrikethroughPrice`
  and `AggregateOffer.highPrice` are never used. **The phone rule therefore stays UNKNOWN
  until the monitor has watched a phone for a while.**
* **Total price:** if a source states shipping, it is included. If shipping is unknown, the
  default `allow_unknown_shipping = true` alerts on the item price and the email says
  "Shipping: unknown". Set it to `false` to treat unknown shipping as UNKNOWN instead.

## Sources (checked 2026-10-30)

All three sites are enabled, each with laptop, phone and monitor category pages. A live
dry run on 2026-10-30 read 336 products from the nine pages in about 80 seconds.

| Source | How products are read | Notes |
|--------|------------------------|-------|
| hintaseuranta.fi | schema.org `ItemList` (JSON-LD) on category pages | robots.txt disallows `/haku/` (search), `/API/` and `/ajax/`. |
| hintaopas.fi | CSS selectors on the site's own `data-test` attributes; spec table rows on product pages | Products are identified by `?p=<id>`. Pages use `?offset=44, 88, …`. robots.txt disallows `/search?` and `/c/…` URLs with `sort=`, `stores=` and several filter params. |
| kauppa.dna.fi | CSS selectors on the site's own `data-testid` attributes | Uses only the one-off price, never the monthly instalment or "30 pv alin hinta". The page's own next-page link points at an internal hostname, so a URL template is used instead. Laptop titles rarely state RAM, so RAM is looked up on product pages, a few per run. |

Sites change their markup. If a source stops yielding products, the monitor emails you once,
and the selectors in `config.toml` need updating.

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
   - This is test mode: no email is sent and nothing is saved.
3. **Optional: get matches by email from your computer.** Open *Command Prompt* in the
   project folder (in File Explorer, type `cmd` in the address bar and press Enter), then run:

   ```bat
   set SMTP_HOST=smtp.gmail.com
   set SMTP_USERNAME=you@gmail.com
   set SMTP_PASSWORD=your-16-character-app-password
   set ALERT_EMAIL_TO=you@gmail.com
   run-local.bat --send-email
   ```

   These settings only last until you close that window. They're never written to a file.
   History is kept in `state\local-state.json`, so you won't get the same deal twice.

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

### 1. Email (free options)

Any SMTP account works. Two free examples:

* **Gmail:** turn on 2-step verification, create an *App password*, and use host
  `smtp.gmail.com`, port `587`.
* **Outlook.com and other providers:** see your provider's SMTP settings. Some providers
  disable basic-auth SMTP.

Port 465 uses implicit TLS. Any other port uses STARTTLS.

### 2. GitHub Secrets

Go to **Repository → Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Example | Required |
|--------|---------|----------|
| `SMTP_HOST` | `smtp.gmail.com` | yes |
| `SMTP_PORT` | `587` | no (default 587) |
| `SMTP_USERNAME` | `you@gmail.com` | yes |
| `SMTP_PASSWORD` | app password | yes |
| `ALERT_EMAIL_TO` | `you@example.com` (comma-separated for several) | yes |
| `ALERT_EMAIL_FROM` | defaults to `SMTP_USERNAME` | no |

To run locally with email, set the same names as environment variables. Never put them in
`config.toml`.

### 3. Run it

Push to GitHub, then go to **Actions → Price monitor → Run workflow**. Tick *dry run* for
the first try. After that it runs hourly.

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
* An alert's identity is `rule | source | product URL`. After an email is **successfully**
  sent, the alert is recorded. If SMTP fails, nothing is marked and the next run retries.
* An already-alerted product is emailed again only if its price has dropped by at least
  **5 % and 10 €** since the last email. The same or a higher price never re-alerts. The
  thresholds are the `REALERT_*` constants in `price_monitor/alerts.py`.
* If a source fails `source_failure_alert_threshold` runs in a row (HTTP errors, or a page
  with zero extracted products after a layout change), you get one email about it. Another
  one only comes after the source has recovered and then broken again.
* To reset everything, delete the `monitor-state` branch.

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `Setup failed: missing environment variables: …` | A secret is not set or is misspelled (see the table above). |
| `config file not found: config.toml` | Copy `config.example.toml` to `config.toml` and commit it. |
| `no offers extracted` warning | The page has no schema.org Product data, or your selectors no longer match. Compare View Source with your selectors. |
| `robots.txt disallows …` | That URL is off-limits. Pick another category URL. Don't work around it. |
| `HTTP 403` / `AccessDenied` | The site is refusing automated access. Disable that source. |
| `giving up on … after 3 attempts` | The site is temporarily down or rate-limiting. It's retried next hour. |
| Laptops are always UNKNOWN | RAM isn't in the title or on the product page as `32 GB RAM` / `RAM: 32 GB`. Run with `-v` to see each verdict's reasons. |
| Phones never alert | Expected at first. The reference price needs this monitor's own history (see `[rules.phone]`). |
| `… is corrupted` | `state.json` is broken. Delete the `monitor-state` branch to start fresh (alerts may repeat once). |
| Email not arriving | Check the Actions log for `Sent alert email` / SMTP errors, and check your spam folder. Gmail needs an app password, not your normal password. |
| Scheduled runs late or missing | GitHub delays or drops scheduled runs under load. Use *Run workflow* to test. |

## Project layout

```
price_monitor/
  __main__.py   CLI entry point (exit 0 ok, 1 setup error, 2 email failed)
  config.py     config.toml loading and validation
  fetcher.py    robots.txt, rate limit, timeouts, retries, time budget
  extract.py    JSON-LD / CSS-selector extraction, price parsing
  specs.py      conservative spec parsing (unknown = None)
  rules.py      laptop / phone / monitor rules, reference price
  state.py      persistent JSON state
  alerts.py     duplicate-alert policy
  notify.py     SMTP email
  monitor.py    one run, wiring everything together
tests/          pytest suite + synthetic fixtures (no live requests)
```
