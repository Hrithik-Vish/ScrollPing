# database.py

## Purpose
Supabase data-access layer for ScrollPing, used by two different programs:
- the **scraper** (`main.py`): retry-with-backoff loops around Supabase calls, uniform empty-list-on-failure returns, prints for run-time visibility, no exceptions escape to the caller (everything down to `update_scraper_logs`, below);
- the **Telegram command handler** ([[bot]]): the `get_user` … `set_subscription_status` functions at the end of the file. These behave differently on purpose, see "Bot-side functions".

All database access goes through functions in this file; nothing else talks to Supabase.

## Setup
- Loads `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SECRET_KEY` from `.env`; raises `ValueError` immediately if any are missing.
- Client is created with the **secret (service role) key** — bypasses RLS, since this runs as a trusted backend job, not a user-facing client. Keep it server-side only (`.env` locally, repository secrets in CI); never ship it to a browser or bot client.
- The publishable key is required by the import-time check but not used for anything yet. Either drop the check or keep it for the future frontend; note that any CI job that imports this module must provide all three variables.
- [[pytest]]: `tests/conftest.py` sets dummy values for all three so `import main` works without real credentials.

## Shared pattern
- `for attempt in range(1, max_attempts + 1):` — avoids off-by-one errors.
- `time.sleep(5)` between retry attempts.
- On final failed attempt: logs and returns a safe empty value (`[]`, or `False`/`None` where a tuple/bool is expected) — never raises.
- supabase-py's `.data` is `[]` on zero matching rows for select/insert/update alike — this is the basis for every truthy check below.

## Tables the code touches (columns used)
- `websites` — `id`, `web_domain_name` (the exact URL string, also the key in [[site_parse_rules.json]])
- `novels` — `id`, `web_id`, `novel_name` (**the slug**, e.g. `the-forgotten-field`), `latest_chap` (`numeric(7,2)`, `not null default -1`: `-1` means "not scraped yet"), `chapter_url` (absolute URL), `updated_at`; UNIQUE on `(web_id, novel_name)`
- `users` — `id` (the Supabase auth user id), `username`, `telegram_user_id` (UNIQUE), `status` (`active` / `paused`)
- `user_subscriptions` — `user_id`, `novel_id`, `reading_status` (`reading`, `completed`, `left`, `on hiatus`); UNIQUE index on `(user_id, novel_id)`, so duplicate subscriber rows cannot occur

Full SQL, RLS and cleanup jobs: [[schema]].
- `scraper_logs` — `id`, `last_run_timestamp`, `run_status` (defaults to `fail`; flipped to `success` as the last step of a run)

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
- Iterates the updated-novels list; for each novel, up to 3 attempts to update `latest_chap`, `chapter_url` and `updated_at` (UTC timestamp) by `id`. A successful `execute()` ends that novel's loop with `break`.
- Per-novel failure isolation: if one novel fails all attempts, its `id` is appended to `failToUpdate_list` and the loop moves on.
- **Returns:** `failToUpdate_list` (possibly empty). A list of failures, not a status flag. [[main|run()]] uses it to drop those novels from the notification list.
- Consumes the flat `{"id", "novel_name", "latest_chap", "chapter_url"}` rows produced by [[main|compilation_fnct]] — no reshaping.
- It does not check `.data`, so an update that matched no row counts as success (unlike `update_scraper_logs`).
- History: the first version had no `break`, so every novel was written three times per run (and stamped three `updated_at` values). Fixed Nov 2026.

### `insert_scraper_logs()`
- 3 attempts. Inserts a new `scraper_logs` row with `last_run_timestamp` (UTC).
- On success, extracts `row_id` directly from `insert_scraper_log.data[0]["id"]` — no follow-up SELECT needed.
- If the insert reports success but returns no `.data`, this is explicitly treated as an error (`raise ValueError(...)`) rather than silently continuing — routes into the same except block as any other failure.
- **Returns:** `(status, row_id)` — `(True, id)` on success and `(False, None)` when all attempts fail, so callers can always unpack two values. [[main|run()]] prints a message and exits with code 1 on `False`.

### `update_scraper_logs(row_id)`
- 3 attempts. Updates the `scraper_logs` row at `row_id`, setting `run_status = "success"`.
- Checks `.data` truthiness before setting `status = True` — matches the pattern used elsewhere, since a no-op update (row_id not found) still returns without raising, and `.data` is the only signal that a row was actually touched.
- Prints `no row matched id: …` when nothing was updated.
- **Returns:** `status` (bool). [[main|run()]] currently ignores it.

### `fetch_subscribers(novel_id_list)`
- 5 attempts (highest of any function) — by this point in the run, all the expensive upstream work (scraping, comparing, finding updates) is already done. If this fails, the entire run's output is wasted, so it's worth retrying hard rather than giving up early.
- Nested select join: `novel_id, users!inner(telegram_user_id)` from `user_subscriptions`, filtered with `.in_("novel_id", novel_id_list)`, `.eq("reading_status", "reading")` and `.eq("users.status", "active")`. The `!inner` join plus the embedded filter is what makes **paused users receive nothing**; the returned shape is unchanged.
- **Returns (example):** `[{"novel_id": 1, "users": {"telegram_user_id": "123456"}}, ...]` — `users` is a nested dict per row (not flattened). The id may come back as a string or an int; both work as chat ids.
- Bulk operation by design (vs. per-site functions above) — see Key learnings.
- Feeds `group_subscribers` in [[main|main.py]], which groups the rows by Telegram id for [[telegramMessenger]].
- It is called even when no novel was updated (empty `novel_id_list`); a guard in `run()` would save one query on quiet runs.

## Bot-side functions (used by [[bot]])
Unlike the scraper functions above, these **do not retry and do not hide failures**: a database error is raised, and the bot's error handler tells the user something went wrong. This matters because "user not found" (`None`) and "the database is down" (an exception) must never look the same, or an outage would make the bot try to register people who already exist.

- **`get_user(telegram_user_id)`** → the `users` row (`id, username, telegram_user_id, status`) or `None`.
- **`create_user(telegram_user_id, username)`** → creates the Supabase **auth user first** (`users.id` references `auth.users`) with a placeholder email `tg<id>@scrollping.invalid` and `email_confirm: true`, then inserts the `users` row with that id. Supabase refuses to create an auth user without an email or phone, and the bot has neither; no password is set and the `.invalid` domain can never receive mail. If the `users` insert fails, the auth user is deleted again so no orphan is left; a unique violation (two simultaneous registrations) returns the existing row.
- **`set_user_status(user_id, status)`** → `active` / `paused` (anything else raises `ValueError` without a query). Returns whether a row was updated.
- **`delete_user_account(user_id)`** → deletes the auth user; `auth.users → users → user_subscriptions` all cascade, so nothing else needs deleting. Orphaned novels are removed by the weekly cleanup job.
- **`get_or_create_website(web_domain_name)`** → id. The key comes from `site_parse_rules.json` (trusted config, never user input). The weekly job deletes websites without novels, so the row can be missing even for a supported site; it is simply recreated.
- **`subscribe_user_to_novel(user_id, web_domain_name, slug, max_subscriptions)`** → `"added"`, `"already_subscribed"` or `"limit_reached"`. Order: count the user's subscriptions → get/create the website → look the novel up (without creating it) → already subscribed? → at the cap? → create the novel → insert the subscription. Checking the cap before creating anything means a refused add leaves no orphan novel, and an already-followed novel is still reported as followed when the user is at the cap. New novels are inserted **without `latest_chap`** (sending `None` would write a real null and skip the `-1` column default) and with `updated_at = now()`, so the 97-day cleanup can eventually remove a slug that never turns out to be real. Unique violations (races) are treated as success.
- **`list_subscriptions(user_id)`** → flat rows `{id, reading_status, novel_name, latest_chap, web_domain_name}`, sorted by name.
- **`remove_subscription(user_id, subscription_id)`** and **`set_subscription_status(user_id, subscription_id, status)`** → bool. Both filter on the subscription id **and** the user id. The secret key bypasses RLS, so this filter is the only thing stopping one user from touching another user's row. `set_subscription_status` rejects unknown statuses with `ValueError`.

Known limitation: the 50-novel cap is checked in code, not by a database trigger, so two simultaneous adds could slip past it. The bot handles one update at a time, so this only matters if it is ever scaled out.

## Known quirks / open items
- `update_novels_table` doesn't verify that a row was matched (see above).
- `fetch_novels`, `fetch_websites` and the others retry internally and never raise; [[main|process_website]] wraps `fetch_novels` in `retry()` anyway, which is redundant but harmless.
- Fixed earlier: a dead `return []` after a `raise` in `insert_scraper_logs`; the mixed return types of `insert_scraper_logs`.
- V2 idea: log failures (skipped links, per-site fetch failures, DB errors) to a separate free-tier Supabase project so detailed logs don't compete with app data.

## Key learnings & principles
- **Per-site vs. bulk failure isolation:** `fetch_novels`/`update_novels_table` operate per-item because a single failure is tolerable and retried next run. `fetch_subscribers` is bulk because a missed notification can't be "retried later" — the moment is gone.
- **Retry attempt counts are deliberately tiered:** 2 (early, cheap-to-skip fetch) → 3 (default) → 5 (last step before the run's entire output would be wasted). Not arbitrary — reflects how much sunk work would be lost by giving up at that point.
- **Subscriber vs. novel cardinality:** subscriber count is structurally smaller/rarer than novel count in isolation — one subscriber can follow many novels, but a novel with zero subscribers is a normal, valid state. This is part of why a failed subscriber fetch is treated as high-stakes (see `fetch_subscribers` above), not because subscribers are numerous.
- **Retry loop convention:** `range(1, max_attempts + 1)` everywhere, `time.sleep(5)` between attempts — kept consistent across the scraper functions for predictability.
- **Raise vs. return empty:** a batch job can shrug off a failed read and try again next run; an interactive bot must tell the person something went wrong, and must be able to tell "nothing there" from "couldn't look".