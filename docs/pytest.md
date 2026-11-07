# Testing (pytest) and CI

**State:** 388 tests pass, 2 are skipped by design (manual Telegram smoke tests). Runs in a few seconds, needs no real credentials, never touches the network.

## Layout
```
pytest.ini                      # [pytest] pythonpath = . / testpaths = tests
tests/
├── conftest.py                 # dummy env vars so `import main` works
├── fixtures/real_links.json    # hrefs captured from the live sites via parse_html
├── test_main.py                # retry, compilation_fnct, process_website, group_subscribers, real-link tests
├── test_mgeko.py               # parse_html_li_block, compilation_fnct_li_block, mgeko through process_website
├── test_fetching.py            # fetch_html + retry working together
├── test_site_isolation.py      # mode guard and per-site isolation
├── test_run.py                 # how run() wires the Telegram step in
├── test_telegram_messenger.py  # the messenger module
├── test_novel_links.py         # pasted link -> (site, slug), against the real rules file
├── test_ratelimiter.py         # token bucket with a fake clock
├── test_bot_database.py        # bot-side database functions against a scripted fake Supabase
├── test_bot.py                 # bot helpers, every handler, lifecycle and HEALTH_PORT wiring
├── test_health.py              # the /health endpoint, with real HTTP requests on a free local port
└── test_smokeTest.py           # MANUAL, sends real messages (skipped by default)
```
`pytest.ini` lives in the **root** on purpose. A `pytest.ini` inside `tests/` is ignored when the root has one, which is how `pythonpath = .` was once silently lost (plain `pytest` failed with `No module named 'src'` while `python -m pytest` happened to work).

## Running
- **VS Code (Docker):** tasks in `.vscode/tasks.json` run inside the `scrollping-dev` container (project at `/app`): run current file, pytest, verbose, stop at first failure, last failed, current file, by keyword. Shortcuts: Ctrl+Alt+1 run file, Ctrl+Alt+2 pytest, Ctrl+Alt+3 onward the variants.
- **Terminal:** `python -m pytest` (or just `pytest`).
- **CI:** `.github/workflows/tests.yml` runs `python -m pytest -q` on every push and pull request (Python 3.12, pip cache, no secrets, `permissions: contents: read`).

## Conventions
- **Everything external is faked.** `conftest.py` sets dummy `SUPABASE_*` values because `database.py` raises at import without them, plus `PTB_TIMEDELTA=1` so python-telegram-bot's `RetryAfter.retry_after` is a `timedelta` (no deprecation warnings; the code handles both forms).
- **Fresh data per test.** `compilation_fnct` mutates `fetched_novels`, so fixtures return new dicts each time.
- **Fakes, not mocks of internals:** a `FakeGet` for `requests.get`, a `FakeBot` for `telegram.Bot` that replays scripted outcomes, recorders for the database functions in `test_run.py`.
- **A scripted fake Supabase** (`test_bot_database.py`): a queue of responses (rows, or an exception to raise) and a log of every call chain, so tests assert on exactly what was sent (which filters, which payload keys) without a database.
- **Fake Telegram objects** (`test_bot.py`): small `FakeMessage` / `FakeCallbackQuery` classes and a `FakeDB` standing in for `database`, driven with `asyncio.run`. Handlers are tested end to end through those, including garbage and foreign callback data.
- **No real waiting.** `main.time.sleep` and `asyncio.sleep` are patched; tests assert on the sleeps instead.
- **Async code is driven with `asyncio.run`** inside plain tests — no extra plugin.
- **Real data over invented data.** `real_links.json` plus the real `site_parse_rules.json` feed `test_real_*` tests; one guard test checks every `base_url` is host-only and matches its site.
- **Check that tests can fail.** New tests were validated by putting the old buggy code back and watching them fail. For the bot, the ownership filter on `remove_subscription` and the paused-user filter on `fetch_subscribers` were deliberately removed to confirm that tests go red.

## What is covered
- **`test_main.py`** — `retry`; `compilation_fnct` (updates, ordering, fractional chapters, skipped links, string chapters from the DB, asura hash stripping, absolute hash-free URLs, apostrophes in slugs, missing `base_url`); `process_website` failure paths; `finding_updated_novels`; real captured links per site; `group_subscribers` (batching, bad rows, no mutation, duplicates).
- **`test_fetching.py`** — success, headers and timeouts, network errors propagate, 429/5xx raise, 403/404 return `""`, and `retry` recovers after failures, gives up after 3, and doesn't repeat a 403.
- **`test_site_isolation.py`** — no rules or unsupported mode → skipped before any fetch; the real mangadex rules (mode `json`) are skipped, the 4 HTML sites and mgeko are not; one crashing site doesn't stop the others; `KeyboardInterrupt` still propagates.
- **`test_run.py`** — order (DB update → send → log flip), grouped users reach `send_all`, send problems still flip the log then exit 1, no token in output, failed log insert stops everything, summary prints counts only.
- **`test_telegram_messenger.py`** — see [[telegramMessenger]].
- **`test_mgeko.py`** — parser (hrefs and text, order, blocks missing a part, empty pages); compiler (new chapter with reader URL, `26-3` → `26.3` with the dash kept in the URL, no update when not newer, highest wins for duplicates, untracked slugs skipped, malformed items, absolute hrefs); mgeko through `process_website`.
- **`test_novel_links.py`** — supported links per site, harmless variations (no scheme, `www`, case, port, query), rejections (lookalike hosts, `user@host` tricks, wrong schemes, bad slugs, too long), the asura hash never reaching the stored slug, hash stripping only for sites that have the rule and matching the scraper's.
- **`test_ratelimiter.py`** — burst, warn-once-then-drop, refill, capacity cap, separate users, purge of idle users.
- **`test_bot_database.py`** — `fetch_subscribers` filters paused users; `create_user` (placeholder email, orphan cleanup, races); status and delete; website recreation; `subscribe_user_to_novel` (new/existing novel, no `latest_chap` in the insert, already subscribed, cap, races); list, remove and status change with ownership filters.
- **`test_health.py`** — 503 until the bot is running, 200 after, HEAD, query strings, unknown paths and POST refused, nothing leaked in headers or logs.
- **`test_bot.py`** — helpers (`chapter_text` with `-1`, formatting), `process_add` for every outcome, `/start` for new/existing/paused users, `/add` flows, pickers, every callback including foreign and garbage ids, `/stop`, the rate gate (private chats only, warn once, buttons), the error handler, application wiring, logging not leaking the bot token, and the hosting hooks (`post_init`/`post_shutdown`, `HEALTH_PORT`/`PORT` parsing and precedence, `main()` starting the health server only when asked).

## Manual smoke test
`tests/test_smokeTest.py` sends real messages to your own chat. It needs `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` and is skipped unless `RUN_SMOKE_TESTS=1` is set in the shell:
```
docker exec -e RUN_SMOKE_TESTS=1 -w /app scrollping-dev python -m pytest tests/test_smokeTest.py -v -s
```
`-k hero` runs the formatting test (apostrophe slug, `&` in the URL, fractional and whole chapters); `-k long` runs the 120-row test that should arrive as numbered chunks. Then check Telegram by eye: links tappable, no preview cards, no `.0`, chunks in order.

## Bugs the tests and reviews found
- `compilation_fnct` skipped every real link until slashes were stripped before splitting.
- `update_novels_table` wrote each novel three times (missing `break`).
- `retry()` never retried `fetch_html` because it swallowed its own errors.
- A site without `segment_count` in its rules could crash the whole run.
- Notifications went out for novels whose DB write had failed.
- `pytest.ini` config in `tests/` was silently ignored.
- A null `latest_chap` (a novel created by the bot before its first scrape) would have crashed `float(None)` and silenced that whole site every run; fixed with a `not null default -1` column.
- `httpx` logged the Telegram bot token inside every request URL at INFO level (spotted in the first live run); the bot now keeps those loggers quiet and a test pins it.
- A hash-like slug test case was wrong on the first try (`ab39de10` without the dash isn't stripped), which pinned down exactly what `slug_hash_strip_regex` matches.

## Not built yet
- A scheduled workflow for the scraper itself (needs repository secrets for Supabase and the bot token).
- Tests for the not-yet-implemented MangaDex `json` mode.
- Anything against the real Telegram or Supabase services besides the manual smoke test; the bot was checked live by hand.

## Related notes
[[main]] · [[fetching]] · [[telegramMessenger]] · [[database]] · [[bot]] · [[novel_links]] · [[ratelimiter]]