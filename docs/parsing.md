# parsing.py

## Purpose
Extracts candidate chapter-link URLs from raw HTML, using a keyword+number regex on anchor `href` attributes. Deliberately scoped to "find and return candidate links" only — per-site slug/chapter-number extraction and format differences are `compilation_fnct`'s job, not this file's.

## Function: `parse_html(file)`

**Input:** raw HTML string (from `fetch_html`)
**Returns:** list of href strings (candidate chapter links); `[]` if none found

### Flow
1. Parses `file` with BeautifulSoup using the `lxml` parser.
2. Finds all `<a>` tags whose `href` matches the chapter regex.
3. Extracts `href` via `.get("href")` for each match, appending only truthy values to the result list.

### The regex
```
r"[/-]?(chapters?|chs?|episodes?|eps?|comics?|titles?|series|mangas?)[/-]?(\d+(\.\d+)?)"
```
- Matches a keyword (chapter/ch/episode/ep/comic/title/series/manga, singular or plural) followed by a number, loosely anchored anywhere in the URL string, optionally separated by `/` or `-`.
- Supports half-chapters (e.g. `66.5`) via the optional decimal group.
- This is intentionally loose — it's a first-pass candidate filter, not a final extractor. Precision (rejecting non-chapter matches, pulling out the actual number) is deferred to `compilation_fnct`.

## Known gap (confirmed via real data)
- On asurascans.com, the regex incorrectly matches non-chapter promotional links like `/comics/30-years-since-the-prologue-05c7df14` — it matches "comics" + trailing digits from the hash suffix, not an actual chapter number.
- **Decision:** don't fix the regex for this. It's rare (only a few per page, only on asurascans) and cheaply filtered downstream in `[[main|compilation_fnct]]` via path-segment count (these promo links have only 2 path segments vs. 3–4 for real chapter links).

## Per-site parsing status (V1 target sites)

**Working** (plain HTML anchor scraping, keyword+number regex sufficient):
- asurascans.com
- hivetoons.org
- kunmanga.online
- vortexscans.org

**Not working** (output format differs from plain HTML anchors — needs separate logic, not a regex fix):
- mgeko.cc (`mode: html_li_block`)
- api.mangadex.org (`mode: json` — not HTML at all, structurally different)

Both have their actual rule entries already finalized in `[[site_parse_rules.json]]`, waiting on this file's dispatch logic to use them.

## Future work (noted in file as a comment)
- `#re.compile needs working with edge cases` — flagged for JS-rendering / Cloudflare-related edge cases in the regex, separate from the asurascans promo-link issue above.
- Future: per-site class-based CSS selector parsing for sites that don't include "chapter" keywords in URLs at all.
