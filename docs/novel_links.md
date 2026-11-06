# novel_links.py — pasted link → (site, slug)

**Status:** done and covered by `tests/test_novel_links.py` (53 tests). Pure functions: no network, no database. A pasted link is **never fetched**.

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
6. Validate the slug: `[a-z0-9]+(-[a-z0-9]+)*`, at most 150 characters (`bad_slug`).

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
3. Add a case to `tests/test_novel_links.py`. A guard test already checks every pattern compiles and has a `slug` group.

## Related notes
[[bot]] · [[parsing]] · [[main]] · [[pytest]]
