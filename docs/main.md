# main.py — ScrollPing Orchestrator

**Status:** The full pipeline works end to end for 5 of 6 sites (asurascans, hivetoons, kunmanga, vortexscans, mgeko): scrape → compare → update DB → notify subscribers → close the log row. mangadex is skipped on purpose until its `json` mode is implemented (see [[parsing]]). Covered by [[pytest]] (`tests/test_main.py`, `tests/test_mgeko.py`, `tests/test_run.py`, `tests/test_site_isolation.py`). Users manage their subscriptions through the separate Telegram command handler, see [[bot]]; `main.py` itself only scrapes and sends.

## Purpose
Runs one full scrape cycle: fetch tracked websites → scrape each for new chapters → compare against the DB → persist updates → find subscribers → send Telegram alerts → mark the run successful.

## `run()` — execution flow
1. [[database]].`insert_scraper_logs()` → `(status, row_id)`. If `status` is `False`: print a message and `sys.exit(1)` — nothing else runs.
2. [[database]].`fetch_websites()` → `site_id` and `web_url` lists.
3. Load [[site_parse_rules.json]] **once** (`site_parse_rules`).
4. `finding_updated_novels(site_id, web_url, site_parse_rules)` → `updated_novel_list`.
5. [[database]].`update_novels_table(updated_novel_list)` → list of novel ids whose write failed.
6. **Drop the failed ids from `updated_novel_list`.** A novel whose DB write failed is not notified now. Its `latest_chap` was never saved, so the next run finds the same chapter again and notifies then. This prevents both stale data and duplicate alerts.
7. Collect `novel_id_list`, then [[database]].`fetch_subscribers(novel_id_list)` (reading subscriptions of **active** users only; paused users are skipped).
8. `group_subscribers(subscribers_list, updated_novel_list)` → `user_tele_dict`.
9. [[telegramMessenger]].`send_all(user_tele_dict)` inside a `try`, then `print_send_summary`.
10. [[database]].`update_scraper_logs(row_id)` flips the log to `success`.
11. If a fatal send problem happened, `sys.exit(1)` (after step 10).

### Why sending sits where it does
- Sending is **after** the DB update so users are never told about data the DB doesn't have yet.
- The log flip is **after** sending, but a send problem can never stop the flip: the `try/except` records a `notify_failed` flag and the flip runs regardless. The flag only decides the exit code at the very end.
- `InvalidToken`, a missing token (`RuntimeError`) and any unexpected exception from the messenger each print a message, set `notify_failed`, and end with exit code 1 so the GitHub Actions run shows red. The `InvalidToken` branch prints a fixed message on purpose — the library's own message contains the token.
- Partial send failures (blocked users, bad requests, exhausted retries) do **not** change the exit code; they are counted in the summary.
- `scraper_logs.run_status` therefore means "scrape and DB work finished", not "every notification was delivered".

## Functions

**`finding_updated_novels(site_id_list, web_url_list, site_parse_rules)`**
Loops sites one at a time and flattens the results. Each site runs inside `try/except Exception`: an unexpected error prints the site id, URL and full traceback, then the loop continues. This is the per-site isolation promised in the README. `KeyboardInterrupt` is not swallowed.

**`process_website(site_id, web_url, site_parse_rules)`**
Per-site pipeline:
1. Look up `site_parse_rules.get(web_url)`. No rules, or a `mode` not in `SUPPORTED_MODES` → print a skip message and `return []` **before any network call**.
2. [[fetching]].`fetch_html` through `retry()` (3 attempts).
3. Parse, by `rules["mode"]`: `html` → [[parsing]].`parse_html` + `compilation_fnct`; `html_li_block` → [[parsing]].`parse_html_li_block` + `compilation_fnct_li_block`.
4. [[database]].`fetch_novels` through `retry()`.
5. Build `fetched_novels`: a dict keyed by novel name (the slug) with the other columns as values.
6. Run the compile function for the mode with `(parsing_data, fetched_novels, site_rules)`.
7. Print every skipped item (`site_id: web_url: link`) and return the updated rows.
Fails soft: it returns `[]` at any stage that yields nothing.

`SUPPORTED_MODES = ("html", "html_li_block")` lives at the top of `main.py`. Adding the `json` mode (MangaDex) later starts with adding it there, plus a parse and compile pair and a branch in `process_website`.

**`compilation_fnct(parsing_data, fetched_novels, rules)`**
Pure: no globals, no file I/O, no DB or network. Takes only one site's rule dict. For each scraped href it:
- splits the path and checks the segment count against `rules["segment_count"]`;
- runs `chapter_regex` on the last two segments and reads the chapter with `group(1)` as a `float` (so `12.5` works);
- takes the slug from segment 1, stripping a hash suffix when `slug_hash_strip_regex` is set (asurascans), and writes the stripped slug back into the path;
- skips links whose slug isn't in `fetched_novels`;
- keeps the highest chapter per novel, building the absolute `chapter_url` with `urljoin(rules["base_url"], path)`.
After the loop, it hands the comparison to `collect_updated_rows` and returns `(updated_rows, skipped)`. A row is `{"id", "novel_name", "latest_chap", "chapter_url"}`.
`fetched_novels` is mutated in place; that's fine because it is a throwaway dict per site, and tests build a fresh copy each time.

**`collect_updated_rows(fetched_novels, original_latest)`**
Shared tail of every compile function. Compares each novel's `latest_chap` with the snapshot taken before the scan and returns the rows that moved forward. Pure.

**`compilation_fnct_li_block(parsing_data, fetched_novels, rules)`**
The `html_li_block` (mgeko) counterpart, same signature and return shape. `parsing_data` is a list of `{"identity", "chapter_text"}` dicts from `parse_html_li_block`:
- the slug is the last path segment of the identity href (`/manga/the-archmages-restaurant/` → `the-archmages-restaurant`);
- the chapter token comes from `chapter_regex` run on the chapter text (`Chapter 26-3-eng-li` → `26-3`) and is stored as a float with the dash turned into a dot (`26.3`);
- the raw token is kept for the reader URL, because the site writes sub-chapters with a dash: `rules["chapter_url_template"]` (`/reader/en/{slug}-chapter-{chapter}-eng-li/`) is joined to `base_url`;
- items with a missing or malformed href/text, or a slug that isn't tracked, go to `skipped`; if a novel appears twice the highest chapter wins.
Edge case accepted for V1: a dash sub-chapter of 10 or more (`26-10`) becomes `26.1`, which sorts below `26.3`.

**`group_subscribers(subscribers_list, updated_novel_list)`**
Pure. Turns the rows from `fetch_subscribers` into `{telegram_id: [novel rows]}`. Rows with no usable user (`users` missing/`None`, empty or null `telegram_user_id`) or whose `novel_id` isn't in today's updates are skipped. It does not dedupe, because `user_subscriptions` has a UNIQUE index on `(user_id, novel_id)`.

**`print_send_summary(summary)`**
Prints counts only: `telegram: sent=… blocked=… bad_request=… failed=…`. Never chat ids or message text, because the repo is public and so are its Actions logs.

**`retry(func, max_attempts, *args)`**
Generic wrapper: calls `func`, and on any exception sleeps 5 seconds and tries again, up to `max_attempts`. Returns `[]` after the last failure. It can only retry something that raises — which is why [[fetching]].`fetch_html` raises on temporary failures. The `database.fetch_*` functions retry internally and never raise, so wrapping them is a harmless no-op.

## Known gaps / deferred on purpose
- **mangadex** is skipped each run (mode not supported yet). It stays closed until V1 is finalized.
- **`skipped` links** are only printed per site. No table yet (V2: a log table with automatic cleanup).
- **Slugs are shown as titles** in notifications. A display-title column is V2.
- **Failed sends are not retried in a later run.** The novels table is already updated, so a user who exhausted in-run retries misses that alert. V2 idea: a notified flag or table for at-least-once delivery.
- **Per-site failures leave no history.** A site that fails every run is only visible in the logs. V2: separate pass/fail tracking, possibly in a second free-tier Supabase project dedicated to logs and errors.
- **`fetch_subscribers` is called even when nothing was updated** (and `send_all({})` still does one `get_me()` call as a token health check). Skipping the subscriber query when `novel_id_list` is empty is a cheap cleanup.
- `update_scraper_logs`'s return value is ignored by `run()`.
- **New novels start at `latest_chap = -1`.** The bot creates novels without a chapter, and the column defaults to `-1` (`not null`), so the compile functions need no null handling: `-1` is below every real chapter, including chapter 0. The first scrape that sees the novel on its listing page fills in the real number and notifies the subscriber. A novel that is real but quiet stays at `-1` until it next appears on the site's latest-updates page.

## Design history worth remembering
- `compilation_fnct`'s signature went through three shapes: a hidden global `site_parse_rules` → opening the JSON on every call → today's version, where the file is loaded once in `run()` and one site's rule dict is passed down. Chosen to keep it testable in isolation.
- An early `main.py` had `process_website` looping over all sites and reading an undeclared global — it would have crashed with `NameError`. Fixed by separating the orchestrator from the per-site step.
- Bugs found in review and fixed (Nov 2026): `update_novels_table` wrote every novel three times (missing `break`); `retry()` never retried `fetch_html` (it swallowed its own errors); a site without `segment_count` in its rules crashed the run; notifications went out for novels whose DB write had failed.

## Related notes
- [[database]]
- [[fetching]]
- [[parsing]]
- [[telegramMessenger]]
- [[bot]]
- [[schema]]
- [[pytest]]
- [[site_parse_rules.json]]
