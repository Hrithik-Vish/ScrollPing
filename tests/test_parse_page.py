"""Phase 2: parse_page, the asura island reader, classify_site and validate_rules.

The fixtures in tests/fixtures/*_latest.html are trimmed copies of captures taken on a GitHub Actions
runner (Azure IP). Their structure is intact, so the real selectors are what gets tested.
"""

import copy
import html as html_lib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import pytest
from bs4 import BeautifulSoup

from src import novel_links, parsing

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RULES = json.loads((ROOT / "site_parse_rules.json").read_text(encoding="utf-8"))

ASURA = "https://asurascans.com/"
HIVE = "https://hivetoons.org/"
KUN = "https://www.kunmanga.online/"
VORTEX = "https://vortexscans.org/"
MGEKO = "https://www.mgeko.cc/jumbo/manga/"

FIXTURE_FILES = {ASURA: "asura_latest.html", HIVE: "hive_latest.html", KUN: "kun_latest.html",
                 VORTEX: "vortex_latest.html", MGEKO: "mgeko_latest.html"}


def load(site_key):
    return (FIXTURES / FIXTURE_FILES[site_key]).read_text(encoding="utf-8")


def parse(site_key, html=None):
    return parsing.parse_page(load(site_key) if html is None else html, RULES[site_key])


def by_slug_chapter(result):
    return {(i["slug"], i["chapter"]): i for i in result["items"]}


# ---------------------------------------------------------------------------------------------
# synthetic asura islands (for the paths real captures cannot show: bad data, missing fields)
# ---------------------------------------------------------------------------------------------

def make_entry(**overrides):
    entry = {
        "comic_name": "Some Comic", "comic_slug": "some-comic", "comic_public_url": "/comics/some-comic-f212c582",
        "number": 10, "is_premium": False,
        "published_at": "2026-11-09T12:00:00.000000Z", "early_access_until": "2026-11-09T18:00:00.000000Z",
    }
    entry.update(overrides)
    return {k: v for k, v in entry.items() if v is not ...}


def astro_wrap(value):
    if isinstance(value, dict):
        return [0, {k: astro_wrap(v) for k, v in value.items()}]
    if isinstance(value, list):
        return [1, [astro_wrap(v) for v in value]]
    return [0, value]


def island_page(entries, component="/_astro/LatestUpdates.AbC123.js", extra_island=""):
    props = {"chapters": [1, [astro_wrap(e) for e in entries]], "other": [0, "x"]}
    attr = html_lib.escape(json.dumps(props), quote=True)
    return (f'<html><body>{extra_island}<astro-island component-url="{component}" props="{attr}">'
            f"</astro-island></body></html>")


# ---------------------------------------------------------------------------------------------
# unwrap_astro
# ---------------------------------------------------------------------------------------------

def test_unwrap_astro_handles_plain_objects_arrays_and_nesting():
    assert parsing.unwrap_astro([0, 5]) == 5
    assert parsing.unwrap_astro([0, "text"]) == "text"
    assert parsing.unwrap_astro([0, None]) is None
    assert parsing.unwrap_astro([0, {"a": [0, 1], "b": [1, [[0, 2], [0, 3]]]}]) == {"a": 1, "b": [2, 3]}
    assert parsing.unwrap_astro([1, [[0, {"x": [0, True]}], [0, {"x": [0, False]}]]]) == [{"x": True}, {"x": False}]


def test_unwrap_astro_returns_unknown_shapes_untouched():
    assert parsing.unwrap_astro("plain") == "plain"
    assert parsing.unwrap_astro([7, "x"]) == [7, "x"]
    assert parsing.unwrap_astro([1, 2, 3]) == [1, 2, 3]
    assert parsing.unwrap_astro({"a": 1}) == {"a": 1}


# ---------------------------------------------------------------------------------------------
# asura (island)
# ---------------------------------------------------------------------------------------------

LOCKED_ASURA = {
    ("emperor-of-solo-play", 92.0): "2026-11-10T02:41:27.273007+00:00",
    ("i-am-the-fated-villain", 369.0): "2026-11-10T02:11:49.315027+00:00",
    ("reincarnator", 159.0): "2026-11-09T23:21:24.781461+00:00",
    ("limitless-predation", 30.0): "2026-11-09T22:47:46.953765+00:00",
}
FREE_ASURA = {("emperor-of-solo-play", 91.0), ("emperor-of-solo-play", 90.0), ("cyberpunk-chivalry", 6.0),
              ("cyberpunk-chivalry", 5.0), ("cyberpunk-chivalry", 4.0), ("i-am-the-fated-villain", 368.0)}


def test_asura_reads_every_island_chapter_with_its_lock_state():
    result = parse(ASURA)
    items = by_slug_chapter(result)
    assert len(result["items"]) == 10
    assert {k for k, v in items.items() if v["locked"]} == set(LOCKED_ASURA)
    assert {k for k, v in items.items() if not v["locked"]} == FREE_ASURA
    assert result["diagnostics"]["lock"] == "ok"
    assert result["diagnostics"]["items"] == 10
    assert result["diagnostics"]["titles"] == 5
    assert all(isinstance(i["locked"], bool) for i in result["items"])


def test_asura_unlock_time_is_the_sites_timestamp_in_utc_iso():
    items = by_slug_chapter(parse(ASURA))
    for key, expected in LOCKED_ASURA.items():
        assert items[key]["unlock_at"] == expected
    # all locked samples unlock exactly 6 hours after publishing, as the spec observed
    soup_props = BeautifulSoup(load(ASURA), "lxml").find("astro-island")["props"]
    entries = [parsing.unwrap_astro(v) for v in json.loads(soup_props)["chapters"][1]]
    for entry in entries:
        if entry["is_premium"]:
            gap = parsing._parse_iso(entry["early_access_until"]) - parsing._parse_iso(entry["published_at"])
            assert gap == timedelta(hours=6)


def test_asura_check_a_island_urls_equal_the_anchor_urls_in_the_same_page():
    """Spec check A: the chapter URL built from the island is the URL of the matching <a>."""
    result = parse(ASURA)
    anchors = {urljoin("https://asurascans.com", a["href"])
               for a in BeautifulSoup(load(ASURA), "lxml").find_all("a", href=True)}
    built = {i["href"] for i in result["items"]}
    assert built == anchors


def test_asura_slug_and_url_keep_the_hash_only_in_the_url():
    item = by_slug_chapter(parse(ASURA))[("emperor-of-solo-play", 92.0)]
    assert item["slug"] == "emperor-of-solo-play"
    assert item["href"] == "https://asurascans.com/comics/emperor-of-solo-play-f212c582/chapter/92"


def test_asura_titles_come_from_the_island_by_slug():
    result = parse(ASURA)
    assert result["titles"]["emperor-of-solo-play"] == "Emperor of Solo Play"
    assert result["titles"]["cyberpunk-chivalry"] == "Cyberpunk Chivalry"
    assert by_slug_chapter(result)[("reincarnator", 159.0)]["title"] == "Reincarnator"


def test_asura_island_is_found_even_if_the_component_name_changes():
    page = island_page([make_entry()], component="/_astro/SomethingElse.zzz.js")
    result = parsing.parse_page(page, RULES[ASURA])
    assert result["diagnostics"]["lock"] == "ok" and len(result["items"]) == 1


def test_asura_missing_island_falls_back_to_links_with_unknown_lock_state():
    page = load(ASURA).replace("astro-island", "astro-gone")
    result = parse(ASURA, page)
    assert result["diagnostics"]["lock"] == "missing"
    assert len(result["items"]) == 10                      # alerts still work from the plain links
    assert all(i["locked"] is None for i in result["items"])
    assert all(i["unlock_at"] is None for i in result["items"])
    assert result["titles"] == {}
    assert parsing.classify_site(result["diagnostics"], True, None) == "degraded"


def test_asura_fallback_items_still_carry_slug_and_chapter():
    page = load(ASURA).replace("astro-island", "astro-gone")
    items = by_slug_chapter(parse(ASURA, page))
    assert ("emperor-of-solo-play", 92.0) in items
    assert items[("emperor-of-solo-play", 92.0)]["href"].endswith("/chapter/92")


def test_asura_unparsable_props_count_as_a_missing_island():
    page = '<html><body><astro-island component-url="/_astro/LatestUpdates.x.js" props="not json{"></astro-island></body></html>'
    result = parse(ASURA, page)
    assert result["diagnostics"]["lock"] == "missing" and result["items"] == []


def test_asura_island_is_malformed_when_more_than_half_the_entries_are_unusable():
    good = make_entry(number=1)
    bad = [make_entry(is_premium=...) for _ in range(3)]               # missing the lock flag
    assert parsing.parse_page(island_page([good] + bad), RULES[ASURA])["diagnostics"]["lock"] == "missing"
    # exactly half unusable is still accepted; the good ones are kept
    result = parsing.parse_page(island_page([good, make_entry(is_premium=...)]), RULES[ASURA])
    assert result["diagnostics"]["lock"] == "ok" and len(result["items"]) == 1
    assert result["diagnostics"]["skipped_entries"] == 1


def test_asura_entry_without_url_and_slug_or_chapter_is_skipped():
    entries = [make_entry(number=1), make_entry(number=2), make_entry(comic_public_url=..., comic_slug=...),
               make_entry(number=...)]
    result = parsing.parse_page(island_page(entries), RULES[ASURA])
    assert [i["chapter"] for i in result["items"]] == [1.0, 2.0]


def test_asura_slug_field_is_used_when_the_public_url_is_missing():
    entry = make_entry(comic_public_url=..., comic_slug="Only-Slug")
    item = parsing.parse_page(island_page([entry, make_entry(number=2)]), RULES[ASURA])["items"][0]
    assert item["slug"] == "only-slug"
    assert item["href"] == "https://asurascans.com/comics/only-slug/chapter/10"


def test_asura_slug_mismatch_between_url_and_field_is_counted_not_fatal():
    entry = make_entry(comic_slug="a-different-slug")
    result = parsing.parse_page(island_page([entry]), RULES[ASURA])
    assert result["items"][0]["slug"] == "some-comic"            # the URL wins: that is what users paste
    assert result["diagnostics"]["slug_mismatches"] == 1


@pytest.mark.parametrize(
    "number, expected_tail, expected_chapter",
    [(156, "/chapter/156", 156.0), (156.0, "/chapter/156", 156.0), (12.5, "/chapter/12.5", 12.5),
     ("12.5", "/chapter/12.5", 12.5), ("7", "/chapter/7", 7.0)],
)
def test_asura_chapter_number_is_written_the_way_the_site_writes_it(number, expected_tail, expected_chapter):
    item = parsing.parse_page(island_page([make_entry(number=number)]), RULES[ASURA])["items"][0]
    assert item["href"].endswith(expected_tail)
    assert item["chapter"] == expected_chapter


def test_asura_locked_chapter_with_bad_unlock_time_uses_published_plus_window():
    entry = make_entry(is_premium=True, early_access_until="garbage", published_at="2026-11-09T12:00:00Z")
    item = parsing.parse_page(island_page([entry]), RULES[ASURA])["items"][0]
    assert item["locked"] is True
    assert item["unlock_at"] == "2026-11-09T18:00:00+00:00"


def test_asura_locked_chapter_with_no_usable_times_has_no_unlock_time():
    entry = make_entry(is_premium=True, early_access_until=None, published_at="nope")
    item = parsing.parse_page(island_page([entry]), RULES[ASURA])["items"][0]
    assert item["locked"] is True and item["unlock_at"] is None


def test_asura_free_chapter_with_bad_times_has_no_unlock_time_and_no_fallback():
    entry = make_entry(is_premium=False, early_access_until="garbage")
    item = parsing.parse_page(island_page([entry]), RULES[ASURA])["items"][0]
    assert item["locked"] is False and item["unlock_at"] is None


def test_asura_lock_flag_is_is_premium_not_the_unlock_time():
    # early_access_until exists on free chapters too (in the past); it must not make them locked
    entry = make_entry(is_premium=False, early_access_until="2099-01-01T00:00:00Z")
    assert parsing.parse_page(island_page([entry]), RULES[ASURA])["items"][0]["locked"] is False


def test_asura_title_whitespace_is_cleaned_and_empty_titles_are_dropped():
    entries = [make_entry(comic_name="  Two\n Lines  ", number=1), make_entry(comic_name="   ", comic_public_url="/comics/other-aaaaaaaa", number=1)]
    result = parsing.parse_page(island_page(entries), RULES[ASURA])
    assert result["titles"] == {"some-comic": "Two Lines"}


def test_asura_z_suffix_and_offsets_are_normalised_to_utc():
    entry = make_entry(is_premium=True, early_access_until="2026-11-09T18:00:00+05:30")
    assert parsing.parse_page(island_page([entry]), RULES[ASURA])["items"][0]["unlock_at"] == "2026-11-09T12:30:00+00:00"


# ---------------------------------------------------------------------------------------------
# hivetoons and vortexscans (badge-only locks)
# ---------------------------------------------------------------------------------------------

HIVE_LOCKED = {"never-mind-the-heir-i'll-focus-on-healing", "crocodile", "my-brother-is-a-vicious-dog"}
HIVE_FREE = {"the-demon-king's-friend", "lookism", "sword-devouring-swordmaster", "eleceed"}
VORTEX_LOCKED = {"sovereign-of-a-hundred-blades:-heir-to-the-strongest-style", "heroes-never-rest",
                 "got-a-gallery-in-the-wild"}
VORTEX_FREE = {"the-hero's-ballad", "copying-skills-with-affection", "solo-swordmaster",
               "the-necromancer-familys-young-heir"}


@pytest.mark.parametrize("site, locked, free", [(HIVE, HIVE_LOCKED, HIVE_FREE), (VORTEX, VORTEX_LOCKED, VORTEX_FREE)])
def test_badge_sites_mark_locked_rows_by_the_lock_class(site, locked, free):
    result = parse(site)
    assert {i["slug"] for i in result["items"] if i["locked"]} == locked
    assert {i["slug"] for i in result["items"] if not i["locked"]} == free
    assert all(i["unlock_at"] is None for i in result["items"])          # never an unlock guess
    assert result["diagnostics"] == {"items": 7, "titles": 6, "lock": "ok"}


@pytest.mark.parametrize("site", [HIVE, VORTEX])
def test_free_rows_carry_the_newly_free_class_too_but_only_paid_rows_are_locked(site):
    """Trap found in the real captures: free rows have 'newly-free-chapter-row'; only 'paid-chapter-row' locks."""
    text = load(site)
    assert "newly-free-chapter-row" in text
    soup = BeautifulSoup(text, "lxml")
    chapter_rows = [a for a in soup.find_all("a", href=True) if "chapter" in a["href"] and "#" not in a["href"]]
    free_rows = [a for a in chapter_rows if "paid-chapter-row" not in a["class"]]
    assert free_rows and all("newly-free-chapter-row" in a["class"] for a in free_rows)
    items = parse(site)["items"]
    assert not any(i["locked"] for i in items if i["slug"] in (HIVE_FREE | VORTEX_FREE))


@pytest.mark.parametrize("site", [HIVE, VORTEX])
def test_comment_links_never_become_a_chapter_url(site):
    """Real captures link '/series/x/chapter-N#comment-ID' too; the fragment must be dropped and merged."""
    text = load(site)
    assert "#comment-" in text                                      # the trap is really in the fixture
    result = parse(site)
    assert all("#" not in i["href"] for i in result["items"])
    hrefs = [i["href"] for i in result["items"]]
    assert len(hrefs) == len(set(hrefs)) == 7                        # merged into the plain chapter link


@pytest.mark.parametrize("site", [HIVE, VORTEX])
def test_titles_come_from_the_title_attribute_of_the_series_card(site):
    result = parse(site)
    soup = BeautifulSoup(load(site), "lxml")
    for anchor in soup.select("a.line-clamp-2"):
        assert novel_links.clean_title(anchor["title"]) in result["titles"].values()
    assert result["titles"]["lookism" if site == HIVE else "solo-swordmaster"] in ("Lookism", "Solo Swordmaster")


@pytest.mark.parametrize("site, slug", [(HIVE, "eleceed"), (VORTEX, "the-necromancer-familys-young-heir")])
def test_a_series_with_no_title_anchor_on_the_page_just_has_no_title(site, slug):
    result = parse(site)
    assert slug not in result["titles"]
    assert next(i for i in result["items"] if i["slug"] == slug)["title"] is None


def test_vortex_colon_slug_matches_between_chapter_link_and_percent_encoded_title_link():
    text = load(VORTEX)
    assert "%3A" in text and ":-heir-to-the-strongest-style/chapter" in text    # the two spellings really occur
    slug = "sovereign-of-a-hundred-blades:-heir-to-the-strongest-style"
    result = parse(VORTEX)
    assert result["titles"][slug] == "Sovereign of a Hundred Blades: Heir to the Strongest Style"
    assert next(i for i in result["items"] if i["slug"] == slug)["title"] == result["titles"][slug]


@pytest.mark.parametrize("site", [HIVE, VORTEX, ASURA, KUN, MGEKO])
def test_scraper_slugs_equal_the_slugs_the_bot_stores_for_a_pasted_link(site):
    """If these ever disagreed, a followed novel would silently never be matched."""
    rules = RULES[site]
    for item in parse(site)["items"]:
        link = urljoin(rules["base_url"], item["href"])
        assert novel_links.parse_novel_link(link, RULES) == {"ok": True, "site_key": site, "slug": item["slug"]}


def test_badge_quiet_moment_with_no_locks_is_normal_not_a_problem():
    page = load(HIVE).replace("paid-chapter-row", "plain-row").replace("lucide-lock", "lucide-none")
    result = parse(HIVE, page)
    assert result["diagnostics"]["lock"] == "ok"
    assert not any(i["locked"] for i in result["items"])
    assert parsing.classify_site(result["diagnostics"], True, None) == "ok"


def test_badge_renamed_lock_class_is_flagged_missing_and_degraded():
    page = load(HIVE).replace("paid-chapter-row", "premium-row")       # icons stay, class is renamed
    result = parse(HIVE, page)
    assert result["diagnostics"]["lock"] == "missing"
    assert parsing.classify_site(result["diagnostics"], True, None) == "degraded"


def test_badge_renamed_icon_but_class_still_works_is_not_flagged():
    page = load(HIVE).replace("lucide-lock", "lucide-padlock")
    result = parse(HIVE, page)
    assert result["diagnostics"]["lock"] == "ok"
    assert {i["slug"] for i in result["items"] if i["locked"]} == HIVE_LOCKED


def test_duplicate_chapter_links_are_merged_and_locked_wins():
    page = ('<a class="newly-free-chapter-row" href="/series/x-novel/chapter-5">5</a>'
            '<a class="newly-free-chapter-row paid-chapter-row" href="/series/x-novel/chapter-5"><svg class="lucide-lock"></svg></a>')
    result = parsing.parse_page(page, RULES[HIVE])
    assert len(result["items"]) == 1 and result["items"][0]["locked"] is True


def test_links_to_other_sites_and_other_depths_are_not_chapter_items():
    page = ('<a href="https://evil.example/series/x-novel/chapter-5">x</a>'
            '<a href="/series/x-novel">series page</a>'
            '<a href="/series/x-novel/chapter-5/extra">too deep</a>'
            '<a href="https://hivetoons.org/series/y-novel/chapter-7">same site, absolute</a>')
    result = parsing.parse_page(page, RULES[HIVE])
    assert [(i["slug"], i["chapter"]) for i in result["items"]] == [("y-novel", 7.0)]


def test_slug_in_a_chapter_link_is_unquoted_and_lowercased():
    page = '<a href="/series/Some%3ANovel/chapter-3">x</a>'
    assert parsing.parse_page(page, RULES[HIVE])["items"][0]["slug"] == "some:novel"


def test_decimal_chapters_are_parsed():
    page = '<a href="/series/x-novel/chapter-12.5">x</a>'
    assert parsing.parse_page(page, RULES[HIVE])["items"][0]["chapter"] == 12.5


# ---------------------------------------------------------------------------------------------
# kunmanga
# ---------------------------------------------------------------------------------------------

def test_kunmanga_titles_come_from_all_three_real_title_layouts():
    result = parse(KUN)
    soup = BeautifulSoup(load(KUN), "lxml")
    expected = {}
    for selector in (".post-title h4 a", ".post-title h3 a", "h5.widget-title a"):
        matched = soup.select(selector)
        assert matched, f"fixture lost the {selector} layout"
        for anchor in matched:
            expected[novel_links.parse_novel_link(urljoin("https://www.kunmanga.online", anchor["href"]), RULES)["slug"]] = \
                novel_links.clean_title(anchor.get_text())
    assert result["titles"] == expected and len(expected) == 6
    assert result["diagnostics"] == {"items": 6, "titles": 6, "lock": "n/a"}


def test_kunmanga_items_are_never_locked_and_have_no_unlock_time():
    items = parse(KUN)["items"]
    assert len(items) == 6
    assert all(i["locked"] is False and i["unlock_at"] is None for i in items)


def test_the_old_h4_only_selector_would_have_missed_most_kunmanga_titles():
    """Regression for the spec's original selector (found against the real capture)."""
    rules = copy.deepcopy(RULES[KUN])
    rules["title_selector"] = ".post-title h4 a"
    assert len(parsing.parse_page(load(KUN), rules)["titles"]) == 2 < 6


# ---------------------------------------------------------------------------------------------
# mgeko (li blocks)
# ---------------------------------------------------------------------------------------------

def test_mgeko_items_titles_and_chapters():
    result = parse(MGEKO)
    assert [(i["slug"], i["chapter"]) for i in result["items"]] == [
        ("after-rebirth-i-used-mirror-reversal-for-vengeance", 74.0),
        ("turning-into-a-tentacle-monster-made-me-crazier", 50.0),
        ("the-apocalypse-is-here", 88.0),
    ]
    assert result["diagnostics"] == {"items": 3, "titles": 3, "lock": "n/a"}
    assert all(i["locked"] is False and i["unlock_at"] is None for i in result["items"])
    assert all(i["chapter_text"].startswith("Chapter ") for i in result["items"])
    assert result["titles"]["the-apocalypse-is-here"] == "The Apocalypse is Here"


def test_mgeko_blocks_without_a_chapter_row_are_not_update_items():
    soup = BeautifulSoup(load(MGEKO), "lxml")
    blocks = soup.select("li.novel-item")
    assert len(blocks) == 4 and sum(1 for b in blocks if b.select_one("h5.chapter-title")) == 3
    assert len(parse(MGEKO)["items"]) == 3


def test_mgeko_hyphenated_chapter_becomes_a_decimal():
    page = ('<li class="novel-item"><a class="list-body" href="/manga/x-novel/"></a>'
            '<h4 class="novel-title">X Novel</h4><h5 class="chapter-title">Chapter 74-5</h5></li>')
    item = parsing.parse_page(page, RULES[MGEKO])["items"][0]
    assert item["chapter"] == 74.5 and item["slug"] == "x-novel"


def test_mgeko_chapter_text_without_a_number_gives_a_none_chapter_the_compile_step_skips():
    page = ('<li class="novel-item"><a class="list-body" href="/manga/x-novel/"></a>'
            '<h4 class="novel-title">X</h4><h5 class="chapter-title">Prologue</h5></li>')
    assert parsing.parse_page(page, RULES[MGEKO])["items"][0]["chapter"] is None


# ---------------------------------------------------------------------------------------------
# shape and the old entry points
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("site", list(FIXTURE_FILES))
def test_parse_page_shape_is_the_same_for_every_site(site):
    result = parse(site)
    assert set(result) == {"items", "titles", "diagnostics"}
    assert {"items", "titles", "lock"} <= set(result["diagnostics"])
    for item in result["items"]:
        assert {"href", "chapter_text", "locked", "title", "slug", "chapter", "unlock_at"} <= set(item)
    assert result["diagnostics"]["items"] == len(result["items"])
    assert result["diagnostics"]["titles"] == len(result["titles"])


@pytest.mark.parametrize("site", list(FIXTURE_FILES))
def test_every_fixture_classifies_as_ok(site):
    assert parsing.classify_site(parse(site)["diagnostics"], True, None) == "ok"


def test_empty_or_unrelated_page_has_no_items():
    for site in (HIVE, KUN, MGEKO, ASURA):
        result = parsing.parse_page("<html><body><p>Just a moment...</p></body></html>", RULES[site])
        assert result["items"] == [] and result["diagnostics"]["items"] == 0
        assert parsing.classify_site(result["diagnostics"], True, None) == "failed"


def test_parse_page_does_not_change_the_old_parsers():
    assert parsing.parse_html(load(HIVE)) == [a["href"] for a in BeautifulSoup(load(HIVE), "lxml").find_all("a", href=True)
                                               if "chapter" in a["href"]]
    blocks = parsing.parse_html_li_block(load(MGEKO), RULES[MGEKO])
    assert len(blocks) == 3 and set(blocks[0]) == {"identity", "chapter_text"}


# ---------------------------------------------------------------------------------------------
# classify_site
# ---------------------------------------------------------------------------------------------

OK_DIAG = {"items": 20, "titles": 18, "lock": "ok"}


@pytest.mark.parametrize(
    "diagnostics, fetched_ok, exception, expected",
    [
        (OK_DIAG, True, None, "ok"),
        ({"items": 5, "titles": 5, "lock": "n/a"}, True, None, "ok"),
        (OK_DIAG, False, None, "failed"),                                    # fetch failed after retries
        (OK_DIAG, True, RuntimeError("boom"), "failed"),                     # unexpected exception
        (None, True, None, "failed"),
        ({}, True, None, "failed"),
        ({"items": 0, "titles": 0, "lock": "ok"}, True, None, "failed"),     # a latest page always has items
        ({"items": 20, "titles": 0, "lock": "ok"}, True, None, "degraded"),  # title selector found nothing
        ({"items": 20, "titles": 18, "lock": "missing"}, True, None, "degraded"),
        ({"items": 20, "titles": 0, "lock": "missing"}, True, None, "degraded"),
    ],
)
def test_classify_site(diagnostics, fetched_ok, exception, expected):
    assert parsing.classify_site(diagnostics, fetched_ok, exception) == expected


def test_classify_site_never_returns_skipped():
    # "skipped" (no rules / unsupported mode) is decided by the caller before any fetch
    assert parsing.classify_site(None, False, None) != "skipped"


# ---------------------------------------------------------------------------------------------
# validate_rules
# ---------------------------------------------------------------------------------------------

def test_the_real_rules_file_is_valid():
    assert parsing.validate_rules(RULES) == []


def test_the_docs_copy_of_the_rules_file_is_identical():
    assert json.loads((ROOT / "docs" / "site_parse_rules.json").read_text(encoding="utf-8")) == RULES


def test_every_real_site_has_a_display_name_and_a_title_source():
    for key, site in RULES.items():
        if site["mode"] == "json":
            continue
        assert site["display_name"].strip()
        assert site.get("title_selector") or site.get("title_field")


def test_only_asura_is_timing_capable_and_hive_vortex_are_badge_only():
    assert "lock_detection" in RULES[ASURA] and "lock_badge" not in RULES[ASURA]
    for site in (HIVE, VORTEX):
        assert "lock_badge" in RULES[site] and "lock_detection" not in RULES[site]
    for site in (KUN, MGEKO):
        assert "lock_badge" not in RULES[site] and "lock_detection" not in RULES[site]
    assert RULES[ASURA]["tier_labels"] == {"standard": "Free reader", "member": "Asura+ member"}


def mutated(site, **changes):
    rules = copy.deepcopy(RULES)
    for key, value in changes.items():
        if value is ...:
            rules[site].pop(key, None)
        else:
            rules[site][key] = value
    return rules


@pytest.mark.parametrize(
    "site, changes, fragment",
    [
        (HIVE, {"display_name": ...}, "display_name"),
        (HIVE, {"display_name": "  "}, "display_name"),
        (HIVE, {"base_url": "ftp://x"}, "base_url"),
        (HIVE, {"chapter_regex": "chapter[/-](\\d+"}, "chapter_regex"),
        (HIVE, {"chapter_regex": "chapter[/-]\\d+"}, "capture group"),
        (HIVE, {"chapter_regex": ...}, "chapter_regex is missing"),
        (ASURA, {"slug_hash_strip_regex": "(["}, "slug_hash_strip_regex"),
        (HIVE, {"novel_link_patterns": ...}, "novel_link_patterns"),
        (HIVE, {"novel_link_patterns": ["^/series/([^/]+)"]}, "named group 'slug'"),
        (HIVE, {"novel_link_patterns": ["^/series/(?P<slug>"]}, "novel_link_patterns"),
        (HIVE, {"title_selector": ...}, "no title source"),
        (HIVE, {"title_selector": "a[[["}, "title_selector"),
        (HIVE, {"segment_count": ...}, "segment_count"),
        (HIVE, {"segment_count": 1}, "segment_count"),
        (HIVE, {"mode": "xml"}, "unknown mode"),
        (HIVE, {"lock_detection": RULES[ASURA]["lock_detection"]}, "must not both be set"),
        (ASURA, {"lock_badge": RULES[HIVE]["lock_badge"]}, "must not both be set"),
        (ASURA, {"tier_labels": ...}, "tier_labels"),
        (ASURA, {"tier_labels": {"standard": "Free"}}, "tier_labels"),
        (HIVE, {"lock_badge": {"type": "html_class", "marker_selector": "svg"}}, "locked_class"),
        (HIVE, {"lock_badge": {"type": "html_class", "locked_class": "x"}}, "marker_selector"),
        (MGEKO, {"item_selector": ...}, "item_selector"),
        (MGEKO, {"identity_selector": "li[[["}, "identity_selector"),
        (MGEKO, {"chapter_url_template": "/reader/{slug}"}, "chapter_url_template"),
    ],
)
def test_validate_rules_reports_problems(site, changes, fragment):
    problems = parsing.validate_rules(mutated(site, **changes))
    assert any(fragment in p and site in p for p in problems), problems


def test_validate_rules_flags_a_lock_detection_site_missing_required_fields():
    rules = copy.deepcopy(RULES)
    del rules[ASURA]["lock_detection"]["fields"]["is_locked"]
    del rules[ASURA]["lock_detection"]["fields"]["public_url"]
    del rules[ASURA]["lock_detection"]["fields"]["slug"]
    problems = parsing.validate_rules(rules)
    assert any("is_locked" in p for p in problems) and any("public_url or slug" in p for p in problems)


def test_validate_rules_accepts_the_mangadex_placeholder_and_rejects_garbage_input():
    assert parsing.validate_rules({"x": {"mode": "json", "language_filter": "en"}}) == []
    assert parsing.validate_rules({}) != [] and parsing.validate_rules(None) != []
    assert parsing.validate_rules({"x": "not an object"}) != []


def test_fragment_is_dropped_and_lock_survives_the_merge():
    page = ('<a class="newly-free-chapter-row paid-chapter-row" href="/series/x-novel/chapter-5"><svg class="lucide-lock"></svg></a>'
            '<a class="newly-free-chapter-row" href="/series/x-novel/chapter-5#comment-99">comments</a>')
    result = parsing.parse_page(page, RULES[HIVE])
    assert [(i["href"], i["locked"]) for i in result["items"]] == [("/series/x-novel/chapter-5", True)]
