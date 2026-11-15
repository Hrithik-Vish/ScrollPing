# database.py

## Purpose
Supabase data-access layer for ScrollPing, used by two different programs:
- the **scraper** (`main.py`): everything down to `update_scraper_logs`, below. Transient failures are retried, and after the last attempt a function returns an explicit "it failed" value (empty list, `0`, `None`, `ok=False`) instead of raising, so one database hiccup cannot stop a whole run. Prints are counts-only.
- the **Telegram command handler** ([[bot]]): the `get_user` … `set_alert_timing` functions at the end of the file. These behave differently on purpose, see "Bot-side functions".

All database access goes through functions in this file; nothing else talks to Supabase.

## Setup
- Loads `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SECRET_KEY` from `.env`; raises `ValueError` immediately if any are missing.
- Client is created with the **secret (service role) key**, which bypasses RLS, since this runs as a trusted backend. Keep it server-side only (`.env` locally, repository secrets in CI, Render environment variables for the bot).
- The publishable key is required by the import-time check but not used for anything yet. Any CI job that imports this module must provide all three variables.
- [[pytest]]: `tests/conftest.py` sets dummy values for all three so `import main` works without real credentials.

## Tables the code touches (columns used)
- `websites`: `id`, `web_domain_name` (the exact URL string, also the key in [[site_parse_rules.json]])
- `novels`: `id`, `web_id`, `novel_name` (**the slug**), `display_title` (scraped title, nullable), `latest_chap` (`numeric(7,2)`, `not null default -1`: `-1` means "not scraped yet"), `chapter_url`, `updated_at`; UNIQUE on `(web_id, novel_name)`
- `users`: `id`, `username`, `telegram_user_id` (UNIQUE), `status` (`active` / `paused`)
- `user_subscriptions`: `id`, `user_id`, `novel_id`, `reading_status`, `alert_timing` (`early` / `when_free` / null); UNIQUE on `(user_id, novel_id)`
- `user_site_tiers`: `user_id`, `web_id`, `tier` (`standard` / `member`); UNIQUE on `(user_id, web_id)`
- `pending_notifications`: the delivery queue; UNIQUE on `(user_id, novel_id, chapter)`
- `site_health`, `app_state`, `scraper_logs`

Full SQL, the SQL functions, RLS and cleanup jobs: [[schema]].

## Conventions of the scraper functions
- `_retry(label, call, attempts)` runs a call and, on an exception, waits `RETRY_SLEEP_SECONDS` (5) and tries again, re-raising after the last attempt; the public functions catch that and return their failure value. Messages print the error class and a short text, never a row.
- `_chunks`: ids go to `.in_()` filters 100 at a time and to RPC calls 200 at a time, so URLs and bodies stay small.
- `_rows(response)` / `_scalar(response)`: tolerate the shapes supabase-py returns (`None`, a list, a bare number for scalar functions).
- **`rpc(name, params)` needs its `params` argument** in the installed supabase-py (no default), so parameterless functions are called with `{}`. A fake client that allowed a missing argument once hid a bug here; the test fake is now strict.
- Retry counts are tiered by how much work would be wasted by giving up: 2 for early, cheap-to-skip reads; 3 by default; 5 for the recipient read (the step before the run's output would be lost).

## Scraper functions

### Reading
- **`fetch_websites()`**: 3 attempts, `[{"id", "web_domain_name"}, ...]`; `[]` for empty or failed.
- **`fetch_novels(site_id)`**: 2 attempts, `select *` for one site (so it includes `display_title`); `[]` for empty or failed.
- **`fetch_subscribers_with_preferences(novel_id_list, web_id_by_novel=None)`** → `SubscriberFetch(recipients, tiers, failed_novel_ids)` (a `NamedTuple`; unpacks as three values and has an `.ok` property).
  - `recipients`: `[{"novel_id", "user_id", "alert_timing", "telegram_user_id"}]` for **active** users whose status for the novel is `reading`. The query is `user_subscriptions` with an inner join to `users` and the filters `reading_status = reading`, `users.status = active`.
  - `tiers`: `{(user_id, web_id): "standard" | "member"}`; a missing key means standard.
  - `failed_novel_ids`: novels whose recipients could not be read reliably. **The caller must not advance those novels,** so the chapter is detected again next run. Without this, a database failure would look the same as "nobody reads this novel", the novel would be advanced, and the alert would be gone for good. A failed tier lookup does the same for that user's novels, because an early-access alert scheduled with the wrong tier could never be corrected later.

### Writing the novels
- **`update_novels_table(rows)`**: one `update_novels_batch` RPC for the whole run (`id`, `latest_chap`, `chapter_url`, `display_title`; the SQL sets `updated_at` and keeps the old title when none is sent). If the batch fails twice, it falls back to one row at a time to isolate a bad value. Returns the **ids that were not written** (possibly empty). A failed id only means the write did not happen: the queue already has the alert.
- **`sync_novel_titles(updates)`**: `sync_novel_titles` RPC for `[{"id", "display_title"}]`. Never touches `updated_at` (D4: a title sync must not keep a dead novel alive). Fail-soft; returns the ids actually changed.

### The delivery queue
- **`insert_queue_records(records)`**: upsert in batches of 500 with `on_conflict="user_id,novel_id,chapter"` and `ignore_duplicates=True`, so re-inserting a chapter is a no-op. Every record is normalised to the same keys (a PostgREST batch needs that). Returns `{"attempted", "inserted", "failed", "failed_novel_ids"}`: the last two describe batches that could not be written, so the caller leaves those novels un-advanced.
- **`maintain_queue()`** → `{"ok", "recovered", "cancelled", "expired"}`: `maintain_notification_queue` RPC.
- **`claim_due(limit=500)`**: `claim_due_notifications` RPC, which atomically takes due rows (`FOR UPDATE SKIP LOCKED`), marks them `sending` and returns them joined with the user's Telegram id, the novel's name and title, and the site. Fail-soft: `[]` on error.
- **`ack_sent(ids)`**, **`release(ids, error_text)`**, **`mark_dead(ids, error_text)`**: the acknowledgements, chunked, each returning how many rows changed. A failed acknowledgement leaves rows `sending`, and maintenance recovers them (at-least-once delivery, accepted). Error text is whitespace-collapsed and cut to 500 characters.
- **`handle_blocked_users(telegram_user_id)`**: pauses the user and cancels their `pending` and `sending` rows. Returns `{"ok", "paused", "cancelled"}`. `/start` undoes the pause; cancelled rows are not resurrected.
- **`queue_stats()`**: last-24-hour numbers for the digest, or `None`.

### Site health and admin state
- **`record_site_result(web_id, status, error=None)`**: `record_site_result` RPC; `status` must be `ok`, `degraded` or `failed` (anything else raises `ValueError` before a call). Returns `{"consecutive_failures", "failure_alerted"}` or `None`.
- **`fetch_site_alerts()`**: sites with 3+ consecutive failures that have not been reported (joined with the site name). **`ack_site_alerts(web_ids)`**: mark them reported (call only after the alert was delivered).
- **`fetch_site_health_rows()`**: every site's health row, for the digest.
- **`get_app_state(key)`**: the stored value or `None`. **Unlike every other function here it raises** when the database cannot be read: "never sent" and "could not check" must not look the same, or a blip would make the daily digest send again. **`set_app_state(key, value)`**: upsert with `updated_at = now`, returns a bool.

### The run log
- **`insert_scraper_logs()`**: 3 attempts; inserts a `scraper_logs` row and returns `(True, row_id)` or `(False, None)`; a success without `.data` is treated as an error.
- **`update_scraper_logs(row_id)`**: 3 attempts; sets `run_status = "success"`; checks `.data` so a row that matched nothing is not reported as success.

## Bot-side functions (used by [[bot]])
Unlike the scraper functions above, these **do not retry and do not hide failures**: a database error is raised, and the bot's error handler tells the user something went wrong. "User not found" (`None`) and "the database is down" (an exception) must never look the same, or an outage would make the bot try to register people who already exist. Every write filters on the caller's own user id.

- **`get_user(telegram_user_id)`** → the `users` row or `None`.
- **`create_user(telegram_user_id, username)`** → creates the Supabase **auth user first** (`users.id` references `auth.users`) with a placeholder email `tg<id>@scrollping.invalid` and no password, then inserts the `users` row with that id. If the insert fails the auth user is deleted again; a unique violation (two simultaneous registrations) returns the existing row.
- **`set_user_status(user_id, status)`**: `active` / `paused` (anything else raises `ValueError` without a query).
- **`delete_user_account(user_id)`**: deletes the auth user; `auth.users → users → user_subscriptions` (and `user_site_tiers`, `pending_notifications`) cascade.
- **`get_or_create_website(web_domain_name)`** → id. The key comes from `site_parse_rules.json` (trusted config, never user input).
- **`subscribe_user_to_novel(user_id, web_domain_name, slug, max_subscriptions)`** → `"added"`, `"already_subscribed"` or `"limit_reached"`. Order: count the user's subscriptions → get/create the website → look the novel up (without creating it) → already subscribed? → at the cap? → create the novel → insert the subscription. Checking the cap before creating anything means a refused add leaves no orphan novel. New novels are inserted **without `latest_chap`** (sending `None` would write a real null and skip the `-1` default) and with `updated_at = now()`, so the 97-day cleanup can eventually remove a slug that never turns out to be real. Unique violations (races) are treated as success.
- **`list_subscriptions(user_id)`** → flat rows `{id, reading_status, alert_timing, novel_name, display_title, latest_chap, web_id, web_domain_name}`, sorted by name; the source of every `/list` page.
- **`remove_subscription(user_id, subscription_id)`**, **`set_subscription_status(...)`**, **`set_alert_timing(user_id, subscription_id, timing)`** (`early`, `when_free` or `None`; unknown values raise `ValueError`) → bool. All filter on the subscription id **and** the user id: the secret key bypasses RLS, so that filter is the only thing stopping one user from touching another's row.
- **`get_novel_summary(web_domain_name, slug)`** → `{id, web_id, novel_name, display_title}` or `None`; words the `/add` confirmation.
- **`get_website_domain(web_id)`** → the domain or `None`; lets a callback prove an id is a real site.
- **`get_user_tier(user_id, web_id)`** → tier or `None` (never chosen); **`list_user_tiers(user_id)`** → `{web_id: tier}`; **`set_user_tier(user_id, web_id, tier)`** upserts on `(user_id, web_id)`; unknown tiers raise `ValueError`.

Known limitation: the 50-novel cap is checked in code, not by a database trigger, so two simultaneous adds could slip past it. The bot handles one update at a time, so this only matters if it is ever scaled out.

## Key learnings & principles
- **Per-item vs. bulk isolation:** `update_novels_table` falls back to one row at a time because a single bad value must not block the rest; the recipient read reports *which novels* it could not read instead of pretending.
- **Raise vs. return empty:** a batch job can shrug off a failed read and try again next run; an interactive bot must tell the person something went wrong, and must be able to tell "nothing there" from "couldn't look". `get_app_state` raises for the same reason inside the scraper.
- **A strict test double.** The scripted fake in `tests/test_scraper_database.py` requires `rpc()` to get a params dict, like the real client, and records every call chain, so tests assert exactly what was sent.
- **Live checks complement fakes.** `tests/test_live_database_smoke.py` (manual) runs the functions against the real project, covering the parts a fake cannot: the `websites(...)` join, `limit(1)`, and `upsert` with `on_conflict`.

## Related notes
[[main]] · [[schema]] · [[bot]] · [[telegramMessenger]] · [[admin]] · [[pytest]]
