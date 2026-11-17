# main.py — ScrollPing Orchestrator

**Status:** The full pipeline works end to end for 5 of 6 sites (asurascans, hivetoons, kunmanga, vortexscans, mgeko): scrape → compare → queue → update novels → deliver → admin step → close the log row. mangadex is skipped on purpose until its `json` mode is implemented (see [[parsing]]). Covered by [[pytest]] (`tests/test_main.py`, `test_run.py`, `test_site_isolation.py`, `test_mgeko.py`, `test_admin_step.py`, `test_pipeline_flow.py`). Users manage their subscriptions through the separate Telegram command handler, see [[bot]]; `main.py` itself only scrapes and sends.

## Purpose
Runs one full cycle: gather what changed on every tracked site → decide who must be told, and when → write that to a delivery queue → advance the novels → deliver what is due → tell the admins about problems → mark the run successful.

`main.py` is the **impure shell**. The decisions live in pure modules it calls: [[parsing]] (what is on a page), [[scheduling]] (who is told when, which badge), [[admin]] (alerts and digest). The Telegram side is [[telegramMessenger]]; every database call is in [[database]].

## `run()` — execution flow
1. **Log gate.** [[database]].`insert_scraper_logs()` → `(status, row_id)`. If `status` is `False`: print a message and `sys.exit(1)`; nothing else runs.
2. **Queue maintenance.** `maintain_queue()` gives back rows a dead run left in `sending` for over 30 minutes, cancels rows whose subscription is no longer `reading`, and expires rows more than 24 hours overdue. Fail-soft: a failure is printed and the run continues.
3. **Config.** `fetch_websites()`, load `site_parse_rules.json` **once**, run [[parsing]].`validate_rules` and print each problem (the run still continues; a broken site fails on its own).
4. **Per-site loop.** `finding_updated_novels(...)` returns one **result object** per site (see `process_website`).
5. **Site health.** For every site that was not `skipped`, `record_site_result(site_id, status, error)` (fail-soft; failures are only counted).
6. **Titles.** All sites' `title_updates` go to `sync_novel_titles` in one call. It never touches `updated_at` (decision D4: a title sync must not keep a dead novel alive past the 97-day cleanup).
7. **Recipients** (only if some chapter changed). `fetch_subscribers_with_preferences(novel_ids, web_id_by_novel)` → recipients (active users, status `reading`, with their per-novel timing), site tiers, and `failed_novel_ids`.
8. **Queue first (decision D1).** `scheduling.build_queue_records(...)` makes one record per (chapter, recipient) with its own `due_at`, and `insert_queue_records` writes them. Novels whose recipients could not be read, or whose rows failed to insert, are **held back**: they are not advanced, so the chapter is simply detected again next run.
9. **Novels second.** `update_novels_table(rows not held back)`: one RPC for the whole run. A failed id only means that write did not happen; it never blocks an alert, because the queue already has the rows.
10. **Deliver.** `dispatch_queue(rules=...)` claims due rows (500 at a time, at most 10 passes), groups them per Telegram user, and hands them to [[telegramMessenger]].`send_all`, which acknowledges every outcome through a `QueueDelivery`. Runs even on a quiet run, because the queue also holds retries and delayed unlocks from earlier runs. **It stops after the first pass in which any row was given back** (a transient failure): a released row is `pending` and due again at once, so another pass in the same run would claim it again, and every claim counts an attempt. Without this rule an outage of a minute or two burned the whole attempt cap and marked the alerts dead; now a failing row is claimed once per run and keeps its attempts for the following hourly runs. `InvalidToken`, a missing token and any unexpected error each print a message and set `notify_failed`.
11. **Admin step.** `run_admin_tasks(rules, now)`: alerts for sites that failed three or more runs in a row, and the daily digest when due (see below). Wrapped in `try/except`: a problem here never fails an otherwise good run.
12. **Close the log.** `update_scraper_logs(row_id)` flips the row to `success`. This happens even after a fatal send problem: the scrape and database work did finish.
13. If `notify_failed`, `sys.exit(1)` (after step 12), so the GitHub Actions run shows red.

### Why the order is what it is
- **Queue before novels.** Previously the novel was updated first and a crash before sending lost the alert for good. Now the alert exists in the database before anything is advanced. A crash between steps 8 and 9 costs nothing: the next run detects the chapter again, and the queue's unique key `(user_id, novel_id, chapter)` makes the re-insert a no-op.
- **Delivery is last among the data steps,** so users are never told about data the database does not have.
- **The log flip never depends on delivery.** `scraper_logs.run_status` means "scrape and database work finished", not "every notification was delivered".
- **Partial send failures do not change the exit code.** Blocked users, refused messages and exhausted retries are counted in the summary; only run-level problems (token) exit 1.
- **A quiet run does not check the token.** `send_all` is only called when something was claimed, so a rejected token is noticed the next time there is something to send.

## Functions

**`finding_updated_novels(site_id_list, web_url_list, site_parse_rules)`**
Loops the sites one at a time and returns one result object each. Each site runs inside `try/except Exception`: an unexpected error prints the site id, URL and traceback (to the log only), and that site becomes a `failed` result carrying **only the error class name** (never a message), so the other sites still run. `KeyboardInterrupt` is not swallowed.

**`process_website(site_id, web_url, site_parse_rules)`** → result object
`{"site_id", "web_url", "status", "error", "rows", "title_updates", "skipped"}`. Steps:
1. No rules for the site, or a `mode` not in `SUPPORTED_MODES` → status `skipped`, **before any network call**.
2. [[fetching]].`fetch_html` through `retry()` (3 attempts); nothing returned → `failed`, error `fetch failed`.
3. [[parsing]].`parse_page` → items, titles, diagnostics; [[parsing]].`classify_site` turns the diagnostics into `ok` / `degraded` / `failed`. No items on the page → `failed` (a "latest updates" page always has some), and the database is not even asked.
4. [[database]].`fetch_novels` through `retry()`; a site with no tracked novels is still a healthy scrape.
5. Compile (below) → rows; `find_title_updates` → title updates; print the untracked links (at most 25 per site, then a count).

Status meanings: `ok`; `degraded` (the page parsed but titles or lock detection are missing: alerts still work, the health row says why); `failed`; `skipped` (never recorded in site health).

**`compilation_fnct(items, fetched_novels, rules)`** (mode `html`) and **`compilation_fnct_li_block(...)`** (mode `html_li_block`, mgeko)
Pure. Both consume the **items** `parse_page` produced, so link parsing no longer happens here. For every tracked slug they keep the item with the highest chapter (the first one seen wins a tie) and emit a row only if it is newer than the database's `latest_chap`. A row is
`{"id", "novel_name", "latest_chap", "chapter_url", "display_title", "access_state", "unlock_at", "web_id"}`:
- `access_state` is `locked` / `free`, or `unknown` when lock parsing failed; `unlock_at` is an ISO time or `None`;
- `chapter_url` is the page's own link joined to `base_url` (so asura keeps its hash, which is part of the working URL); mgeko builds it from `chapter_url_template` and the raw chapter token (`26-3` stays `26-3` in the URL);
- `web_id` is not in the original spec's row shape; it is carried so a recipient's tier **on this site** can be looked up when the queue records are built.
`fetched_novels` is not mutated. Returns `(rows, skipped)`, where `skipped` lists links to novels nobody tracks.

**`find_title_updates(titles, fetched_novels, skip_ids)`**
Tracked novels whose scraped title differs from the stored one. A novel that already has a chapter row is left out: the batch update writes its title.

**`QueueDelivery`**
What the messenger calls after each send. It holds no logic: `ack_sent`, `release`, `mark_dead` and `blocked` are one database call each (`blocked` also cancels the user's waiting rows).

**`group_claimed(claimed)`** → `{telegram_user_id: [claimed row, ...]}` in queue order. Pure.

**`dispatch_queue(delivery=None, rules=None)`**
The claim → send loop described in step 10, including the early stop after a pass that released rows (it prints one counts-only line). If more rows were still due than one pass can claim, they wait for the next run. Returns the summed counts-only summary. Exceptions from the messenger propagate to `run()`; rows still `sending` are recovered by the next run's maintenance step.

**`run_admin_tasks(rules, now)`** (step 11)
- No admin ids (`ADMIN_TELEGRAM_USER_IDS` empty) → one printed line, nothing else.
- Alerts: for every site the database reports as failing 3+ runs and not yet reported, send [[admin]].`build_alert` to every admin. A site is marked reported only if **at least one admin received it**, so an undelivered alert is tried again next run.
- Digest: read the "last sent" marker; if [[admin]].`digest_due` says it is time, build and send it, and store the marker **only after a successful delivery**. If the marker cannot be read the digest is skipped (a database blip must not cause a duplicate); alerts, which come first, are unaffected.

**`retry(func, max_attempts, *args)`**
Generic wrapper: calls `func`, and on any exception sleeps 5 seconds and tries again, up to `max_attempts`. Returns `[]` after the last failure. It can only retry something that raises, which is why [[fetching]].`fetch_html` raises on temporary failures.

**`print_send_summary(summary)`** prints counts only: `telegram: sent=… blocked=… bad_request=… failed=… retried=…`. Never chat ids, titles or message text: the repository is public, and so are its Actions logs.

`SUPPORTED_MODES = ("html", "html_li_block")` lives at the top of `main.py`. Adding the `json` mode (MangaDex) later starts with adding it there, plus a parse and compile pair and a branch in `process_website`.

## Known gaps / deferred on purpose
- **mangadex** is skipped each run (mode not supported yet).
- **Skipped links** are only printed per site (capped); there is no log table yet.
- **Per-site history** is one row per site (`site_health`), not a log: it shows the current streak and the last failure, not the past.
- **A chapter that appears and disappears between two runs is never seen.** Nothing here can fix that; it is inherent to polling.
- **New novels start at `latest_chap = -1`.** The bot creates novels without a chapter, and the column defaults to `-1` (`not null`), so the compile functions need no null handling: `-1` is below every real chapter, including chapter 0. The first scrape that sees the novel fills in the real number and notifies the subscriber.
- `update_scraper_logs`'s return value is ignored by `run()`.

## Design history worth remembering
- The delivery queue replaced the old flow "update novels, then send, losing failed sends for good". Its design decisions (D1 queue before novels, D4 titles never touch `updated_at`, delivery on every run) are spelled out above.
- `compilation_fnct`'s signature went through several shapes: a hidden global → opening the JSON on every call → one site's rule dict passed down → items from `parse_page`. Chosen each time to keep it testable in isolation.
- Bugs found in review and fixed over time: `update_novels_table` wrote every novel three times (missing `break`); `retry()` never retried `fetch_html`; a site without `segment_count` crashed the run; hive and vortex also link `…/chapter-425#comment-18624`, whose fragment used to end up in the chapter URL (fragments are now dropped when items are parsed).

## Related notes
[[database]] · [[fetching]] · [[parsing]] · [[scheduling]] · [[admin]] · [[telegramMessenger]] · [[bot]] · [[schema]] · [[pytest]] · [[site_parse_rules.json]]
