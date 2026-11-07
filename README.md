# ScrollPing

[![tests](https://github.com/Hrithik-Vish/ScrollPing/actions/workflows/tests.yml/badge.svg)](https://github.com/Hrithik-Vish/ScrollPing/actions/workflows/tests.yml)

A Python scraper that watches manga / manhwa / manhua / novel aggregator sites for new chapter releases and sends Telegram alerts to the people following those titles, plus a Telegram bot (`@ScrollPingBot`) where people sign up and manage the novels they follow. It is built to run entirely within free-tier limits.

> **Status:** 🚧 V1 nearly complete. The full pipeline (scrape → compare → update database → notify subscribers) works end to end for 5 of the 6 target sites, and the Telegram bot (sign-up, add by pasted link, list, status, remove, pause, delete) is built and live-tested. 388 automated tests pass in CI. The scraper runs on a GitHub Actions schedule and the bot deploys to a Render free web service (see [`docs/deployment.md`](docs/deployment.md)). Still to do for V1: a security review before opening it to other people.

## Why I built this

ScrollPing is my first independent project outside of coursework: an attempt to bridge the gap between academic exercises and a real, end-to-end system. Rather than a tutorial clone, it's a tool I wanted to exist: automated chapter-release alerts for the novels and manga I follow, without manually checking a dozen sites. A deliberate constraint is to run entirely on free tiers of every service used, which has shaped many of the decisions below.

## Features

- Monitors multiple aggregator sites for new chapter releases, with per-site parsing rules kept in a JSON file
- Handles half-chapters (`66.5`) and builds absolute chapter links for each site
- Sends **one batched Telegram message per subscriber** (split into numbered chunks if it gets long)
- **A Telegram bot for self-service**: sign up with `/start`, follow a novel by pasting any link to it, and manage subscriptions with buttons (list, change reading status, remove, pause, delete account)
- Bot hardening: private chats only, per-user rate limiting (token bucket), a 50-novel cap per user, strict link and input validation, ownership checks on every change
- **Per-site error isolation**: a site that is down, blocked or broken is skipped; the rest of the run continues
- Bounded retries on network and database calls, with permanent errors (403/404) not retried
- Telegram sending is rate-limit aware, retries network failures, and never lets a notification problem block the run's final status
- Designed to stay within free-tier limits: batched database calls and one message per user
- 388 automated tests (pytest) run on every push through GitHub Actions

## How it works

ScrollPing is two programs that share one database:

- **`main.py`**, the scraper: runs on a schedule, finds new chapters and sends alerts.
- **`src/bot.py`**, the Telegram command handler: an always-on process that receives commands so people can sign up and manage what they follow.

### The scraper

One run of `main.py`:

1. **Log gate**: insert a `scraper_logs` row (its status defaults to `fail`). If this insert fails, the run stops with exit code 1.
2. **Load config**: fetch the tracked sites from the database and load `site_parse_rules.json` once.
3. **Per-site loop** (each site isolated): fetch the HTML (retrying temporary failures), parse candidate chapter links, load that site's known novels, and compare. Sites without rules, or with a mode that isn't supported yet, are skipped before any request is made.
4. **Accumulate** updates from all sites into one list.
5. **Write**: update the `novels` table. Any novel whose write failed is dropped from the notification list, so it is announced on the next run instead (no stale or duplicate alerts).
6. **Notify**: fetch the subscribers of the updated novels (only people whose notifications are active and whose status for that novel is `reading`), group them by Telegram id, and send each person one message.
7. **Close the log**: flip the `scraper_logs` row to `success`. If the Telegram step hit a fatal problem (invalid or missing token, unexpected error), the row is still flipped, because the scrape itself succeeded, and then the run exits with code 1 so the CI run shows red.

### The bot

`python -m src.bot` runs it with long polling, so it needs no public URL.

| Command | What it does |
|---|---|
| `/start` | Registers you (or switches notifications back on if you paused them) |
| `/add` | Follow a novel: `/add <link>`, or `/add` and then send the link. Paste any link to the novel or one of its chapters, as it is |
| `/list` | Your novels, with a menu per novel (change status, remove) |
| `/remove` | Pick a novel to stop following |
| `/status` | Pick a novel and set it to `reading`, `completed`, `left` or `on hiatus` (only `reading` sends alerts) |
| `/stop` | Pause notifications, or delete your account and data |
| `/help`, `/cancel` | Help and supported sites; abandon a half-finished `/add` |

A new novel starts as "waiting for first update": the next scraper run that sees it on the site's latest-updates page records the chapter and sends the first alert. Links are parsed, never fetched: the site and slug are read from the URL, so `…/comics/nano-machine-ab39de10/chapter/1` and `…/comics/nano-machine/chapter/1` both follow `nano-machine`.

### What a notification looks like

> **New chapters**
> the-hero's-ballad — Ch. 12.5
> lookism — Ch. 631

Each title is a tappable link to the chapter, and link previews are turned off. In V1 the title is the novel's slug; proper display titles are planned for V2.

## Supported sites

| Site | Mode | Status |
|---|---|---|
| asurascans.com | `html` | ✅ working |
| hivetoons.org | `html` | ✅ working |
| kunmanga.online | `html` | ✅ working |
| vortexscans.org | `html` | ✅ working |
| mgeko.cc | `html_li_block` | ✅ working (selector-based parsing of listing blocks) |
| mangadex.org (API) | `json` | ⏸️ deferred until V1 is finalized (the site now serves a browser-verification page) |

Sites that are skipped print a short message and cost no network request.

## Tech stack

| Layer | Tools |
|---|---|
| Scraping | `requests`, `beautifulsoup4`, `lxml` |
| Database | Supabase (PostgreSQL) via `supabase-py` |
| Notifications | `python-telegram-bot` (async, driven through `asyncio.run`) |
| Config | `python-dotenv`, `site_parse_rules.json` |
| Testing / CI | `pytest`, GitHub Actions |
| Dev environment | Docker container + VS Code tasks (optional) |

## Project structure

```
ScrollPing/
├── main.py                    # run(): the scraper pipeline; compilation_fnct(+_li_block), group_subscribers
├── site_parse_rules.json      # per-site rules (mode, regex, base_url, link patterns, ...)
├── src/
│   ├── fetching.py            # fetch_html: raises on temporary failures so retries work
│   ├── parsing.py             # parse_html, parse_html_li_block: candidate chapters from HTML
│   ├── database.py            # all Supabase access: scraper functions (retry loops) and bot functions
│   ├── telegramMessenger.py   # build_message, send_all, retry queue (outgoing alerts)
│   ├── bot.py                 # Telegram command handler: long polling, commands, buttons
│   ├── novel_links.py         # pasted link -> (site, slug), pure
│   ├── ratelimiter.py         # per-user token bucket for incoming messages
│   └── health.py              # tiny /health endpoint for the bot's host
├── render.yaml                # Render Blueprint for the bot's web service
├── .github/workflows/         # tests.yml (CI), scraper.yml (scheduled scraper run)
├── tests/                     # 388 tests + a manual Telegram smoke test
│   ├── fixtures/real_links.json
│   └── test_*.py
├── docs/                      # Obsidian-style notes on each module (main, database, schema, fetching,
│                              #   parsing, telegramMessenger, bot, novel_links, ratelimiter, deployment, pytest, security-review)
├── .github/workflows/tests.yml  # pytest on every push and pull request
├── .vscode/tasks.json         # run files/tests inside the dev container
├── pytest.ini
├── requirements.txt
├── Dockerfile
└── LICENSE
```

## Data model

Columns the code uses:

| Table | Columns |
|---|---|
| `websites` | `id`, `web_domain_name` (unique; the exact URL, also the key in `site_parse_rules.json`) |
| `novels` | `id`, `web_id`, `novel_name` (the slug), `latest_chap` (`not null`, default `-1` = not scraped yet), `chapter_url`, `updated_at`; unique on `(web_id, novel_name)` |
| `users` | `id` (the Supabase auth user id), `username`, `telegram_user_id` (unique), `status` (`active` / `paused`) |
| `user_subscriptions` | `user_id`, `novel_id`, `reading_status` (`reading`, `completed`, `left`, `on hiatus`); unique on `(user_id, novel_id)` |
| `scraper_logs` | `id`, `last_run_timestamp`, `run_status` (default `fail`) |

The full SQL, row level security policies and the pg_cron cleanup jobs are in [`docs/schema.md`](docs/schema.md).

## Setup

```bash
git clone https://github.com/Hrithik-Vish/ScrollPing.git
cd ScrollPing

python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # macOS/Linux

pip install -r requirements.txt
```

Create a `.env` file in the project root (it is git-ignored):

| Variable | Needed for | Notes |
|---|---|---|
| `SUPABASE_URL` | scraper and bot | project URL |
| `SUPABASE_PUBLISHABLE_KEY` | scraper and bot | checked at startup, not otherwise used yet |
| `SUPABASE_SECRET_KEY` | scraper and bot | service key, bypasses RLS: **server-side only**, never in a client |
| `TELEGRAM_BOT_TOKEN` | alerts and the bot | from BotFather |
| `TELEGRAM_CHAT_ID` | smoke test only | your own chat id |

Create the tables in Supabase with the SQL in [`docs/schema.md`](docs/schema.md). In the Supabase Auth settings, turn off public sign-ups: the bot creates users through the admin API, which ignores that switch.

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

- **Scraper:** `.github/workflows/scraper.yml` runs `main.py` on GitHub Actions. cron-job.org starts it every hour through GitHub's API (a fine-grained token limited to this repository with only the Actions permission), and GitHub's own schedule is kept as a backup. Add the repository secrets `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `TELEGRAM_BOT_TOKEN`.
- **Bot:** `render.yaml` is a Render Blueprint for a free web service that redeploys when CI passes (enter the four environment variables in the Render dashboard). The free plan sleeps after 15 minutes without requests, so a timer (cron-job.org) requests the bot's `/health` page every 5 minutes; it also alerts if the bot stops.

Step by step, with the caveats: [`docs/deployment.md`](docs/deployment.md).

## Testing

```bash
python -m pytest            # 388 passed, 2 skipped
```

- No real credentials are needed: `tests/conftest.py` sets dummy values and every external call (HTTP, Supabase, Telegram) is faked.
- GitHub Actions runs the same command on every push and pull request.
- **Manual smoke test** (sends real messages to your own chat; skipped unless you opt in):
  ```bash
  RUN_SMOKE_TESTS=1 python -m pytest tests/test_smokeTest.py -v -s
  ```
  On Windows PowerShell, set `$env:RUN_SMOKE_TESTS = "1"` first.
- Inside the Docker dev container, the VS Code tasks in `.vscode/tasks.json` run the file or the tests (Ctrl+Alt+1 and Ctrl+Alt+2 in my setup).

More detail in `docs/pytest.md`.

## Design decisions worth knowing

- **Compare, then write, then notify.** Notifications go out right after the database update, so users never get data the database doesn't have; failed writes are re-detected next run.
- **Pure, testable core.** `compilation_fnct`, `group_subscribers`, `build_message` and the link parser do no I/O, so they are tested with plain data.
- **Two programs, one database layer.** The scraper tolerates failures and retries; the bot raises on database errors so it can tell "nothing there" from "couldn't look". All database access lives in `database.py`.
- **Links are parsed, never fetched.** Adding a novel costs no request to the site; the scraper confirms it later when the novel shows up on a listing page.
- **`-1` instead of null.** A novel the scraper hasn't seen yet has `latest_chap = -1`, which sorts below every real chapter, so the compare code needs no special case.
- **The bot uses a service key, so it checks everything itself.** Identity comes only from Telegram, every subscription change filters on the user's own id, and incoming messages are rate limited per user.
- **Fail soft per site, fail loud for config.** A broken site is skipped; a bad Telegram token ends the run with a non-zero exit code.
- **Rules as data.** Site quirks (segment counts, hash suffixes, base URLs, chapter regexes) live in `site_parse_rules.json`, not in code.
- **Logs print counts, not identities.** The repository is public, and so are the Actions logs.
- **Everything on free tiers.** Batched queries, one message per user, short pauses between sends.

## Known limitations (V1)

- Notifications show slugs (`the-forgotten-field`) rather than display titles.
- MangaDex is not scraped yet.
- Alerts can lag by up to about an hour (the scraper runs hourly). GitHub's own schedule is only a backup because it is best effort and is switched off after 60 days without repository activity; the primary trigger is cron-job.org, which depends on a GitHub token that expires (at most after a year).
- The bot depends on a free Render service plus a timer that pings it every 5 minutes to stay awake, and shares Render's 750 free hours a month with any other free service in the workspace; free-tier rules can change.
- A novel is only confirmed when it appears on its site's latest-updates page, so a real but quiet novel shows "waiting for first update" until its next release, and a typo'd slug waits there until the 97-day cleanup removes it.
- The 50-novel cap (raised from 20 for the burn-in) is checked in code, not by a database trigger.
- mgeko sub-chapters of 10 or more (`26-10`) compare as `26.1`.
- Blocked users keep being messaged; they resume getting alerts if they come back.
- A notification that fails all its in-run retries is not retried on a later run.
- Skipped links and per-site failures are only visible in the run output.

## Roadmap

**Done**
- [x] Fetching with retry-aware error handling
- [x] Parsing for the four HTML sites, with rules in JSON
- [x] Database layer with retries and per-item failure isolation
- [x] Compilation with absolute, hash-free chapter URLs
- [x] Subscriber grouping and batched Telegram messaging (chunking, flood control, retry queue)
- [x] `run()` wiring with fatal-error handling and a non-zero exit code
- [x] Per-site isolation and mode guards
- [x] GitHub Actions CI for the tests
- [x] Scheduled GitHub Actions workflow for the scraper
- [x] Deployment of the always-on bot to Render, with a health endpoint for the keep-awake timer and monitoring
- [x] mgeko (`html_li_block`) parsing and compilation
- [x] Telegram command handler: sign-up, add by link, list, status, remove, pause, delete, rate limiting
- [x] 388-test suite and a manual smoke test

**Next (V1)**
- [ ] Security pass (RLS review, secrets handling) before opening it to other people: the brief for the reviewer is [`docs/security-review.md`](docs/security-review.md)

**Deferred (V2 and later)**
- [ ] Display titles instead of slugs
- [ ] MangaDex (JSON mode), including a way past the browser verification
- [ ] A separate free-tier Supabase project (or table) for logs: skipped links, per-site failures, errors
- [ ] A notified flag or table for at-least-once delivery across runs
- [ ] Mark blocked subscribers inactive
- [ ] Optional one-time link verification on `/add`, with a per-user limit
- [ ] A database trigger for the subscription cap
- [ ] Playwright for JavaScript-rendered or Cloudflare-protected sites
- [ ] Per-site CSS-selector parsing for sites without chapter keywords in URLs
- [ ] Web app as the main control hub (admin view for the owner, each user sees only their own data), sign-up through Telegram

## Security

Secrets live in `.env` (git-ignored) locally and in GitHub Actions secrets in CI; nothing sensitive is committed. The Supabase secret key bypasses Row Level Security and must stay on the server side, which is why the bot validates its own input and checks ownership on every change. Logging keeps the HTTP libraries quiet because a Telegram request URL contains the bot token. CI logs print counts only, never chat ids or message text. A dedicated security review is planned before the project is opened up (open items are listed in [`docs/schema.md`](docs/schema.md)).

## License

MIT, see [LICENSE](LICENSE).