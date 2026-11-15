# ScrollPing

[![tests](https://github.com/Hrithik-Vish/ScrollPing/actions/workflows/tests.yml/badge.svg)](https://github.com/Hrithik-Vish/ScrollPing/actions/workflows/tests.yml)

A Python scraper that watches manga / manhwa / manhua / novel aggregator sites for new chapter releases and sends Telegram alerts to the people following those titles, plus a Telegram bot (`@ScrollPingBot`) where people sign up and manage the novels they follow. It is built to run entirely within free-tier limits.

> **Status:** 🚧 V1 burn-in build. The whole pipeline (scrape → compare → queue → deliver) works end to end for 5 of the 6 target sites, with real titles in the alerts, early-access (locked chapter) awareness for asurascans, a delivery queue that survives crashes, per-site health tracking with admin alerts and a daily digest, and a bot with a two-level `/list`, per-novel alert timing and `/settings`. 1,031 automated tests pass in CI. The scraper runs on a GitHub Actions schedule and the bot deploys to a Render free web service (see [`docs/deployment.md`](docs/deployment.md)). Still to do for V1: the live burn-in and a security review before opening it to other people.

## Why I built this

ScrollPing is my first independent project outside of coursework: an attempt to bridge the gap between academic exercises and a real, end-to-end system. Rather than a tutorial clone, it's a tool I wanted to exist: automated chapter-release alerts for the novels and manga I follow, without manually checking a dozen sites. A deliberate constraint is to run entirely on free tiers of every service used, which has shaped many of the decisions below.

## Features

- Monitors multiple aggregator sites for new chapter releases, with per-site parsing rules kept in a JSON file that is validated at startup
- Real **display titles** scraped from the sites (with a readable guess from the slug when a site does not give one)
- **Early-access awareness (asurascans):** locked chapters are detected, with their unlock time. Members are told straight away, everyone else when the chapter becomes free; each novel can override that. Alerts show `🔒 free in ~5h` or `🔒 early access`
- **A delivery queue** (`pending_notifications`): detected chapters are queued first and the novel is advanced second, so a crash can never lose an alert; every send is acknowledged row by row, failures are retried, and a user who blocked the bot is paused automatically
- Sends **one batched Telegram message per user**, grouped by site, split into numbered chunks (only the first one makes a sound) if it gets long; a refused message falls back to one message per novel, then to plain text
- **Site health:** every run records whether each site was `ok`, `degraded` or `failed`; three failed runs in a row alert the admins, and a daily digest summarises the sites and the queue
- **A Telegram bot for self-service:** sign up with `/start`, follow a novel by pasting any link, manage subscriptions through a two-level menu (site → novel → status, alert timing, remove), and choose your account type on early-access sites with `/settings`
- Bot hardening: HTML-escaped output everywhere, private chats only, per-user rate limiting, a 50-novel cap per user, strict link and input validation, ownership checks on every change
- **Per-site error isolation:** a site that is down, blocked or broken is skipped; the rest of the run continues
- Designed to stay within free-tier limits: batched database calls (one RPC for a whole run's novel updates), one message per user
- 1,031 automated tests (pytest) run on every push through GitHub Actions

## How it works

ScrollPing is two programs that share one database:

- **`main.py`**, the scraper: runs on a schedule, finds new chapters, queues alerts and delivers them.
- **`src/bot.py`**, the Telegram command handler: an always-on process that receives commands so people can sign up and manage what they follow.

### The scraper

One run of `main.py` (details in [`docs/main.md`](docs/main.md)):

1. **Log gate:** insert a `scraper_logs` row (its status defaults to `fail`). If this insert fails, the run stops with exit code 1.
2. **Queue maintenance:** give back rows a dead run left half-sent, cancel rows whose subscription ended, expire rows more than 24 hours overdue.
3. **Load config:** fetch the tracked sites and load `site_parse_rules.json` once; problems in the rules file are printed.
4. **Per-site loop** (each site isolated): fetch the HTML (retrying temporary failures), parse it into chapter items with titles and lock state, compare with the novels the database knows, and return a result object (`ok` / `degraded` / `failed` / `skipped`). Sites without rules, or with a mode that is not supported yet, are skipped before any request.
5. **Site health** is recorded and **titles** are stored (they never touch `updated_at`, so they cannot keep a dead novel alive).
6. **Recipients:** who reads the updated novels (active users, status `reading`), with their site tier and per-novel timing.
7. **Queue, then novels:** one queue row per chapter and recipient, each with its own due time (`now`, or the unlock time for people who wait), **inserted first**; only then are the novels updated, in one call. A crash between the two only causes re-detection, and the unique key makes the re-insert a no-op.
8. **Deliver:** claim due rows (`FOR UPDATE SKIP LOCKED`, so two runs can never send the same row), send, and acknowledge each row.
9. **Admin step:** alerts for sites that failed 3+ runs in a row, and the daily digest after 23:35 IST.
10. **Close the log:** flip the `scraper_logs` row to `success`. If delivery hit a fatal problem (invalid or missing token, unexpected error), the row is still flipped, because the scrape itself succeeded, and then the run exits with code 1 so the CI run shows red.

### The bot

`python -m src.bot` runs it with long polling, so it needs no public URL.

| Command | What it does |
|---|---|
| `/start` | Registers you (or switches notifications back on if you paused them) |
| `/add` | Follow a novel: `/add <link>`, or `/add` and then send the link. Paste any link to the novel or one of its chapters, as it is. On an early-access site you are asked once what kind of reader you are |
| `/list` | Your novels: pick a site, then a novel, then change its status, its alert timing (early-access sites) or remove it |
| `/settings` | Your account type (for example "Free reader" or "Asura+ member") on sites with early access; it decides when early chapters alert you |
| `/remove` | Pick a novel to stop following |
| `/status` | Pick a novel and set it to `reading`, `completed`, `left` or `on hiatus` (only `reading` sends alerts) |
| `/stop` | Pause notifications, or delete your account and data |
| `/help`, `/cancel` | Help and supported sites; abandon a half-finished `/add` |

A new novel starts as "waiting for first update": the next scraper run that sees it on the site's latest-updates page records the chapter and sends the first alert. Links are parsed, never fetched: the site and slug are read from the URL, so `…/comics/nano-machine-ab39de10/chapter/1` and `…/comics/nano-machine/chapter/1` both follow `nano-machine`.

### What a notification looks like

> **📚 New chapters**
>
> **Asura Scans**
> • Killer Pietro — Ch. 156 🔒 free in ~5h
>
> **Hive Toons**
> • Lookism — Ch. 631

Each title is a tappable link to the chapter, and link previews are turned off. The lock badge is worked out at the moment of sending, so it is always current.

## Supported sites

| Site | Mode | Status |
|---|---|---|
| asurascans.com | `html` + page island | ✅ working, with early-access detection and unlock times |
| hivetoons.org | `html` | ✅ working, locked chapters flagged (no unlock time) |
| kunmanga.online | `html` | ✅ working |
| vortexscans.org | `html` | ✅ working, locked chapters flagged (no unlock time) |
| mgeko.cc | `html_li_block` | ✅ working (selector-based parsing of listing blocks) |
| mangadex.org (API) | `json` | ⏸️ deferred until V1 is finalized (the site now serves a browser-verification page) |

Sites that are skipped print a short message and cost no network request.

## Tech stack

| Layer | Tools |
|---|---|
| Scraping | `requests`, `beautifulsoup4`, `lxml`, `soupsieve` |
| Database | Supabase (PostgreSQL, pg_cron) via `supabase-py` |
| Notifications | `python-telegram-bot` (async, driven through `asyncio.run`) |
| Config | `python-dotenv`, `site_parse_rules.json` |
| Testing / CI | `pytest`, GitHub Actions |
| Dev environment | Docker container + VS Code tasks (optional) |

## Project structure

```
ScrollPing/
├── main.py                    # run(): the scraper pipeline; compilation, process_website, dispatch, admin step
├── site_parse_rules.json      # per-site rules (mode, regex, selectors, lock detection, tier labels, ...)
├── src/
│   ├── fetching.py            # fetch_html: raises on temporary failures so retries work
│   ├── parsing.py             # parse_page (items + titles + lock state), classify_site, validate_rules
│   ├── scheduling.py          # pure: resolve_due_at, build_queue_records, badge_for
│   ├── admin.py               # pure: admin ids, digest schedule, alert and digest text
│   ├── database.py            # all Supabase access: scraper functions, queue functions, bot functions
│   ├── telegramMessenger.py   # build_message, send_all (queue delivery), send_admin
│   ├── bot.py                 # Telegram command handler: long polling, menus, buttons
│   ├── novel_links.py         # pasted link -> (site, slug); title helpers; pure
│   ├── ratelimiter.py         # per-user token bucket for incoming messages
│   └── health.py              # tiny /health endpoint for the bot's host
├── render.yaml                # Render Blueprint for the bot's web service
├── .github/workflows/         # tests.yml (CI), scraper.yml (scheduled scraper run)
├── scripts/
│   └── preview_run.py         # read-only preview of what the next run would queue and send (never writes)
├── tests/                     # 1,040 tests (9 manual smoke tests are skipped by default)
│   ├── fixtures/              # trimmed real captures of all five sites + real_links.json
│   └── test_*.py
├── docs/                      # Obsidian-style notes on each module, schema, deployment, tests, security brief
│   └── migrations/            # the V1 burn-in SQL migration and its test script
├── .env.example               # every environment variable, documented
├── .python-version            # 3.12 (what CI and Render use)
├── .vscode/tasks.json         # run files/tests inside the dev container
├── pytest.ini
├── requirements.txt
├── Dockerfile
└── LICENSE
```

## Data model

Columns the code uses (the full SQL, row level security and the cron jobs are in [`docs/schema.md`](docs/schema.md); the V1 burn-in additions are in [`docs/migrations/001_v1_burnin.sql`](docs/migrations/001_v1_burnin.sql)):

| Table | Columns |
|---|---|
| `websites` | `id`, `web_domain_name` (unique; the exact URL, also the key in `site_parse_rules.json`) |
| `novels` | `id`, `web_id`, `novel_name` (the slug), `display_title` (scraped title, nullable), `latest_chap` (`not null`, default `-1` = not scraped yet), `chapter_url`, `updated_at`; unique on `(web_id, novel_name)` |
| `users` | `id` (the Supabase auth user id), `username`, `telegram_user_id` (unique), `status` (`active` / `paused`) |
| `user_subscriptions` | `user_id`, `novel_id`, `reading_status` (`reading`, `completed`, `left`, `on hiatus`), `alert_timing` (`early` / `when_free` / null = follow the site tier); unique on `(user_id, novel_id)` |
| `user_site_tiers` | `user_id`, `web_id`, `tier` (`standard` / `member`) |
| `pending_notifications` | the delivery queue: user, novel, chapter, URL, `access_state`, `unlock_at`, `due_at`, `status` (`pending`, `sending`, `sent`, `dead`, `cancelled`), attempts; unique on `(user_id, novel_id, chapter)` |
| `site_health` | per site: last status, consecutive failures, when it started failing, last error, whether the admin was told |
| `app_state` | small key/value store (the digest's "last sent" marker) |
| `scraper_logs` | `id`, `last_run_timestamp`, `run_status` (default `fail`) |

## Setup

```bash
git clone https://github.com/Hrithik-Vish/ScrollPing.git
cd ScrollPing

python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # macOS/Linux

pip install -r requirements.txt
```

Copy `.env.example` to `.env` (git-ignored) and fill it in:

| Variable | Needed for | Notes |
|---|---|---|
| `SUPABASE_URL` | scraper and bot | project URL |
| `SUPABASE_PUBLISHABLE_KEY` | scraper and bot | checked at startup, not otherwise used yet |
| `SUPABASE_SECRET_KEY` | scraper and bot | service key, bypasses RLS: **server-side only**, never in a client |
| `TELEGRAM_BOT_TOKEN` | alerts and the bot | from BotFather |
| `ADMIN_TELEGRAM_USER_IDS` | admin alerts and the digest | comma-separated Telegram ids; empty switches both off |
| `TELEGRAM_CHAT_ID`, `SMOKE_USER_ID`, `SMOKE_NOVEL_ID` | manual smoke tests only | your own chat id; a real user id and novel id |

Create the tables in Supabase with the SQL in [`docs/schema.md`](docs/schema.md), then run [`docs/migrations/001_v1_burnin.sql`](docs/migrations/001_v1_burnin.sql) (it is safe to run twice). In the Supabase Auth settings, turn off public sign-ups: the bot creates users through the admin API, which ignores that switch.

Run the scraper (one pass, meant for a schedule):

```bash
python main.py
```

Run the bot (stays running; send `/start` to your bot in Telegram):

```bash
python -m src.bot
```

Only one process can poll Telegram per bot token at a time, so stop a local bot before starting a deployed one. If `HEALTH_PORT` (or `PORT`, which Render sets) is present, the bot also serves a `/health` page for hosts and timers; with neither set, no port is opened.

### Deployment

- **Scraper:** `.github/workflows/scraper.yml` runs `main.py` on GitHub Actions. cron-job.org starts it every hour through GitHub's API (a fine-grained token limited to this repository with only the Actions permission), and GitHub's own schedule is kept as a backup. Add the repository secrets `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `TELEGRAM_BOT_TOKEN` and `ADMIN_TELEGRAM_USER_IDS`.
- **Bot:** `render.yaml` is a Render Blueprint for a free web service that redeploys when CI passes (enter the four environment variables in the Render dashboard). The free plan sleeps after 15 minutes without requests, so a timer (cron-job.org) requests the bot's `/health` page every 5 minutes; it also alerts if the bot stops.

Step by step, with the caveats: [`docs/deployment.md`](docs/deployment.md).

## Testing

```bash
python -m pytest            # 1031 passed, 9 skipped
```

- No real credentials are needed: `tests/conftest.py` sets dummy values and every external call (HTTP, Supabase, Telegram) is faked.
- GitHub Actions runs the same command on every push and pull request.
- **Manual smoke tests** (skipped unless you opt in; they send real messages to your own chat and read your real database):
  ```bash
  RUN_SMOKE_TESTS=1 python -m pytest tests/test_smokeTest.py tests/test_live_database_smoke.py -v -s
  ```
  On Windows PowerShell, set `$env:RUN_SMOKE_TESTS = "1"` first.
- The SQL functions have their own test script, [`docs/migrations/001_v1_burnin_tests.sql`](docs/migrations/001_v1_burnin_tests.sql), run by hand in the Supabase SQL editor (55 checks, including a two-tab test that two workers never claim the same row).
- Inside the Docker dev container, the VS Code tasks in `.vscode/tasks.json` run the file or the tests (Ctrl+Alt+1 and Ctrl+Alt+2 in my setup).

More detail in [`docs/pytest.md`](docs/pytest.md).

## Design decisions worth knowing

- **Queue first, novels second.** A detected chapter is written to the delivery queue before the novel's `latest_chap` moves. A crash in between only means the chapter is detected again next run, and a unique key makes the second insert a no-op. A novel whose queue rows could not be written is simply not advanced.
- **Pure, testable core.** `compilation_fnct`, `resolve_due_at`, `build_queue_records`, `badge_for`, `build_message`, `build_digest` and the link parser do no I/O, so they are tested with plain data. The messenger knows nothing about the database; it reports outcomes through a small `delivery` object.
- **Decisions live in code, not in SQL.** Who is told when is decided by readable, tested Python; the SQL functions only do the things that must be atomic (claiming rows, batch writes).
- **Two programs, one database layer.** The scraper tolerates failures and retries; the bot raises on database errors so it can tell "nothing there" from "couldn't look". All database access lives in `database.py`.
- **Links are parsed, never fetched.** Adding a novel costs no request to the site; the scraper confirms it later when the novel shows up on a listing page.
- **`-1` instead of null.** A novel the scraper hasn't seen yet has `latest_chap = -1`, which sorts below every real chapter, so the compare code needs no special case.
- **The bot uses a service key, so it checks everything itself.** Identity comes only from Telegram, every change filters on the user's own id, every outgoing text is HTML-escaped, and incoming messages are rate limited per user.
- **Fail soft per site, fail loud for config.** A broken site is skipped and counted in its health row; a bad Telegram token ends the run with a non-zero exit code.
- **Rules as data.** Site quirks (segment counts, selectors, lock detection, tier labels) live in `site_parse_rules.json`, which is validated at startup.
- **Logs print counts, not identities.** The repository is public, and so are the Actions logs.
- **Everything on free tiers.** Batched queries, one message per user, short pauses between sends.

## Known limitations (V1)

- MangaDex is not scraped yet.
- Only asurascans gives an unlock time, so only there can "only when free" alerts wait. Hive and vortex flag locked chapters but do not say when they unlock, so everyone is alerted straight away with an "early access" badge, and the alert timing menu is not offered for those sites.
- If a site renames its lock markup, alerts keep working but lock state turns "unknown" and the site shows as `degraded` in the digest; if a site stops giving titles, alerts fall back to a title guessed from the slug.
- Alerts can lag by up to about an hour (the scraper runs hourly). GitHub's own schedule is only a backup because it is best effort and is switched off after 60 days without repository activity; the primary trigger is cron-job.org, which depends on a GitHub token that expires (at most after a year).
- The bot depends on a free Render service plus a timer that pings it every 5 minutes to stay awake, and shares Render's 750 free hours a month with any other free service in the workspace; free-tier rules can change.
- A novel is only confirmed when it appears on its site's latest-updates page, so a real but quiet novel shows "waiting for first update" until its next release, and a typo'd slug waits there until the 97-day cleanup removes it.
- The weekly cleanup deletes websites that have no novels, and a user's saved account type for that site goes with it; they are simply asked again the next time they follow a novel there.
- The 50-novel cap (raised from 20 for the burn-in) is checked in code, not by a database trigger.
- mgeko sub-chapters of 10 or more (`26-10`) compare as `26.1`.
- A queued alert that cannot be delivered for 24 hours expires (it is marked dead, not retried forever).
- The queue can only deliver what it was told about: a chapter that appears and disappears between two runs is never seen.

## Roadmap

**Done**
- [x] Fetching with retry-aware error handling
- [x] Parsing for the four HTML sites and mgeko, with rules in validated JSON
- [x] Display titles, and early-access awareness for asurascans
- [x] A crash-safe delivery queue with per-row acknowledgements and retries
- [x] Per-site health, admin alerts and a daily digest
- [x] Per-site isolation and mode guards
- [x] GitHub Actions CI and a scheduled scraper workflow
- [x] Deployment of the always-on bot to Render, with a health endpoint
- [x] Telegram command handler: sign-up, add by link, two-level list, alert timing, settings, pause, delete, rate limiting
- [x] A 1,000+ test suite, SQL function tests, manual smoke tests and a read-only preview tool

**Next (V1)**
- [ ] Live burn-in: a read-only preview, a local run, then watch the first scheduled runs and a month of measurements, step by step in [`docs/burnin.md`](docs/burnin.md)
- [ ] Security pass (RLS review, secrets handling) before opening it to other people: the brief for the reviewer is [`docs/security-review.md`](docs/security-review.md)

**Deferred (V2 and later)**
- [ ] MangaDex (JSON mode), including a way past the browser verification
- [ ] Unlock times for hive and vortex (if the sites ever expose them)
- [ ] A separate free-tier Supabase project (or table) for logs: skipped links, per-site failures, errors
- [ ] Outage start and duration in the daily digest (needs the failure start kept until recovery)
- [ ] Optional one-time link verification on `/add`, with a per-user limit
- [ ] A database trigger for the subscription cap
- [ ] Playwright for JavaScript-rendered or Cloudflare-protected sites
- [ ] Web app as the main control hub (admin view for the owner, each user sees only their own data), sign-up through Telegram

## Security

Secrets live in `.env` (git-ignored) locally and in GitHub Actions secrets in CI; nothing sensitive is committed. The Supabase secret key bypasses Row Level Security and must stay on the server side, which is why the bot validates its own input and checks ownership on every change. The new SQL functions and tables are closed to the public API roles (`anon`, `authenticated`) and open only to the service role; a test checks that against the real project. Logging keeps the HTTP libraries quiet because a Telegram request URL contains the bot token. CI logs print counts only, never chat ids, titles or message text. A dedicated security review is planned before the project is opened up (open items are listed in [`docs/security-review.md`](docs/security-review.md) and [`docs/schema.md`](docs/schema.md)).

## License

MIT, see [LICENSE](LICENSE).
