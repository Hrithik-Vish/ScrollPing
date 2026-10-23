# fetching.py

## Purpose
Fetches raw HTML from a given site URL, with layered error handling for network failures.

## Function: `fetch_html(url)`

**Returns:** `response.text` (str) on success, `""` on any failure.

### Flow
1. Sets a descriptive User-Agent header (Cloudflare still fingerprints past this at deeper layers, but it's a baseline).
2. Calls `requests.get()` with split timeouts: `(5, 30)` — 5s connect timeout, 30s read timeout.
3. Checks `response.ok`; returns `response.text` if true.
4. On non-ok status: logs the status code, returns `""`.

### Exception handling (layered, most specific first)
- `ConnectTimeout` → logs, returns `""`
- `ConnectionError` → logs, returns `""`
- `RequestException` (catch-all for remaining request errors) → logs error detail, returns `""`

All four exit paths (non-ok, and the three exception branches) consistently return `""` — this makes `fetch_html`'s failure mode uniform and easy for callers (`process_website`) to check with a simple falsy test.

## Known constraints
- No retry logic inside `fetch_html` itself — retries are handled by the caller via the shared `retry()` helper in [[main|main.py]].
- User-Agent alone doesn't defeat Cloudflare on harder sites (e.g. flamecomics.xyz) — that's why those sites are deferred/dropped rather than "fixed" here.

## Site dispatch reference (commented block in file)
A `SITES` list documenting fetch-working sites with `site_name`, `url`, `priority`, and `mode` (`html` / `html_li_block` / `json`). This is reference data that eventually became [[site_parse_rules.json]], not active code itself.
