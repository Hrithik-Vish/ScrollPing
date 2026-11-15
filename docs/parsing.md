# parsing.py

## Purpose
Turns a site's raw HTML into **what the pipeline needs to know**: which chapters are on the page, which novel each belongs to, what the novel is called, and whether the chapter is locked (and until when). Everything here is pure: HTML text and a rules dict in, plain data out. No network, no database, no clock.

The entry point is `parse_page(html, rules)`. The older `parse_html` and `parse_html_li_block` are kept (and tested) but `main.py` no longer calls them; `parse_page` replaces both.

## `parse_page(html, rules)` → `{"items", "titles", "diagnostics"}`
```
items        [{"href", "chapter_text", "locked", "title", "slug", "chapter", "unlock_at"}, ...]
titles       {slug: title}
diagnostics  {"items": n, "titles": n, "lock": "ok" | "missing" | "n/a", ...counts only}
```
- `slug` is normalised exactly like a pasted link is in [[novel_links]]: percent-decoded, lower-cased, and with the site's hash suffix stripped (`slug_hash_strip_regex`). If the scraper and the bot ever disagreed on a slug, a followed novel would silently never match; a test pins that they agree for every item of every fixture.
- `chapter` is a float (`12.5`), or `None` when a chapter text has no number.
- `locked` is `True` / `False`, or **`None` when the site's lock data could not be read** (asura without its page island). Sites without locks report `False`.
- `unlock_at` is an ISO 8601 UTC string or `None`.
- Which of three strategies runs depends on the site's rules:

| Rules contain | Strategy | Sites |
|---|---|---|
| `lock_detection` | read the page's `<astro-island>` data (below) | asurascans |
| `mode: html_li_block` | selector-based blocks | mgeko |
| otherwise | chapter links (`<a href>`), optional `lock_badge`, titles via `title_selector` | hivetoons, vortexscans, kunmanga |

### Link-based sites (hive, vortex, kunmanga)
- An `<a href>` is a chapter item when its path has `segment_count` segments and `chapter_regex` matches its last two segments. Links to other hosts are ignored; same-site absolute links count.
- **Fragments are dropped** before anything else. Hive and vortex also link `/series/x/chapter-425#comment-18624` (to the chapter's comments); without this, a comment link could become the chapter URL a user is sent to. After dropping the fragment it merges with the plain link.
- Duplicate links (a chapter shown in several page sections) are merged into one item; if any copy carries the lock class, the chapter counts as locked.
- **Titles** come from every element matching `title_selector` (its own `href` gives the slug via `novel_link_patterns`; its text, or the attribute named by `title_attr`, is the title; the first title seen for a slug wins). Hive and vortex use `a.line-clamp-2` with `title_attr: title`. Kunmanga renders titles in three different layouts; the real capture showed `.post-title h4 a` alone found only 16 of 45 novels, so the rule is the union `.post-title a, h5.widget-title a`. A series that has no title anchor on the page simply has no title (the bot and messenger fall back to a guess from the slug).
- **Lock badge** (`lock_badge`): a chapter is locked when its `<a>` has the class `locked_class` (`paid-chapter-row`). Careful: on the real pages almost every chapter row also has the class `newly-free-chapter-row`, so the match is an exact class-list check, never a substring like "free". Lock *state* only: these sites give no unlock time.
- `diagnostics.lock` is `missing` when the lock icon (`marker_selector`, `svg.lucide-lock`) is on the page but no element has the lock class (the site renamed it); otherwise `ok`. A quiet moment with no locked chapters is normal and not flagged.

### asurascans: the page island
Asura renders its latest updates from an `<astro-island>` element whose `props` attribute is JSON in Astro's wrapped format (`[0, value]`, `[1, [wrapped...]]`). `unwrap_astro` unwraps it; `parse_astro_island(html, rules)` finds the list named by `lock_detection.list_key` (`chapters`), preferring the island whose `component-url` contains `lock_detection.component` (`LatestUpdates`) but not depending on it.
- Each entry has the comic's public URL, slug, name, chapter number, `is_premium`, `published_at` and `early_access_until`. `locked = is_premium`; `unlock_at` is `early_access_until` converted to UTC. (Free chapters also carry an `early_access_until`, in the past; it is parsed but means nothing there.) In every sample the unlock time was exactly `published_at` plus 6 hours; if a locked entry has no usable unlock time, `published_at + fallback_window_hours` (6) is used.
- The chapter URL is built from the entry (`<public_url>/chapter/<n>`, written the way the site writes it: `156` not `156.0`, `12.5` as is). A test (spec check A) asserts that every URL built this way equals the URL of the matching `<a>` in the same page.
- The island holds more chapters than the page renders as links (300 against 60 in the capture), and is grouped by comic rather than sorted by time. That is fine: only the highest chapter per slug matters, and the island is the more complete source.
- If more than half of the entries are unusable, or no island is found, the island counts as missing: items fall back to the plain links with `locked = None`, titles are empty, and `diagnostics.lock` is `missing` (the site is `degraded`; alerts still flow).

### mgeko (`html_li_block`)
Per `li.novel-item` block: slug from the novel link (`a.list-body`), chapter text from `h5.chapter-title` (`Chapter 26-3-eng-li` → `26-3` → `26.3`), title from `h4.novel-title` (`title_selector`). Blocks without a chapter row (other sections of the page) contribute titles but no items. Edge case accepted for V1: `26-10` becomes `26.1`.

## `classify_site(diagnostics, fetched_ok, exception)` → `"ok" | "degraded" | "failed"`
`failed`: an exception, a failed fetch, no diagnostics, or zero items. `degraded`: items found but zero titles, or `lock == "missing"`. Otherwise `ok`. (`skipped` is decided by the caller before any fetch and never recorded in site health.)

## `validate_rules(rules)` → list of problem strings (empty = fine)
Checked at the start of every run (problems are printed, not fatal) and by a test against the real file: mode known; `display_name`; `base_url` an http(s) URL; every regex compiles and `chapter_regex` has a capture group; `novel_link_patterns` have a named group `slug`; every CSS selector compiles (with `soupsieve`); a title source exists (`title_selector`, `title_field` or `lock_detection.fields.title`); `lock_detection` and `lock_badge` are not both set; `lock_detection` needs `type`, `list_key`, the `chapter` and `is_locked` fields, a public URL or slug field, and `tier_labels` for `standard` and `member`; `lock_badge` needs `locked_class` and `marker_selector`; li-block sites need their selectors and a template with `{slug}` and `{chapter}`. MangaDex (`json`) is accepted as a placeholder.

## `site_parse_rules.json` — field reference
One entry per site, keyed by the **exact** `websites.web_domain_name` string (copy-pasted from the browser, never hand-typed). The root file is the source of truth; `docs/site_parse_rules.json` is a copy for the notes (a test checks they are identical). The order of the entries is the order sites are listed to users.

| Field | Used by | Meaning |
|---|---|---|
| `mode` | `process_website` | `html` and `html_li_block` today; `json` planned |
| `display_name` | messenger, bot, digest | the site's name shown to people |
| `segment_count` | `parse_page` | number of path segments in a real chapter link (4 for asura, 3 for the others) |
| `chapter_regex` | `parse_page`, compile | applied to the last two segments (or the li-block chapter text); group 1 is the chapter number |
| `slug_hash_strip_regex` | `parse_page`, [[novel_links]] | asurascans only: removes the `-ab39de10` suffix from slugs |
| `base_url` | compile, [[novel_links]] | scheme + host only; joined with a link to build the absolute `chapter_url` |
| `title_selector`, `title_attr` | `parse_page` | where the title is; `title_attr` reads an attribute instead of the text |
| `title_field`, `lock_detection` | `parse_astro_island` | asura: the island's component, list key and field names (`public_url`, `slug`, `chapter`, `title`, `is_locked`, `published_at`, `unlock_at`), `fallback_window_hours` |
| `lock_badge` | `parse_page` | hive and vortex: `locked_class` and `marker_selector` |
| `tier_labels` | bot | the words for the two account types, e.g. `Free reader` / `Asura+ member` (only on sites with `lock_detection`) |
| `item_selector`, `identity_selector`, `identity_attr`, `chapter_text_selector` | li-block parsing | mgeko: which blocks to read, where the novel link is, where the chapter text is |
| `chapter_url_template` | `compilation_fnct_li_block` | mgeko: path template for the reader URL; placeholders `{slug}` and `{chapter}` |
| `novel_link_patterns` | [[novel_links]], `parse_page` | regexes with a named group `slug`, matched against the path of a link (a pasted one, or a series card's own link) |
| `language_filter` | planned `json` | MangaDex: English only for now |

## The old functions (still present)
- **`parse_html(file)`** returns the hrefs matching a loose keyword-plus-number regex. Known quirk: on asurascans it also matches promo links like `/comics/30-years-since-the-prologue-05c7df14`; those never become items because their segment count is wrong.
- **`parse_html_li_block(file, rules)`** returns `{"identity", "chapter_text"}` pairs for mgeko blocks.

## MangaDex (`json`): notes for later
- Not HTML at all: chapter number is `data[i].attributes.chapter`; manga name from the `relationships` entry of type `manga`, `attributes.title["ja-ro"]` (most lack `en`).
- The reader URL is built from the chapter **id**, `https://mangadex.org/chapter/<id>`; `base_url` will be `https://mangadex.org`.
- The same manga can appear under several `translatedLanguage` values; English only for now. Bracket parameters must be URL-encoded via a dict, and the User-Agent must be descriptive.

## Fixtures
`tests/fixtures/*_latest.html` are trimmed copies (3 to 12 KB each) of captures taken on a GitHub Actions runner, so the real markup, including the traps above (comment links, `newly-free-chapter-row`, the `%3A` slug on vortex, the three kunmanga layouts), is what the tests run against. `real_links.json` keeps the older captured hrefs.

## Future work
- Per-site class-based selectors for sites that do not put "chapter" in their URLs at all.
- Unlock times for hive and vortex, if those sites ever expose them.
- The hash-suffix workaround assumes sites keep accepting hashless URLs; if asurascans stops, `slug_hash_strip_regex` should only be used for matching (the compile step already uses the page's own link, hash included).

## Related notes
[[main]] · [[novel_links]] · [[fetching]] · [[scheduling]] · [[pytest]]
