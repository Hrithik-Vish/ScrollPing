# ScrollPing

[![tests](https://github.com/Hrithik-Vish/ScrollPing/actions/workflows/tests.yml/badge.svg)](https://github.com/Hrithik-Vish/ScrollPing/actions/workflows/tests.yml)

A Python scraper that watches manga / manhwa / manhua / novel aggregator sites for new chapter releases and sends Telegram alerts to the people following those titles. It is built to run entirely within free-tier limits.

> **Status:** 🚧 V1 in progress. The full pipeline (scrape → compare → update database → notify subscribers) works end to end for 4 of the 6 target sites, with 146 automated tests passing in CI. Still to build for V1: the Telegram command handler (`/start`, `/add`), mgeko support, and the scheduled GitHub Actions run.

## Why I built this

ScrollPing is my first independent project outside of coursework: an attempt to bridge the gap between academic exercises and a real, end-to-end system. Rather than a tutorial clone, it's a tool I wanted to exist: automated chapter-release alerts for the novels and manga I follow, without manually checking a dozen sites. A deliberate constraint is to run entirely on free tiers of every service used, which has shaped many of the decisions below.

## Features

- Monitors multiple aggregator sites for new chapter releases, with per-site parsing rules kept in a JSON file
- Handles half-chapters (`66.5`) and builds absolute chapter links for each site
- Sends **one batched Telegram message per subscriber** (split into numbered chunks if it gets long)
- **Per-site error isolation**: a site that is down, blocked or broken is skipped; the rest of the run continues
- Bounded retries on network and database calls, with permanent errors (403/404) not retried
- Telegram sending is rate-limit aware, retries network failures, and never lets a notification problem block the run's final status
- Designed to stay within free-tier limits: batched database calls and one message per user
- 146 automated tests (pytest) run on every push through GitHub Actions

## How it works

One run of `main.py`:

1. **Log gate**: insert a `scraper_logs` row (its status defaults to `fail`). If this insert fails, the run stops with exit code 1.
2. **Load config**: fetch the tracked sites from the database and load `site_parse_rules.json` once.
3. **Per-site loop** (each site isolated): fetch the HTML (retrying temporary failures), parse candidate chapter links, load that site's known novels, and compare. Sites without rules, or with a mode that isn't supported yet, are skipped before any request is made.
4. **Accumulate** updates from all sites into one list.
5. **Write**: update the `novels` table. Any novel whose write failed is dropped from the notification list, so it is announced on the next run instead (no stale or duplicate alerts).
6. **Notify**: fetch the subscribers of the updated novels, group them by Telegram id, and send each person one message.
7. **Close the log**: flip the `scraper_logs` row to `success`. If the Telegram step hit a fatal problem (invalid or missing token, unexpected error), the row is still flipped, because the scrape itself succeeded, and then the run exits with code 1 so the CI run shows red.

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
| mgeko.cc | `html_li_block` | 🔜 next: rules and findings are ready, the parsing branch is not built |
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
├── main.py                    # run(): orchestrates the pipeline; compilation_fnct, group_subscribers
├── site_parse_rules.json      # per-site parsing rules (mode, regex, base_url, ...)
├── src/
│   ├── fetching.py            # fetch_html: raises on temporary failures so retries work
│   ├── parsing.py             # parse_html: candidate chapter links from HTML
│   ├── database.py            # Supabase reads/writes with retry loops
│   └── telegramMessenger.py   # build_message, send_all, retry queue
├── tests/                     # 146 tests + a manual Telegram smoke test
│   ├── fixtures/real_links.json
│   └── test_*.py
├── docs/                      # Obsidian-style notes on each module (main, database, fetching,
│                              #   parsing, telegramMessenger, pytest)
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
| `websites` | `id`, `web_domain_name` (the exact URL, also the key in `site_parse_rules.json`) |
| `novels` | `id`, `web_id`, `novel_name` (the slug), `latest_chap`, `chapter_url`, `updated_at` |
| `users` | `telegram_user_id` |
| `user_subscriptions` | `user_id`, `novel_id`, `reading_status`; unique on `(user_id, novel_id)` |
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

Create a `.env` file in the project root (it is git-ignored):

| Variable | Needed for | Notes |
|---|---|---|
| `SUPABASE_URL` | the scraper | project URL |
| `SUPABASE_PUBLISHABLE_KEY` | the scraper | checked at startup, not otherwise used yet |
| `SUPABASE_SECRET_KEY` | the scraper | service key, bypasses RLS: **server-side only**, never in a client |
| `TELEGRAM_BOT_TOKEN` | notifications | from BotFather |
| `TELEGRAM_CHAT_ID` | smoke test only | your own chat id |

Run the scraper:

```bash
python main.py
```

You also need your tables in Supabase and at least one subscribed user; until the command handler exists, subscriptions are added by hand.

## Testing

```bash
python -m pytest            # 146 passed, 2 skipped
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
- **Pure, testable core.** `compilation_fnct`, `group_subscribers` and `build_message` do no I/O, so they are tested with plain data.
- **Fail soft per site, fail loud for config.** A broken site is skipped; a bad Telegram token ends the run with a non-zero exit code.
- **Rules as data.** Site quirks (segment counts, hash suffixes, base URLs, chapter regexes) live in `site_parse_rules.json`, not in code.
- **Logs print counts, not identities.** The repository is public, and so are the Actions logs.
- **Everything on free tiers.** Batched queries, one message per user, short pauses between sends.

## Known limitations (V1)

- Notifications show slugs (`the-forgotten-field`) rather than display titles.
- Users can't subscribe from Telegram yet; rows are added by hand.
- mgeko and MangaDex are not scraped yet.
- There is no scheduled workflow yet, so runs are started manually.
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
- [x] 146-test suite and a manual smoke test
- [x] GitHub Actions CI for the tests

**Next (V1)**
- [ ] Telegram command handler (`/start`, `/add`): a separate long-running process
- [ ] mgeko (`html_li_block`) parsing branch and tests
- [ ] Scheduled GitHub Actions workflow for the scraper (repository secrets for Supabase and the bot token)
- [ ] Security pass (RLS review) and deployment

**Deferred (V2 and later)**
- [ ] Display titles instead of slugs
- [ ] MangaDex (JSON mode), including a way past the browser verification
- [ ] A separate free-tier Supabase project (or table) for logs: skipped links, per-site failures, errors
- [ ] A notified flag or table for at-least-once delivery across runs
- [ ] Mark blocked subscribers inactive
- [ ] Playwright for JavaScript-rendered or Cloudflare-protected sites
- [ ] Per-site CSS-selector parsing for sites without chapter keywords in URLs
- [ ] Web frontend for sign-up, tracking and subscriptions

## Security

Secrets live in `.env` (git-ignored) locally and in GitHub Actions secrets in CI; nothing sensitive is committed. The Supabase secret key bypasses Row Level Security and must stay on the server side. CI logs print counts only, never chat ids or message text.

## License

MIT, see [LICENSE](LICENSE).
