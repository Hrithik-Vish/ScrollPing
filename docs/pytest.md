# Testing (pytest) and CI

**State:** 146 tests pass, 2 are skipped by design (manual Telegram smoke tests). Runs in about 2 seconds, needs no real credentials, never touches the network.

## Layout
```
pytest.ini                      # [pytest] pythonpath = . / testpaths = tests
tests/
├── conftest.py                 # dummy env vars so `import main` works
├── fixtures/real_links.json    # hrefs captured from the live sites via parse_html
├── test_main.py                # retry, compilation_fnct, process_website, group_subscribers, real-link tests
├── test_fetching.py            # fetch_html + retry working together
├── test_site_isolation.py      # mode guard and per-site isolation
├── test_run.py                 # how run() wires the Telegram step in
├── test_telegram_messenger.py  # the messenger module
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
- **No real waiting.** `main.time.sleep` and `asyncio.sleep` are patched; tests assert on the sleeps instead.
- **Async code is driven with `asyncio.run`** inside plain tests — no extra plugin.
- **Real data over invented data.** `real_links.json` plus the real `site_parse_rules.json` feed `test_real_*` tests; one guard test checks every `base_url` is host-only and matches its site.
- **Check that tests can fail.** New tests were validated by putting the old buggy code back and watching them fail.

## What is covered
- **`test_main.py`** — `retry`; `compilation_fnct` (updates, ordering, fractional chapters, skipped links, string chapters from the DB, asura hash stripping, absolute hash-free URLs, apostrophes in slugs, missing `base_url`); `process_website` failure paths; `finding_updated_novels`; real captured links per site; `group_subscribers` (batching, bad rows, no mutation, duplicates).
- **`test_fetching.py`** — success, headers and timeouts, network errors propagate, 429/5xx raise, 403/404 return `""`, and `retry` recovers after failures, gives up after 3, and doesn't repeat a 403.
- **`test_site_isolation.py`** — no rules or unsupported mode → skipped before any fetch; real mgeko/mangadex rules are skipped, the 4 HTML sites are not; one crashing site doesn't stop the others; `KeyboardInterrupt` still propagates.
- **`test_run.py`** — order (DB update → send → log flip), grouped users reach `send_all`, send problems still flip the log then exit 1, no token in output, failed log insert stops everything, summary prints counts only.
- **`test_telegram_messenger.py`** — see [[telegramMessenger]].

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

## Not built yet
- A scheduled workflow for the scraper itself (needs repository secrets for Supabase and the bot token).
- Tests for the bot command handler (`/start`, `/add`) — the handler doesn't exist yet.
- Tests for mgeko's `html_li_block` mode — they arrive with the implementation.

## Related notes
[[main]] · [[fetching]] · [[telegramMessenger]] · [[database]]
