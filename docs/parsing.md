# parsing.py

## Purpose
Extracts candidate chapter-link URLs from raw HTML, using a keyword+number regex on anchor `href` attributes. Deliberately scoped to "find and return candidate links" only — per-site slug/chapter-number extraction and format differences are `compilation_fnct`'s job, not this file's.

## Function: `parse_html(file)` (mode `html`)

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

**Working through its own mode:**
- mgeko.cc (`mode: html_li_block`): see `parse_html_li_block` below.

**Not implemented yet** (the output isn't HTML anchors at all). [[main|process_website]] skips it before fetching because its `mode` isn't in `SUPPORTED_MODES`:
- api.mangadex.org (`mode: json`) — deferred until V1 is finalized (the site now serves a browser-verification page)

## `site_parse_rules.json` — field reference
One entry per site, keyed by the **exact** `websites.web_domain_name` string (copy-pasted from the browser, never hand-typed). The root file is the source of truth; `docs/site_parse_rules.json` is a copy for the notes.

| Field | Used by | Meaning |
|---|---|---|
| `mode` | `process_website` | `html` and `html_li_block` today; `json` planned |
| `segment_count` | `compilation_fnct` | number of path segments in a real chapter link (4 for asura, 3 for the others); promo/nav links with a different count are skipped |
| `chapter_regex` | `compilation_fnct` | applied to the last two segments; group 1 is the chapter number |
| `slug_hash_strip_regex` | `compilation_fnct` | asurascans only: removes the `-ab39de10` suffix from the slug, both for matching and in the stored URL (the site accepts hashless URLs) |
| `base_url` | `compilation_fnct` | scheme + host only (no path, no trailing slash); joined with the path via `urljoin` to build the absolute `chapter_url`. MangaDex has none yet |
| `item_selector`, `identity_selector`, `identity_attr`, `chapter_text_selector` | `parse_html_li_block` | mgeko: which blocks to read, where the novel link is, where the chapter text is |
| `chapter_url_template` | `compilation_fnct_li_block` | mgeko: path template for the reader URL, joined to `base_url`; placeholders `{slug}` and `{chapter}` (the raw token, e.g. `26-3`) |
| `novel_link_patterns` | [[novel_links]] | regexes with a named group `slug`, matched against the path of a link a user pastes into the bot; a site is addable only if it has these and a `base_url` |
| `language_filter` | planned `json` | MangaDex: English only for now |

## `parse_html_li_block(file, rules)` (mode `html_li_block`, mgeko)
**Input:** raw HTML and the site's rule dict. **Returns:** a list of `{"identity": <href>, "chapter_text": <text>}` dicts, in page order; `[]` when nothing matches.

For each element matching `rules["item_selector"]` (`li.novel-item`) it reads the href from `identity_selector` (`a.list-body`, attribute `identity_attr`) and the text from `chapter_text_selector` (`h5.chapter-title`). Blocks missing either part, or with blank text, are dropped. Every selector comes from the rules, none is hard-coded. [[main|compilation_fnct_li_block]] turns the pairs into update rows.

What the site gives us (real markup):
- each entry is `<li class="novel-item">` containing `<a class="list-body" href="/manga/the-archmages-restaurant/">` (the **novel** page, relative, leading slash) and `<h5 class="chapter-title">Chapter 157-eng-li</h5>`;
- sub-chapters look like `Chapter 26-3-eng-li`; the reader URL pattern is `https://www.mgeko.cc/reader/en/<slug>-chapter-<n>-eng-li/` (checked for chapter 1 on two novels, and encoded as `chapter_url_template`).

Consequences:
- `parse_html` returns `[]` for this markup (the href has no keyword followed by digits), which is why the mode exists.
- `chapter_regex` is `Chapter\s+(\d+(?:-\d+)?)`, which captures `26-3`; the hyphen becomes a dot (`26-3` → `26.3`) before `float()`. Edge case: `100-10` becomes `100.1` and sorts below `100-3` (`100.3`); accepted for V1.
- Worth re-checking in a browser from time to time that a current chapter really lives at the templated reader URL; if the site changes it, only `chapter_url_template` needs editing.

## MangaDex (`json`) — notes for later
- Not HTML at all: chapter number is `data[i].attributes.chapter`; manga name from the `relationships` entry of type `manga`, `attributes.title["ja-ro"]` (most lack `en`).
- The reader URL is built from the chapter **id**, `https://mangadex.org/chapter/<id>` (confirm in a browser) — so there is no parsed path to join; `base_url` will be `https://mangadex.org`.
- The same manga can appear under several `translatedLanguage` values; English only for now. Bracket parameters must be URL-encoded via a dict, and the User-Agent must be descriptive.

## Future work (noted in file as a comment)
- `#re.compile needs working with edge cases` — flagged for JS-rendering / Cloudflare-related edge cases in the regex, separate from the asurascans promo-link issue above.
- The hash-suffix workaround assumes sites keep accepting hashless URLs; if asurascans stops, `slug_hash_strip_regex` should only be used for matching.
- Future: per-site class-based CSS selector parsing for sites that don't include "chapter" keywords in URLs at all.
