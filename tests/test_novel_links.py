"""parse_novel_link: pasted link -> (site, slug), using the real site_parse_rules.json."""

import json
from pathlib import Path

import pytest

from src import novel_links

RULES = json.loads((Path(__file__).resolve().parent.parent / "site_parse_rules.json").read_text(encoding="utf-8"))

ASURA = "https://asurascans.com/"
HIVE = "https://hivetoons.org/"
KUN = "https://www.kunmanga.online/"
VORTEX = "https://vortexscans.org/"
MGEKO = "https://www.mgeko.cc/jumbo/manga/"


def ok(site_key, slug):
    return {"ok": True, "site_key": site_key, "slug": slug}


def fail(reason):
    return {"ok": False, "reason": reason}


@pytest.mark.parametrize(
    "link, expected",
    [
        # asura: both link shapes from the design, hash stripped like compilation_fnct does
        ("https://asurascans.com/comics/nano-machine-ab39de10", ok(ASURA, "nano-machine")),
        ("https://asurascans.com/comics/nano-machine-ab39de10/chapter/1", ok(ASURA, "nano-machine")),
        ("https://asurascans.com/comics/nano-machine-ab39de10/", ok(ASURA, "nano-machine")),
        ("https://hivetoons.org/series/some-novel", ok(HIVE, "some-novel")),
        ("https://hivetoons.org/series/some-novel/chapter-12", ok(HIVE, "some-novel")),
        ("https://www.kunmanga.online/manga/some-novel/chapter-3", ok(KUN, "some-novel")),
        ("https://vortexscans.org/series/some-novel/chapter-9", ok(VORTEX, "some-novel")),
        # mgeko: novel page and reader page
        ("https://www.mgeko.cc/manga/the-archmages-restaurant/", ok(MGEKO, "the-archmages-restaurant")),
        (
            "https://www.mgeko.cc/reader/en/the-archmages-restaurant-chapter-1-eng-li/",
            ok(MGEKO, "the-archmages-restaurant"),
        ),
        (
            "https://www.mgeko.cc/reader/en/counterattack-of-the-ugly-orcs-chapter-1-1-eng-li/",
            ok(MGEKO, "counterattack-of-the-ugly-orcs"),
        ),
    ],
)
def test_supported_links(link, expected):
    assert novel_links.parse_novel_link(link, RULES) == expected


@pytest.mark.parametrize(
    "link",
    [
        "asurascans.com/comics/nano-machine-ab39de10",                  # no scheme
        "http://asurascans.com/comics/nano-machine-ab39de10",           # http
        "https://www.asurascans.com/comics/nano-machine-ab39de10",      # www added
        "https://ASURASCANS.com/comics/nano-machine-ab39de10",          # upper-case host
        "https://asurascans.com/comics/nano-machine-ab39de10?ref=x#top",  # query and fragment
        "  https://asurascans.com/comics/nano-machine-ab39de10  \n",    # surrounding whitespace
        "https://asurascans.com:443/comics/nano-machine-ab39de10",      # default port
        "https://kunmanga.online/manga/some-novel/chapter-3",           # www dropped
    ],
)
def test_harmless_variations_still_work(link):
    assert novel_links.parse_novel_link(link, RULES)["ok"] is True


def test_uppercase_slug_is_lowercased():
    assert novel_links.parse_novel_link("https://hivetoons.org/series/Some-Novel", RULES)["slug"] == "some-novel"


def test_percent_encoded_path_is_decoded_before_matching():
    result = novel_links.parse_novel_link("https://hivetoons.org/%73eries/some-novel", RULES)
    assert result == ok(HIVE, "some-novel")


@pytest.mark.parametrize(
    "link, reason",
    [
        ("", "empty"),
        ("   ", "empty"),
        (None, "empty"),
        ("hello world", "not_a_link"),
        ("two links https://hivetoons.org/series/a https://hivetoons.org/series/b", "not_a_link"),
        ("ftp://hivetoons.org/series/some-novel", "not_a_link"),
        ("https://hivetoons.org:8080/series/some-novel", "not_a_link"),
        ("https://hivetoons.org:abc/series/some-novel", "not_a_link"),
        ("javascript:alert(1)", "not_a_link"),
        ("https://example.com/series/some-novel", "unsupported_site"),
        ("https://hivetoons.org.evil.com/series/some-novel", "unsupported_site"),
        ("https://evil.com/hivetoons.org/series/some-novel", "unsupported_site"),
        ("https://hivetoons.org@evil.com/series/some-novel", "unsupported_site"),
        ("https://api.mangadex.org/chapter", "unsupported_site"),   # no link patterns, not addable
        ("https://hivetoons.org/", "not_a_novel_link"),
        ("https://hivetoons.org/about", "not_a_novel_link"),
        ("https://asurascans.com/comics/", "not_a_novel_link"),
        ("https://hivetoons.org/series/-bad-", "bad_slug"),
        ("https://hivetoons.org/series/bad_slug", "bad_slug"),
        ("https://hivetoons.org/series/bad%20slug", "bad_slug"),
        ("https://asurascans.com/comics/-ab39de10", "bad_slug"),     # nothing left after hash strip
        ("https://hivetoons.org/series/" + "a" * 200, "bad_slug"),
    ],
)
def test_rejections(link, reason):
    assert novel_links.parse_novel_link(link, RULES) == fail(reason)


def test_too_long_is_rejected_before_parsing():
    link = "https://hivetoons.org/series/" + "a" * novel_links.MAX_LINK_LEN
    assert novel_links.parse_novel_link(link, RULES) == fail("too_long")


def test_supported_sites_lists_only_addable_sites_with_plain_hosts():
    sites = novel_links.supported_sites(RULES)
    assert [s["host"] for s in sites] == [
        "asurascans.com", "hivetoons.org", "kunmanga.online", "vortexscans.org", "mgeko.cc",
    ]
    assert all("mangadex" not in s["host"] for s in sites)


def test_supported_sites_needs_both_base_url_and_patterns():
    rules = {
        "a": {"base_url": "https://a.com"},
        "b": {"novel_link_patterns": ["^/x/(?P<slug>[^/]+)$"]},
        "c": {"base_url": "https://www.c.com", "novel_link_patterns": ["^/x/(?P<slug>[^/]+)$"]},
    }
    assert novel_links.supported_sites(rules) == [{"key": "c", "host": "c.com"}]


def test_every_pattern_in_the_rules_compiles_and_has_a_slug_group():
    import re
    for key, site in RULES.items():
        for pattern in site.get("novel_link_patterns", []):
            assert "slug" in re.compile(pattern).groupindex, key


# ------------------------------------------------------------------ hash stripping (slug_hash_strip_regex)

@pytest.mark.parametrize(
    "link",
    [
        "https://asurascans.com/comics/nano-machine-ab39de10/chapter/1",
        "https://asurascans.com/comics/nano-machine/chapter/1",          # no hash at all
        "https://asurascans.com/comics/nano-machine-00000000/chapter/1",  # the hash changes over time
        "https://asurascans.com/comics/nano-machine-ffffffff",
        "https://asurascans.com/comics/nano-machine",
    ],
)
def test_asura_hash_never_ends_up_in_the_stored_slug(link):
    assert novel_links.parse_novel_link(link, RULES) == ok(ASURA, "nano-machine")


def test_hash_stripping_only_applies_to_sites_that_have_the_rule():
    # hivetoons has no slug_hash_strip_regex, so a slug that merely looks like a hash is kept
    assert "slug_hash_strip_regex" not in RULES[HIVE]
    assert novel_links.parse_novel_link("https://hivetoons.org/series/nano-machine-ab39de10", RULES) == ok(
        HIVE, "nano-machine-ab39de10"
    )


def test_the_link_parser_strips_the_hash_exactly_like_the_scraper_does():
    # compilation_fnct uses the same rule; if the two ever disagreed, a user's stored slug
    # would never match what the scraper finds and they would silently get no notifications
    import re
    scraper_slug = re.sub(RULES[ASURA]["slug_hash_strip_regex"], "", "nano-machine-ab39de10")
    link_slug = novel_links.parse_novel_link("https://asurascans.com/comics/nano-machine-ab39de10", RULES)["slug"]
    assert scraper_slug == link_slug == "nano-machine"


# ---------------------------------------------------------------------------------------------
# Two-tier slug validation (strict first, relaxed fallback) and the shared title helpers.
# ---------------------------------------------------------------------------------------------

# Real slugs seen on hivetoons / vortexscans captures that the strict rule alone used to reject.
RELAXED_REAL_SLUGS = [
    "the-hero's-ballad",
    "the-archmage's-restaurant",
    "never-mind-the-heir-i'll-focus-on-healing",
    "sovereign-of-a-hundred-blades:-heir-to-the-strongest-style",
    "from-child-actor-to-world-star!",
]


@pytest.mark.parametrize("slug", RELAXED_REAL_SLUGS)
def test_real_relaxed_slugs_are_accepted_via_the_fallback_tier(slug):
    assert novel_links.SLUG_RE.match(slug) is None          # the strict rule really does reject it
    assert novel_links.slug_tier(slug) == "relaxed"
    link = "https://hivetoons.org/series/" + slug
    assert novel_links.parse_novel_link(link, RULES) == ok(HIVE, slug)


def test_plain_slugs_still_use_the_strict_tier():
    assert novel_links.slug_tier("nano-machine") == "strict"
    assert novel_links.slug_tier("solo-leveling-2") == "strict"


def test_percent_encoded_colon_slug_ends_up_the_same_as_the_raw_one():
    raw = novel_links.parse_novel_link("https://vortexscans.org/series/sovereign-of-a-hundred-blades:-heir", RULES)
    encoded = novel_links.parse_novel_link("https://vortexscans.org/series/sovereign-of-a-hundred-blades%3A-heir", RULES)
    assert raw == encoded == ok(VORTEX, "sovereign-of-a-hundred-blades:-heir")


@pytest.mark.parametrize(
    "slug",
    [
        "", "-bad-", "-leading", "bad_slug", "has space", "a/b", "a\\b", "a?b", "a#b", "a%b", "a&b",
        "a<b", "a>b", 'a"b', "a`b", "'leading-apostrophe", "!leading", ":leading",
        "a" * (novel_links.MAX_SLUG_LEN + 1),
    ],
)
def test_slugs_outside_both_tiers_are_rejected(slug):
    assert novel_links.slug_tier(slug) is None


def test_relaxed_slug_at_the_length_limit_is_accepted_and_one_over_is_not():
    assert novel_links.slug_tier("a" * novel_links.MAX_SLUG_LEN) == "strict"
    assert novel_links.slug_tier("a'" * (novel_links.MAX_SLUG_LEN // 2)) == "relaxed"
    assert novel_links.slug_tier("a'" * (novel_links.MAX_SLUG_LEN // 2) + "bc") is None


def test_html_sensitive_characters_in_a_pasted_link_are_still_bad_slugs():
    for evil in ("a<script>", 'a"onmouseover=', "a&amp;b"):
        assert novel_links.parse_novel_link("https://hivetoons.org/series/" + evil, RULES) == fail("bad_slug")


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None),
        ("", None),
        ("   \n\t ", None),
        ("  Killer   Pietro \n", "Killer Pietro"),
        ("Line one\nline two", "Line one line two"),
        ("x" * 500, "x" * novel_links.MAX_TITLE_LEN),
        (12345, "12345"),
    ],
)
def test_clean_title(raw, expected):
    assert novel_links.clean_title(raw) == expected


@pytest.mark.parametrize(
    "slug, expected",
    [
        ("killer-pietro", "Killer Pietro"),
        ("nano-machine", "Nano Machine"),
        ("the-hero-s-ballad", "The Hero's Ballad"),
        ("solo_leveling", "Solo Leveling"),
        ("", ""),
        (None, ""),
    ],
)
def test_prettify_slug_common_cases(slug, expected):
    assert novel_links.prettify_slug(slug) == expected


def test_title_for_prefers_the_scraped_title_then_the_slug_then_a_placeholder():
    assert novel_links.title_for({"display_title": " Real  Title ", "novel_name": "real-title-x"}) == "Real Title"
    assert novel_links.title_for({"display_title": None, "novel_name": "killer-pietro"}) == "Killer Pietro"
    assert novel_links.title_for({"display_title": "   ", "novel_name": "killer-pietro"}) == "Killer Pietro"
    assert novel_links.title_for({"novel_name": None}) == "Unknown title"
    assert novel_links.title_for({}) == "Unknown title"
    assert novel_links.title_for(None) == "Unknown title"
