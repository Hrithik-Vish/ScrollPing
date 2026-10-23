# database.py

## Purpose
Supabase data-access layer for ScrollPing. Every function follows the same shape: retry-with-backoff loop around a Supabase call, uniform empty-list-on-failure return, and prints for run-time visibility (no exceptions escape to the caller).

## Setup
- Loads `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SECRET_KEY` from `.env`; raises `ValueError` immediately if any are missing.
- Client is created with the **secret (service role) key** — bypasses RLS, since this runs as a trusted backend job, not a user-facing client.

## Shared pattern
- `for attempt in range(1, max_attempts + 1):` — avoids off-by-one errors.
- `time.sleep(5)` between retry attempts.
- On final failed attempt: logs and returns a safe empty value (`[]`, or `False`/`None` where a tuple/bool is expected) — never raises.
- supabase-py's `.data` is `[]` on zero matching rows for select/insert/update alike — this is the basis for every truthy check below.

## Functions

### `fetch_websites()`
- 3 attempts. Selects `id, web_domain_name` from `websites`.
- Returns `[]` on empty result *or* all-retries-failed — same return value for both cases, since callers don't need to distinguish "no websites" from "couldn't fetch."
- **Returns (example):** `[{"id": 1, "web_domain_name": "asurascans.com"}, ...]`

### `fetch_novels(site_id)`
- 2 attempts. Selects `*` from `novels` where `web_id = site_id`.
- Same `[]`-on-empty-or-failure pattern as `fetch_websites`.
- **Returns (example):** `[{"id": 1, "web_id": 2, "novel_name": "...", "latest_chap": 66.5, "chapter_url": "..."}, ...]`
- `latest_chap`/`chapter_url` here are exactly the fields `[[main|compilation_fnct]]` compares against and overwrites when it finds an update.
- Low attempt count is deliberate: this happens early in the per-site loop, before any real work has been invested. If it fails, cheaper to skip this site for the run and let the next cron job retry than to burn time retrying a potentially large fetch.

### `update_novels_table(updated_novel_list)`
- Iterates the updated-novels list; for each novel, 3 retry attempts to update `latest_chap`, `chapter_url`, and `updated_at` (UTC timestamp) by `id`.
- Per-novel failure isolation: if one novel's update fails all attempts, its `id` is appended to `failToUpdate_list` and the loop moves on to the next novel — one bad row doesn't block the batch.
- **Returns:** `failToUpdate_list` (possibly empty) — this is a list of failures, not a status flag, unlike the other functions.
- Consumes the flat `{"id", "novel_name", "latest_chap", "chapter_url"}` row shape produced by [[main|compilation_fnct]] — no reshaping needed on this end.

### `insert_scraper_logs()`
- 3 attempts. Inserts a new `scraper_logs` row with `last_run_timestamp` (UTC).
- On success, extracts `row_id` directly from `insert_scraper_log.data[0]["id"]` — no follow-up SELECT needed.
- If the insert reports success but returns no `.data`, this is explicitly treated as an error (`raise ValueError(...)`) rather than silently continuing — routes into the same except block as any other failure.
- **Returns:** `(status, row_id)` tuple — `(True, id)` on success, or `[]` if all attempts fail (note: the failure path's return type differs from the success path's; see Known quirks).

### `update_scraper_logs(row_id)`
- 3 attempts. Updates the `scraper_logs` row at `row_id`, setting `run_status = "success"`.
- Checks `.data` truthiness before setting `status = True` — matches the pattern used elsewhere, since a no-op update (row_id not found) still returns without raising, and `.data` is the only signal that a row was actually touched.
- **Returns:** `status` (bool only, no row_id needed since the row already exists).

### `fetch_subscribers(novel_id_list)`
- 5 attempts (highest of any function) — by this point in the run, all the expensive upstream work (scraping, comparing, finding updates) is already done. If this fails, the entire run's output is wasted, so it's worth retrying hard rather than giving up early.
- Nested select join: `novel_id, users(telegram_user_id)` from `user_subscriptions`, filtered with `.in_("novel_id", novel_id_list)` and `.eq("reading_status", "reading")`.
- **Returns (example):** `[{"novel_id": 1, "users": {"telegram_user_id": 123456}}, ...]`
- Bulk operation by design (vs. per-site functions above) — see Key learnings.
- Feeds the not-yet-built Telegram notification step in [[main|main.py]] — grouping these rows by `novel_id` is still an open task.

## Known quirks / things fixed this session
- `insert_scraper_logs`'s failure path returns `[]` (falsy list) while its success path returns a `(status, row_id)` tuple — callers must handle both shapes. Not yet reconciled to a single return type.
- A dead `return []` that sat after the `raise ValueError(...)` inside `insert_scraper_logs`'s try block was removed — unreachable code, since the raise already routes into the except.

## Key learnings & principles
- **Per-site vs. bulk failure isolation:** `fetch_novels`/`update_novels_table` operate per-item because a single failure is tolerable and retried next run. `fetch_subscribers` is bulk because a missed notification can't be "retried later" — the moment is gone.
- **Retry attempt counts are deliberately tiered:** 2 (early, cheap-to-skip fetch) → 3 (default) → 5 (last step before the run's entire output would be wasted). Not arbitrary — reflects how much sunk work would be lost by giving up at that point.
- **Subscriber vs. novel cardinality:** subscriber count is structurally smaller/rarer than novel count in isolation — one subscriber can follow many novels, but a novel with zero subscribers is a normal, valid state. This is part of why a failed subscriber fetch is treated as high-stakes (see `fetch_subscribers` above), not because subscribers are numerous.
- **Retry loop convention:** `range(1, max_attempts + 1)` everywhere, `time.sleep(5)` between attempts — kept consistent across all six functions for predictability.
