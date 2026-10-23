# main.py — ScrollPing Orchestrator

**Status:** Core pipeline functional for 4/6 sites. mgeko and mangadex not yet wired in ([[parsing]] dispatch pending).

## Purpose
Orchestrates one full scrape cycle: fetch tracked websites → scrape each for new chapters → compare against DB → push updates → fetch subscribers for updated novels.

## Execution flow (top-level script)
1. [[database]].insert_scraper_logs() — starts a log row, gated by `status`.
2. Load `site_id`/`web_url` lists from [[database]].fetch_websites().
3. Load [[site_parse_rules.json]] **once** into memory.
4. `finding_updated_novels(site_id, web_url, site_parse_rules)` → `updated_novel_list`.
5. [[database]].update_novels_table(updated_novel_list) — persists new chapter numbers/URLs.
6. Extract `novel_id_list` from `updated_novel_list`.
7. [[database]].fetch_subscribers(novel_id_list) → `subscribers_list`.
8. [[database]].update_scraper_logs(row_id) — closes out the log row.

## Functions

**`finding_updated_novels(site_id_list, web_url_list, site_parse_rules)`**
Loops sites one at a time, calls `process_website` per site, flattens results into one list. Pure orchestration, no logic of its own. `site_parse_rules` is loaded once at the top of the script and threaded down through this function into `process_website` — never re-read from disk per site.

**`process_website(site_id, web_url, site_parse_rules)`**
Per-site pipeline: fetch HTML via [[fetching]].fetch_html` → parse via `[[parsing]].parse_html` → fetch known novels for this site from `[[database]].fetch_novels` → build a `fetched_novels` dict keyed by novel name → look up this site's rules (`site_parse_rules[web_url]`, see `[[site_parse_rules.json]]`) → run `compilation_fnct` → return only the updated rows. Fails soft (`return []`) at any stage if a step yields nothing, so one broken site doesn't halt the run.

**`compilation_fnct(parsing_data, fetched_novels, rules)`**
Fully pure function — no globals, no file I/O, no DB/network calls. Takes only the single site's rule-dict (not the full rules map, not a URL to look one up with). Splits each scraped href by `/`, validates segment count against `rules`, extracts chapter number + novel slug via regex, strips hash suffixes where applicable (asurascans), and tracks the highest chapter seen per novel — comparing against a pre-loop snapshot of original values to determine what actually changed. Returns `(updated_rows, skipped)`. Being pure makes this the natural first target for [[pytest]]: a test just needs a hand-built `rules` dict, a list of fake hrefs, and a fake `fetched_novels` dict — no file, no mocking, no monkeypatching required.

**`retry(func, max_attempts, *args)`**
Generic retry wrapper used by every DB/network call site-wide. Returns `[]` on exhaustion — same convention as all [[database]].fetch_* functions.

## Known gaps / deferred on purpose
- **mgeko & mangadex**: not in [[parsing]]'s dispatch yet. Both have non-href-based data shapes and need their own extraction path before `compilation_fnct` can process them. Until then, `updated_novel_list` only reflects 4 of 6 sites — expected, not a bug.
- **`skipped` list is discarded**: `compilation_fnct` collects hrefs that failed segment/regex checks, but `process_website` doesn't log or surface them anywhere yet. No visibility into what's silently being filtered out.
- **Telegram messenger**: `telegramMessenger` is imported but not yet called anywhere in the flow — subscriber-to-notification wiring hasn't been built.
- **No handling if `status` is `False`**: the `insert_scraper_logs()` gate has no visible `else` branch — unclear what happens (or should happen) if the log insert fails.

## Design history worth remembering
`compilation_fnct`'s signature went through three shapes before landing here: first it implicitly relied on a module-level global `site_parse_rules`; then it opened [[site_parse_rules.json]] itself on every call (correct in isolation, but wasteful — 6 redundant file reads per scrape cycle); the current version fixes both by loading the file once at the top of the script and threading the relevant site's rule-dict down as a plain parameter. This was a deliberate choice to keep `compilation_fnct` testable in isolation ahead of the upcoming [[pytest]] work.

## Related notes
- [[database]]
- [[fetching]]
- [[parsing]]
- [[site_parse_rules.json]]
