# Testing (pytest) and CI

**State:** 1,042 tests pass and 9 are skipped by design (manual smoke tests that talk to your real Telegram chat or database). The suite runs in a few seconds, needs no real credentials, and never touches the network. A clean virtual environment built from `requirements.txt` runs it green.

## Layout
```
pytest.ini                        # [pytest] pythonpath = . / testpaths = tests
tests/
├── conftest.py                   # dummy env vars (and an empty ADMIN_TELEGRAM_USER_IDS) so `import main` works
├── fixtures/                     # trimmed real captures: asura/hive/vortex/kun/mgeko _latest.html, real_links.json
├── test_parse_page.py            # parse_page, the asura island, classify_site, validate_rules, the real rules file
├── test_scheduling.py            # resolve_due_at, build_queue_records, badge_for
├── test_admin.py                 # admin ids, digest schedule (incl. a simulated week), alert/digest text
├── test_scraper_database.py      # scraper-side database functions against a scripted fake Supabase
├── test_main.py                  # retry, compilation, title updates, process_website results, dispatch helpers
├── test_run.py                   # run(): the order of the steps and each step's failure handling
├── test_admin_step.py            # main.run_admin_tasks: alerts and the digest, database and Telegram faked
├── test_pipeline_flow.py         # the whole pipeline over the real fixtures with an in-memory queue
├── test_preview_run.py           # scripts/preview_run.py: strictly read-only, and what it reports
├── test_site_isolation.py        # skipped sites, per-site isolation
├── test_mgeko.py                 # li-block parsing and compilation
├── test_fetching.py              # fetch_html + retry working together
├── test_telegram_messenger.py    # build_message, send_all, send_admin with a scripted FakeBot
├── test_novel_links.py           # link -> (site, slug), slug tiers, title helpers, against the real rules file
├── test_ratelimiter.py           # token bucket with a fake clock
├── test_bot_database.py          # bot-side database functions against a scripted fake Supabase
├── test_bot.py                   # bot helpers, views, every handler and callback, lifecycle, HTML escaping
├── test_health.py                # the /health endpoint, with real HTTP requests on a free local port
├── test_smokeTest.py             # MANUAL: sends real Telegram messages (skipped by default)
└── test_live_database_smoke.py   # MANUAL: reads and writes your real Supabase project (skipped by default)
```
`pytest.ini` lives in the **root** on purpose. A `pytest.ini` inside `tests/` is ignored when the root has one, which is how `pythonpath = .` was once silently lost (plain `pytest` failed with `No module named 'src'` while `python -m pytest` happened to work).

## Running
- **VS Code (Docker):** tasks in `.vscode/tasks.json` run inside the `scrollping-dev` container (project at `/app`): run current file, pytest, verbose, stop at first failure, last failed, current file, by keyword. Shortcuts: Ctrl+Alt+1 run file, Ctrl+Alt+2 pytest, Ctrl+Alt+3 onward the variants.
- **Terminal:** `python -m pytest` (or just `pytest`).
- **CI:** `.github/workflows/tests.yml` runs `python -m pytest -q` on every push and pull request (Python 3.12, pip cache, no secrets, `permissions: contents: read`).
- **Python version:** `.python-version` says 3.12; the file must keep its leading dot or Render and other tools ignore it.

## Conventions
- **Everything external is faked.** `conftest.py` sets dummy `SUPABASE_*` values because `database.py` raises at import without them, an empty `ADMIN_TELEGRAM_USER_IDS` (so a real id in a developer's `.env` can never make a test message an admin), and `PTB_TIMEDELTA=1` so python-telegram-bot's `RetryAfter.retry_after` is a `timedelta` (no deprecation warnings; the code handles both forms).
- **Parsing is tested on real markup.** The trimmed fixtures are cut from captures taken on a GitHub Actions runner, structure intact, so the selectors that run in production are the ones under test, including the traps found in the real pages (comment links with `#comment-…`, the `newly-free-chapter-row` class, a `%3A` slug on vortex, three title layouts on kunmanga).
- **Strict fakes.** `FakeSupabase` (`test_scraper_database.py`) requires `rpc()` to get a params dict, like the real client, because a lenient fake once hid a `TypeError` that would have hit the live client. It records every call chain so tests assert exactly what was sent.
- **Scripted Telegram.** `FakeBot` replays per-chat outcomes (success or an exception) and a recording `FakeDelivery` logs each acknowledgement into one ordered event list shared with the bot, so tests can assert on **timing** ("ack right after each send"), not only on counts.
- **An in-memory world for the flow tests.** `test_pipeline_flow.py` implements a tiny queue (insert with the unique key, claim by due time, acknowledge) and runs the real `run()` twice: a member alerted at once, a standard reader later, nothing delivered twice, a crash between queue and novel update losing nothing, and the real messenger through `QueueDelivery`.
- **Fresh data per test; no mutation.** Compile and scheduling functions do not mutate their inputs, and tests assert it.
- **No real waiting.** `main.time.sleep`, `database.time.sleep` and `asyncio.sleep` are patched; the send-spacing clock is frozen. Async code is driven with `asyncio.run`, no plugin needed.
- **Check that tests can fail (mutation checks).** Key guarantees were validated by deliberately breaking the code and watching tests go red: novels updated before the queue (9 failures), the member default ignored (6), acknowledgements skipped (14), blocked users given only their current chunk's ids (3), alerts marked reported without delivery, the digest marker stored without delivery, unescaped titles, a toggle without its site check, no HTML default. The code was restored each time.

## What is covered
- **`test_parse_page.py`** — `unwrap_astro`; the asura island (lock flags, unlock times, hash-stripped slugs, titles, URL **check A**: every island URL equals an anchor in the same page, fallbacks for a missing or malformed island, bad or missing unlock times, number formats); hive and vortex (lock class, `newly-free` trap, titles from the `title` attribute, a series with no title, the `%3A` slug, comment links, duplicate merging, other hosts ignored); kunmanga's three layouts; mgeko blocks; `parse_page` shape for every site; `classify_site`; `validate_rules` for the real file and for each kind of mistake; the scraper's slug equals the bot's for a pasted link on every fixture.
- **`test_scheduling.py`** — the full `resolve_due_at` matrix, `build_queue_records` (uniform keys, per-site tiers, unlock stored only for locked rows, duplicates, no mutation), `badge_for` and `format_remaining` edges, and that the badge follows the time of sending.
- **`test_admin.py` / `test_admin_step.py`** — see [[admin]].
- **`test_scraper_database.py`** — every scraper-side function: query shape, chunking, retries, fail-soft behaviour, the `SubscriberFetch` holding novels back, queue batches, acknowledgements, blocked users, health and app state (including that `get_app_state` raises).
- **`test_main.py`** — `retry`; `compilation_fnct` (updates, ordering, fractional chapters, hash kept in the URL, apostrophes, carrying title, lock state and unlock time of the winning item); `find_title_updates`; `process_website` result objects for ok, degraded, failed and skipped paths on real fixtures; real captured links per site; `group_claimed`, `QueueDelivery`, `dispatch_queue`, summaries.
- **Outage regression (`test_main.py`, `test_pipeline_flow.py`)** — a Telegram outage must cost a failing row exactly **one** attempt per run: the dispatch loop stops after a pass that gave rows back (before this, all 10 passes re-claimed the same rows: 80 send attempts for two users in one run, and the original attempt cap of 5 marked alerts dead within the minute). The flow tests use an in-memory queue that counts attempts like the SQL does, and check an outage over several hourly runs, delivery after recovery, and the cap finally giving up.
- **`test_run.py`** — the step order, queue before novels (D1), held-back novels, quiet runs still delivering leftovers, site health recording, title sync, rules problems, fatal send errors flipping the log and exiting 1, no token in output, a failed log insert stopping everything, the admin step's place and isolation.
- **`test_telegram_messenger.py`** — see [[telegramMessenger]].
- **`test_preview_run.py`** — the preview tool reports sites, the chapters that would be queued with who is told now or later, flags first-sight novels and big jumps, honours the site filter, and **fails if any write or Telegram function is called**.
- **`test_site_isolation.py`**, **`test_mgeko.py`**, **`test_fetching.py`**, **`test_novel_links.py`**, **`test_ratelimiter.py`**, **`test_health.py`** — as their names say; `test_novel_links.py` includes the two slug tiers with the real hive and vortex slugs, and the title helpers.
- **`test_bot_database.py`** — `create_user` (placeholder email, orphan cleanup, races), status and delete, website recreation, `subscribe_user_to_novel` (new/existing novel, no `latest_chap` in the insert, already subscribed, cap, races), list, remove and status change with ownership filters, and the new functions: novel summary, website domain, tiers, alert timing.
- **`test_bot.py`** — helpers, every view, `process_add` for every outcome including the tier question, `/start` wording, `/list` hub → site → novel → timing, `/settings`, every callback including foreign, malformed and forged ids, escaping of hostile text through all templates, the 64-byte callback limit, the rate gate, the error handler, application wiring including HTML mode, logging not leaking the bot token, and the hosting hooks.

## Manual smoke tests
Skipped unless `RUN_SMOKE_TESTS=1` is set in the **shell**:
```
RUN_SMOKE_TESTS=1 python -m pytest tests/test_smokeTest.py tests/test_live_database_smoke.py -v -s
```
- **`test_smokeTest.py`** sends real messages to your own chat (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` in `.env`): the alert layout (badges, `&` and `<` in a title, a title guessed from a slug), a 120-row message that should arrive as numbered chunks with only the first making a sound, and a sample admin alert and digest. Then check Telegram by eye.
- **`test_live_database_smoke.py`** reads and writes your real Supabase project (needs `SMOKE_USER_ID` and `SMOKE_NOVEL_ID` for the write tests) and undoes everything: maintenance and queue numbers, the `site_health` join, subscribers, an `app_state` round trip, a queue insert twice (the second ignored), and a novel write that puts `updated_at` back.
- **The SQL functions** have their own test script, `docs/migrations/001_v1_burnin_tests.sql`, run block by block in the Supabase SQL editor (56 checks, including a two-tab test that two workers never claim the same row). It passed on the real project before the first live run.

## Bugs the tests and reviews found
- `compilation_fnct` skipped every real link until slashes were stripped before splitting.
- `update_novels_table` wrote each novel three times (missing `break`).
- `retry()` never retried `fetch_html` because it swallowed its own errors.
- A site without `segment_count` in its rules could crash the whole run.
- Notifications went out for novels whose DB write had failed.
- A Telegram outage could mark alerts dead within one run: `dispatch_queue` re-claimed rows it had just given back, and every claim counts an attempt (found by the owner's outage simulation; fixed by stopping the loop after a pass with failures, and the SQL cap raised from 5 to 24).
- A null `latest_chap` (a novel created by the bot before its first scrape) would have crashed `float(None)` and silenced that whole site every run; fixed with a `not null default -1` column.
- `httpx` logged the Telegram bot token inside every request URL at INFO level (spotted in the first live run); the bot now keeps those loggers quiet and a test pins it.
- `pytest.ini` config in `tests/` was silently ignored.
- Found while building the burn-in: hive and vortex link `…/chapter-425#comment-18624`, which put the comments fragment into chapter URLs; kunmanga's single title selector matched only 16 of 45 novels (three layouts); `title_for` truncated titles before the messenger could add its ellipsis; `rpc()` needs its params argument in the installed supabase-py (the lenient fake had hidden it); the spec's admin-id parser would crash on `--5`; and a failed subscriber read would have looked like "nobody reads this novel" and silently advanced the novel (now `failed_novel_ids`).

## Not built yet
- Tests for the not-yet-implemented MangaDex `json` mode.
- Automated tests against the real Telegram or Supabase services beyond the two manual smoke tests and the SQL script.

## Related notes
[[main]] · [[parsing]] · [[scheduling]] · [[admin]] · [[fetching]] · [[telegramMessenger]] · [[database]] · [[bot]] · [[novel_links]] · [[ratelimiter]]
