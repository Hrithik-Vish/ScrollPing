# ScrollPing

A Python-based scraper that monitors manga/manhwa/manhua/novel aggregator sites for new chapter releases and sends Telegram notifications to subscribers — all built to run entirely within free-tier service limits.

> **Status:** 🚧 Work in progress — core modules (`fetching`, `parsing`, `database`) are complete; `main.py` orchestration is in active development.

## Why I built this

ScrollPing is my first independent project outside of coursework — an attempt to bridge the gap between academic programming exercises and a real, end-to-end system. Rather than a tutorial clone, it's a genuine tool I wanted to exist: automated chapter-release alerts for the novels/manga I follow, without needing to manually check a dozen sites. A deliberate constraint of the project is to run entirely on free tiers of every service used, which has shaped a lot of the architectural decisions below.

## Features

- Monitors multiple aggregator sites for new chapter releases
- Sends batched Telegram notifications to subscribers
- Supports half-chapter numbering (e.g. `66.5`)
- Per-site error isolation — a failure on one site doesn't crash the full run
- Retry-with-backoff on network and database operations
- Designed to stay within free-tier rate limits (batched DB calls, minimal requests)
- Runs on a schedule via GitHub Actions — no server required

## Tech Stack

| Layer | Tools |
|---|---|
| Scraping | `requests`, `beautifulsoup4`, `lxml`, `Playwright` (planned, for JS-rendered sites) |
| Database | Supabase (PostgreSQL) via `supabase-py`, `pg_cron` |
| Notifications | `python-telegram-bot` |
| Scheduling / CI | GitHub Actions |
| Config | `python-dotenv` |

## How It Works

1. **Log gate check** — insert a new run entry into `scraper_logs` (defaults to `fail` until the run completes successfully)
2. **Fetch site list** — pull all tracked sites from the database
3. **Per-site loop:**
   - Fetch the site's HTML
   - Fetch that site's known novels from the database
   - Parse the HTML for chapter listings
   - Compare parsed chapters against stored records to find updates
4. **Batch accumulate** — all detected updates across sites are collected into a single list
5. **Batch write** — one call each to update the database, fetch affected subscribers, and send Telegram notifications (keeps everything within free-tier rate limits)
6. **Mark success** — flip the run's log status to `success` once everything completes

## Project Structure

```
ScrollPing/
├── main.py              # Orchestrates the full pipeline
├── src/
│   ├── __init__.py
│   ├── fetching.py       # HTTP requests with retries & timeouts
│   ├── parsing.py        # HTML parsing & chapter extraction
│   ├── database.py       # Supabase read/write operations
│   └── telegramMessenger.py  # (planned) Telegram notification logic
├── .env                  # Local secrets (not committed)
├── requirements.txt
└── README.md
```

## Setup

```bash
# Clone the repo
git clone https://github.com/Hrithik-Vish/ScrollPing.git
cd ScrollPing

# Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate      # Windows
source venv/bin/activate   # macOS/Linux

# Install dependencies
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
SUPABASE_URL=your_supabase_project_url
SUPABASE_SERVICE_ROLE_KEY=your_service_role_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
```

Run the scraper:

```bash
python main.py
```

## Roadmap

**Done:**
- [x] Fetching module (retries, timeouts, error handling)
- [x] Parsing module (chapter extraction via regex + BeautifulSoup)
- [x] Database module (Supabase read/write with retry logic)

**In progress:**
- [ ] `main.py` orchestration pipeline

**Next up:**
- [ ] Telegram notification integration
- [ ] GitHub Actions scheduled workflow

**Deferred (post-v1):**
- [ ] Playwright integration for JS-rendered / Cloudflare-protected sites
- [ ] Per-site CSS selector parsing for sites without "chapter" in the URL
- [ ] Regex edge case refinement
- [ ] Telegram bot commands for adding novels directly via chat

**Future:**
- [ ] Web frontend for user sign-up, novel tracking, and subscription management

## License

MIT
