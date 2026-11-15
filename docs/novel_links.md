# novel_links.py — pasted link → (site, slug)

**Status:** done and covered by `tests/test_novel_links.py` (95 tests). Pure functions: no network, no database. A pasted link is **never fetched**. Besides link parsing, the module holds the small text helpers the scraper and the bot share (`clean_title`, `prettify_slug`, `title_for`), so neither has to import the other.

## Purpose
[[bot]] lets people add a novel by pasting any link to it. This module works out which site it belongs to and what the novel's slug is, so the stored slug matches exactly what the scraper finds ([[main|compilation_fnct]]).

```
parse_novel_link("https://asurascans.com/comics/nano-machine-ab39de10/chapter/1", rules)
→ {"ok": True, "site_key": "https://asurascans.com/", "slug": "nano-machine"}
```

## `parse_novel_link(text, rules)`
Returns `{"ok": True, "site_key", "slug"}` or `{"ok": False, "reason"}`. Steps:
1. Trim; reject empty (`empty`), over 300 characters (`too_long`) or anything containing whitespace (`not_a_link`).
2. Add `https://` if there is no scheme; only http/https and default ports are accepted (`not_a_link`).
3. Match the **hostname** (lower-cased, leading `www.` stripped) against the addable sites (`unsupported_site`). Lookalikes such as `hivetoons.org.evil.com` or `evil.com/hivetoons.org/...` do not match, only the real host does.
4. Percent-decode the path and try each of the site's `novel_link_patterns` (`not_a_novel_link` if none matches). The query string and fragment are ignored; a trailing slash is fine.
5. Lower-case the slug and, **if the site's rules have `slug_hash_strip_regex`**, strip it. This is the same regex [[main|compilation_fnct]] uses, which matters: asurascans appends a changing hash (`nano-machine-ab39de10`), so the stored slug is always the stable `nano-machine`, and links with or without the hash give the same result. Sites without that rule keep their slug as is.
6. Validate the slug in **two tiers** (`slug_tier`), at most 150 characters (`bad_slug` if neither fits):
   - **strict** (`SLUG_RE`): `[a-z0-9]+(-[a-z0-9]+)*`, plain words joined by hyphens; tried first;
   - **relaxed** (`RELAXED_SLUG_RE`): must start with a letter or digit, then letters, digits, `-` and the three extra characters real sites use, **apostrophe, `!` and `:`** (for example `the-hero's-ballad`, `from-child-actor-to-world-star!`, `sovereign-of-a-hundred-blades:-heir`). Strict is a subset of relaxed, so the second tier only decides which rule matched; it exists so plain slugs stay the documented normal case and every real slug in the captures is accepted. Deliberately **not** allowed: underscore, whitespace and `/ \ ? # % & < > "` and backtick. A percent-encoded colon (`%3A`) is decoded in step 4, so both spellings give the same slug.

## Title helpers (shared by the scraper and the bot)
- **`clean_title(raw)`**: collapse whitespace, strip, cap at 200 characters; empty or `None` becomes `None`.
- **`prettify_slug(slug)`**: a readable guess at a title for display only, never stored: `the-hero-s-ballad` → `The Hero's Ballad`.
- **`title_for(row)`**: the scraped `display_title` if there is one, else the prettified slug, else `Unknown title`. Used by the bot's menus and the messenger's alerts.

## `supported_sites(rules)`
`[{"key": <rules key>, "host": <display host>}]` for the sites that have **both** a `base_url` and `novel_link_patterns`, in rules order. It drives the "supported websites" list shown to users, so MangaDex (no patterns, not addable) is never offered.

## Rules fields used (see [[parsing]] for the full field reference)
| Field | Meaning |
|---|---|
| `base_url` | its host is what a pasted link is matched on |
| `novel_link_patterns` | list of regexes, each with a named group `slug`, run with `re.match` against the decoded path |
| `slug_hash_strip_regex` | optional, asurascans only |

Current patterns, one per link shape:

| Site | Accepted shapes |
|---|---|
| asurascans.com | `/comics/<slug>-<hash>` and `/comics/<slug>-<hash>/chapter/<n>` (and hashless) |
| hivetoons.org | `/series/<slug>[/...]` |
| kunmanga.online | `/manga/<slug>[/...]` |
| vortexscans.org | `/series/<slug>[/...]` |
| mgeko.cc | `/manga/<slug>[/...]` and the reader form `/reader/en/<slug>-chapter-<n>-eng-li/` |

**Verified with real links:** asurascans.com and hivetoons.org (live bot test). **Inferred from chapter URL shapes, worth one real paste each:** kunmanga.online, vortexscans.org, mgeko.cc. A wrong pattern fails safe: the user gets "that is not a novel or chapter page" and nothing is stored.

## Adding a site
1. Add its rules entry (with `base_url`) to `site_parse_rules.json`.
2. Add `novel_link_patterns` with a `(?P<slug>...)` group, and `slug_hash_strip_regex` if the site suffixes slugs.
3. Add a case to `tests/test_novel_links.py`. A guard test already checks every pattern compiles and has a `slug` group. If the site's real slugs use other characters than the relaxed tier allows, widen `RELAXED_SLUG_RE` deliberately and add them to the tests.
4. Remember the scraper must produce the same slug from the site's own chapter links: `tests/test_parse_page.py` checks that for every fixture.

## Related notes
[[bot]] · [[parsing]] · [[main]] · [[telegramMessenger]] · [[pytest]]
