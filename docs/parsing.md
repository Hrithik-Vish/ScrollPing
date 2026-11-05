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

**Working** (plain HTML anchors; the keyword+number regex is enough). Verified against real captured links in `tests/fixtures/real_links.json`:
- asurascans.com
- hivetoons.org
- kunmanga.online
- vortexscans.org

**Not implemented yet** (the output isn't plain anchors, so it needs its own logic, not a regex fix). [[main|process_website]] skips both before fetching because their `mode` isn't in `SUPPORTED_MODES`:
- mgeko.cc (`mode: html_li_block`) — next up
- api.mangadex.org (`mode: json`) — deferred until V1 is finalized (the site now serves a browser-verification page)

## `site_parse_rules.json` — field reference
One entry per site, keyed by the **exact** `websites.web_domain_name` string (copy-pasted from the browser, never hand-typed). The root file is the source of truth; `docs/site_parse_rules.json` is a copy for the notes.

| Field | Used by | Meaning |
|---|---|---|
| `mode` | `process_website` | `html` today; `html_li_block`, `json` planned |
| `segment_count` | `compilation_fnct` | number of path segments in a real chapter link (4 for asura, 3 for the others); promo/nav links with a different count are skipped |
| `chapter_regex` | `compilation_fnct` | applied to the last two segments; group 1 is the chapter number |
| `slug_hash_strip_regex` | `compilation_fnct` | asurascans only: removes the `-ab39de10` suffix from the slug, both for matching and in the stored URL (the site accepts hashless URLs) |
| `base_url` | `compilation_fnct` | scheme + host only (no path, no trailing slash); joined with the path via `urljoin` to build the absolute `chapter_url`. MangaDex has none yet |
| `item_selector`, `identity_selector`, `identity_attr`, `chapter_text_selector` | planned `html_li_block` | mgeko: which blocks to read, where the novel link is, where the chapter text is |
| `language_filter` | planned `json` | MangaDex: English only for now |

## mgeko (`html_li_block`) — findings and plan
What the site gives us (real markup):
- each entry is `<li class="novel-item">` containing `<a class="list-body" href="/manga/the-archmages-restaurant/">` (the **novel** page, relative, leading slash) and `<h5 class="chapter-title">Chapter 157-eng-li</h5>`;
- sub-chapters look like `Chapter 26-3-eng-li`; the reader URL pattern is `https://www.mgeko.cc/reader/en/<slug>-chapter-<n>-eng-li/` (checked for chapter 1 on two novels).

Consequences:
- `parse_html` returns `[]` for the listing markup, because the href has no keyword followed by digits. Today that is the only reason mgeko doesn't crash anything; the mode guard now makes the skip explicit.
- The rules file already uses `Chapter\s+(\d+(?:-\d+)?)`, which captures `26-3`. The old pattern captured only `26`, so later parts of a chapter would never register as updates. The hyphen must become a dot (`26-3` → `26.3`) before `float()`. Edge case: `100-10` becomes `100.1` and collides with `100-1`; accepted for V1.
- The exact chapter link can be built as `/reader/en/<slug>-<chapter text lowercased, spaces to hyphens>/`, falling back to the novel page if it doesn't resolve. Verify one current chapter in a browser before relying on it.

Planned shape (not implemented): parsing returns `(href, chapter text)` pairs for this mode; `compilation_fnct` (or a sibling function) branches on `rules["mode"]`; `SUPPORTED_MODES` in `main.py` gains `html_li_block`; tests come with it.

## MangaDex (`json`) — notes for later
- Not HTML at all: chapter number is `data[i].attributes.chapter`; manga name from the `relationships` entry of type `manga`, `attributes.title["ja-ro"]` (most lack `en`).
- The reader URL is built from the chapter **id**, `https://mangadex.org/chapter/<id>` (confirm in a browser) — so there is no parsed path to join; `base_url` will be `https://mangadex.org`.
- The same manga can appear under several `translatedLanguage` values; English only for now. Bracket parameters must be URL-encoded via a dict, and the User-Agent must be descriptive.

## Future work (noted in file as a comment)
- `#re.compile needs working with edge cases` — flagged for JS-rendering / Cloudflare-related edge cases in the regex, separate from the asurascans promo-link issue above.
- The hash-suffix workaround assumes sites keep accepting hashless URLs; if asurascans stops, `slug_hash_strip_regex` should only be used for matching.
- Future: per-site class-based CSS selector parsing for sites that don't include "chapter" keywords in URLs at all.
