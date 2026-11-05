# fetching.py

## Purpose
Fetches the raw HTML of a site URL, in a way that lets the shared `retry()` helper in [[main|main.py]] do its job.

## Function: `fetch_html(url)`

**Returns:** `response.text` on success; `""` for a permanent HTTP error. **Raises** for anything temporary.

### Flow
1. Sets a browser-like `User-Agent` header (Cloudflare still fingerprints past this on harder sites, but it's a baseline).
2. `requests.get(url, timeout=(5, 30), headers=...)`: 5 s connect timeout, 30 s read timeout.
3. `response.ok` → print `pass`, return the HTML.
4. Status in `RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)` → raise `requests.exceptions.HTTPError("temporary error, status code …")`.
5. Any other non-ok status (400, 401, 403, 404, 410, …) → print the status code and return `""`. Retrying a permanent error wastes time (and hammers a site that is blocking us).

Network errors (`ConnectionError`, `ConnectTimeout`, `ReadTimeout`, any `RequestException`) are **not caught here**; they propagate to the caller.

### How the caller uses it
`process_website` calls `retry(fetching.fetch_html, 3, web_url)`:
- temporary problem → exception → `retry` sleeps 5 s and tries again, up to 3 attempts, then returns `[]`;
- permanent error → `""` → returned immediately, no retry;
- either way `process_website` checks the result with a simple falsy test and skips the site for this run.
A dead site costs about 10 seconds of waiting per run.

## Design history
The first version caught every exception and returned `""`. That looked tidy, but `retry()` can only retry something that raises, so it saw a normal return and never retried: with the site down the test showed **1** request instead of 3. Found in review (Nov 2026) and fixed by letting temporary failures raise. Covered in [[pytest]] by `tests/test_fetching.py`.

## Known constraints
- No retry logic inside `fetch_html` itself — retries belong to the caller.
- A User-Agent alone doesn't defeat Cloudflare on harder sites (e.g. flamecomics.xyz) — those were dropped from V1 rather than "fixed" here. MangaDex now sits behind a browser-verification page; it is deferred until V1 is finalized.
- Invalid URLs (bad scheme, typos in the DB) also raise and get retried 3 times — harmless, just ~10 s.

## Site reference (commented block in the file)
A list of fetch-tested sites with `site_name`, `url`, `priority` and `mode`. It is reference data that became [[site_parse_rules.json]], not active code.
