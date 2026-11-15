"""main.py, everything except run(): retry, compilation, title updates, process_website results,
finding_updated_novels, and the delivery helpers (group_claimed, QueueDelivery, dispatch_queue).

compilation_fnct now consumes the items produced by parsing.parse_page, so most tests build their
input by running real HTML through parse_page (`items_from_links`) instead of handing in raw links.
"""

import copy
import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

import main
from src import parsing

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

CHAPTER_REGEX = r"chapter[/-](\d+(\.\d+)?)"

HIVE_URL = "https://hivetoons.org/"
ASURA_URL = "https://asurascans.com/"
VORTEX_URL = "https://vortexscans.org/"
KUN_URL = "https://www.kunmanga.online/"
MGEKO_URL = "https://www.mgeko.cc/jumbo/manga/"


@pytest.fixture
def hive_rules():
    # hivetoons / kunmanga / vortexscans style: series/{slug}/chapter-{n}
    return {"mode": "html", "segment_count": 3, "chapter_regex": CHAPTER_REGEX, "base_url": "https://hivetoons.org"}


@pytest.fixture
def asura_rules():
    # asurascans style: comics/{slug}-{hash}/chapter/{n}
    return {"mode": "html", "segment_count": 4, "chapter_regex": CHAPTER_REGEX,
            "slug_hash_strip_regex": r"-[a-f0-9]{8}$", "base_url": "https://asurascans.com"}


@pytest.fixture
def novels():
    return {
        "solo-leveling": {"id": 1, "latest_chap": 10, "chapter_url": "/series/solo-leveling/chapter-10", "web_id": "w1"},
        "tower-of-god": {"id": 2, "latest_chap": 50, "chapter_url": "/series/tower-of-god/chapter-50", "web_id": "w1"},
    }


def items_from_links(links, rules):
    """Run bare links through the real parse_page, exactly as a scraped page would be."""
    html = "".join(f'<a href="{link}">x</a>' for link in links)
    return parsing.parse_page(html, rules)["items"]


def item(name, number, **overrides):
    base = {"href": f"/series/{name}/chapter-{number}", "chapter_text": None, "locked": False, "title": None,
            "slug": name, "chapter": float(number), "unlock_at": None}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------

@pytest.fixture
def no_sleep(monkeypatch):
    """retry() sleeps 5s between attempts; skip that in tests."""
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)


def test_retry_returns_value_on_first_success(no_sleep):
    assert main.retry(lambda x: x * 2, 3, 21) == 42


def test_retry_succeeds_after_failures(no_sleep):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("boom")
        return "ok"

    assert main.retry(flaky, 3) == "ok"
    assert calls["n"] == 3


def test_retry_returns_empty_list_after_max_attempts(no_sleep):
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise RuntimeError("nope")

    assert main.retry(always_fails, 3) == []
    assert calls["n"] == 3


# ---------------------------------------------------------------------------
# compilation_fnct (html sites)
# ---------------------------------------------------------------------------

def test_compilation_no_update_when_nothing_newer(hive_rules, novels):
    before = copy.deepcopy(novels)
    items = items_from_links(["/series/solo-leveling/chapter-10", "/series/solo-leveling/chapter-9"], hive_rules)

    rows, skipped = main.compilation_fnct(items, novels, hive_rules)

    assert rows == [] and skipped == []
    assert novels == before


def test_compilation_simple_update(hive_rules, novels):
    items = items_from_links(["/series/solo-leveling/chapter-11"], hive_rules)

    rows, skipped = main.compilation_fnct(items, novels, hive_rules)

    assert len(rows) == 1 and skipped == []
    assert rows[0]["novel_name"] == "solo-leveling"
    assert rows[0]["latest_chap"] == 11
    assert rows[0]["chapter_url"] == "https://hivetoons.org/series/solo-leveling/chapter-11"


def test_compilation_does_not_mutate_the_fetched_novels(hive_rules, novels):
    before = copy.deepcopy(novels)
    main.compilation_fnct(items_from_links(["/series/solo-leveling/chapter-11"], hive_rules), novels, hive_rules)
    assert novels == before


@pytest.mark.parametrize(
    "links",
    [
        ["/series/solo-leveling/chapter-11", "/series/solo-leveling/chapter-12", "/series/solo-leveling/chapter-9"],
        ["/series/solo-leveling/chapter-12", "/series/solo-leveling/chapter-11", "/series/solo-leveling/chapter-9"],
        ["/series/solo-leveling/chapter-9", "/series/solo-leveling/chapter-11", "/series/solo-leveling/chapter-12"],
    ],
    ids=["max-in-middle", "max-first", "max-last"],
)
def test_compilation_picks_highest_chapter_regardless_of_order(hive_rules, novels, links):
    rows, _ = main.compilation_fnct(items_from_links(links, hive_rules), novels, hive_rules)

    assert len(rows) == 1
    assert rows[0]["latest_chap"] == 12
    assert rows[0]["chapter_url"] == "https://hivetoons.org/series/solo-leveling/chapter-12"


def test_compilation_handles_fractional_chapters(hive_rules, novels):
    novels["solo-leveling"]["latest_chap"] = 12
    rows, _ = main.compilation_fnct(items_from_links(["/series/solo-leveling/chapter-12.5"], hive_rules), novels, hive_rules)
    assert [r["latest_chap"] for r in rows] == [12.5]


def test_compilation_leaves_unrelated_novel_alone(hive_rules, novels):
    rows, _ = main.compilation_fnct(items_from_links(["/series/solo-leveling/chapter-11"], hive_rules), novels, hive_rules)
    assert [row["novel_name"] for row in rows] == ["solo-leveling"]


def test_compilation_updates_multiple_novels_in_database_order(hive_rules, novels):
    links = ["/series/tower-of-god/chapter-51", "/series/solo-leveling/chapter-11"]
    rows, _ = main.compilation_fnct(items_from_links(links, hive_rules), novels, hive_rules)
    assert [row["novel_name"] for row in rows] == ["solo-leveling", "tower-of-god"]


@pytest.mark.parametrize(
    "link",
    ["/series/solo-leveling", "/series/solo-leveling/chapter-11/extra", "/series/solo-leveling/about-us"],
    ids=["too-few-segments", "too-many-segments", "no-chapter-number"],
)
def test_links_that_are_not_chapter_links_never_become_updates(hive_rules, novels, link):
    rows, skipped = main.compilation_fnct(items_from_links([link], hive_rules), novels, hive_rules)
    assert rows == [] and skipped == []


def test_untracked_novels_are_reported_as_skipped_and_never_updated(hive_rules, novels):
    rows, skipped = main.compilation_fnct(items_from_links(["/series/unknown-novel/chapter-99"], hive_rules), novels, hive_rules)
    assert rows == [] and skipped == ["/series/unknown-novel/chapter-99"]


def test_compilation_empty_inputs(hive_rules, novels):
    assert main.compilation_fnct([], novels, hive_rules) == ([], [])
    items = items_from_links(["/series/solo-leveling/chapter-11"], hive_rules)
    assert main.compilation_fnct(items, {}, hive_rules) == ([], ["/series/solo-leveling/chapter-11"])


def test_compilation_row_has_exactly_the_documented_keys(hive_rules, novels):
    rows, _ = main.compilation_fnct(items_from_links(["/series/tower-of-god/chapter-51"], hive_rules), novels, hive_rules)
    assert set(rows[0]) == {"id", "novel_name", "latest_chap", "chapter_url", "display_title",
                            "access_state", "unlock_at", "web_id"}
    assert rows[0]["id"] == 2 and rows[0]["web_id"] == "w1"


def test_compilation_accepts_string_latest_chap_from_db(hive_rules, novels):
    novels["solo-leveling"]["latest_chap"] = "10"  # DB may hand back a string/numeric
    rows, _ = main.compilation_fnct(items_from_links(["/series/solo-leveling/chapter-11"], hive_rules), novels, hive_rules)
    assert len(rows) == 1


def test_compilation_asura_strips_the_hash_from_the_slug_but_keeps_it_in_the_url(asura_rules):
    fetched = {"solo-leveling": {"id": 1, "latest_chap": 10, "chapter_url": "old", "web_id": "w"}}
    items = items_from_links(["/comics/solo-leveling-1a2b3c4d/chapter/11"], asura_rules)

    rows, skipped = main.compilation_fnct(items, fetched, asura_rules)

    assert skipped == [] and len(rows) == 1
    assert rows[0]["novel_name"] == "solo-leveling" and rows[0]["latest_chap"] == 11
    # the real page link is the working URL (spec 7.2): the hash is part of it
    assert rows[0]["chapter_url"] == "https://asurascans.com/comics/solo-leveling-1a2b3c4d/chapter/11"


def test_compilation_vortex_apostrophe_slug_survives():
    rules = REAL_RULES[VORTEX_URL]
    fetched = {"the-hero's-ballad": {"id": 1, "latest_chap": 11, "chapter_url": "old", "web_id": "w"}}
    rows, _ = main.compilation_fnct(items_from_links(["/series/the-hero's-ballad/chapter-12"], rules), fetched, rules)
    assert rows[0]["chapter_url"] == "https://vortexscans.org/series/the-hero's-ballad/chapter-12"


def test_compilation_missing_base_url_raises(hive_rules, novels):
    rules = {k: v for k, v in hive_rules.items() if k != "base_url"}
    with pytest.raises(KeyError):
        main.compilation_fnct([item("solo-leveling", 11)], novels, rules)


def test_real_rules_base_urls_are_clean():
    """Guards against config typos: base_url is host-only and matches the site."""
    for site_url, rules in REAL_RULES.items():
        if rules["mode"] not in ("html", "html_li_block"):
            continue  # MangaDex has no base_url yet
        base = urlparse(rules["base_url"])
        assert base.scheme == "https"
        assert base.path == ""  # no trailing slash, no path
        assert base.netloc == urlparse(site_url).netloc


# ---- what the winning item carries into the row (title, lock state, unlock time) ----

def test_the_row_carries_title_lock_state_and_unlock_time_of_the_winning_item(hive_rules, novels):
    items = [
        item("solo-leveling", 12, locked=True, unlock_at="2026-11-10T17:00:00+00:00", title="Solo Leveling"),
        item("solo-leveling", 11, locked=False, title="older item, different title"),
    ]
    rows, _ = main.compilation_fnct(items, novels, hive_rules)
    assert rows[0]["access_state"] == "locked"
    assert rows[0]["unlock_at"] == "2026-11-10T17:00:00+00:00"
    assert rows[0]["display_title"] == "Solo Leveling"


def test_a_free_winner_is_free_even_if_a_lower_chapter_is_locked(hive_rules, novels):
    items = [item("solo-leveling", 11, locked=True), item("solo-leveling", 12, locked=False)]
    rows, _ = main.compilation_fnct(items, novels, hive_rules)
    assert rows[0]["latest_chap"] == 12 and rows[0]["access_state"] == "free"


def test_unknown_lock_state_becomes_access_state_unknown(hive_rules, novels):
    rows, _ = main.compilation_fnct([item("solo-leveling", 11, locked=None)], novels, hive_rules)
    assert rows[0]["access_state"] == "unknown" and rows[0]["unlock_at"] is None


def test_a_tie_between_two_items_is_won_by_the_first_one(hive_rules, novels):
    items = [item("solo-leveling", 11, locked=True, title="first"), item("solo-leveling", 11, locked=False, title="second")]
    rows, _ = main.compilation_fnct(items, novels, hive_rules)
    assert rows[0]["access_state"] == "locked" and rows[0]["display_title"] == "first"


def test_missing_or_blank_titles_become_none(hive_rules, novels):
    rows, _ = main.compilation_fnct([item("solo-leveling", 11, title="   ")], novels, hive_rules)
    assert rows[0]["display_title"] is None


def test_items_without_a_slug_or_chapter_are_skipped(hive_rules, novels):
    items = [item("solo-leveling", 11, slug=None), item("solo-leveling", 11, chapter=None)]
    rows, skipped = main.compilation_fnct(items, novels, hive_rules)
    assert rows == [] and len(skipped) == 2


# ---------------------------------------------------------------------------
# find_title_updates
# ---------------------------------------------------------------------------

def test_title_update_when_the_scraped_title_differs(novels):
    novels["solo-leveling"]["display_title"] = "Old Title"
    updates = main.find_title_updates({"solo-leveling": "New Title"}, novels)
    assert updates == [{"id": 1, "display_title": "New Title"}]


def test_no_title_update_when_it_is_unchanged_or_missing(novels):
    novels["solo-leveling"]["display_title"] = "Same"
    assert main.find_title_updates({"solo-leveling": "Same", "tower-of-god": None}, novels) == []
    assert main.find_title_updates({}, novels) == []
    assert main.find_title_updates({"solo-leveling": "   "}, novels) == []


def test_a_null_stored_title_gets_the_first_scraped_title(novels):
    assert main.find_title_updates({"tower-of-god": "Tower of God"}, novels) == [{"id": 2, "display_title": "Tower of God"}]


def test_titles_are_cleaned_before_comparing(novels):
    novels["solo-leveling"]["display_title"] = "Solo Leveling"
    assert main.find_title_updates({"solo-leveling": "  Solo \n Leveling "}, novels) == []


def test_novels_that_already_have_a_chapter_row_are_left_out(novels):
    updates = main.find_title_updates({"solo-leveling": "A", "tower-of-god": "B"}, novels, skip_ids={1})
    assert updates == [{"id": 2, "display_title": "B"}]


def test_titles_of_untracked_novels_are_ignored(novels):
    assert main.find_title_updates({"someone-elses": "Title"}, novels) == []


# ---------------------------------------------------------------------------
# process_website: the result object (fetching and the database are faked; parsing is real)
# ---------------------------------------------------------------------------

REAL_LINKS = json.loads((FIXTURES / "real_links.json").read_text(encoding="utf-8"))
REAL_RULES = json.loads((ROOT / "site_parse_rules.json").read_text(encoding="utf-8"))


def fixture_html(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def hive_novels(behind=1, with_titles=False):
    """The hive fixture's novels, each `behind` chapters behind the page."""
    page = parsing.parse_page(fixture_html("hive_latest.html"), REAL_RULES[HIVE_URL])
    best = {}
    for it in page["items"]:
        best[it["slug"]] = max(best.get(it["slug"], 0), it["chapter"])
    return [
        {"id": f"id-{i}", "novel_name": slug, "latest_chap": chap - behind, "chapter_url": "old",
         "display_title": page["titles"].get(slug) if with_titles else None, "web_id": "w-hive"}
        for i, (slug, chap) in enumerate(best.items())
    ]


@pytest.fixture
def site(monkeypatch, no_sleep):
    """Faked fetch + database for process_website; tests change `state` to simulate problems."""
    state = {"html": fixture_html("hive_latest.html"), "novels": hive_novels(), "fetch_calls": [], "fetch_error": None}

    def fetch_html(url):
        state["fetch_calls"].append(url)
        if state["fetch_error"]:
            raise state["fetch_error"]
        return state["html"]

    monkeypatch.setattr(main.fetching, "fetch_html", fetch_html)
    monkeypatch.setattr(main.database, "fetch_novels", lambda site_id: state["novels"])
    return state


def test_process_website_happy_path_returns_a_complete_result_object(site):
    result = main.process_website("w-hive", HIVE_URL, REAL_RULES)

    assert set(result) == {"site_id", "web_url", "status", "error", "rows", "title_updates", "skipped"}
    assert (result["site_id"], result["web_url"], result["status"], result["error"]) == ("w-hive", HIVE_URL, "ok", None)
    assert len(result["rows"]) == 7
    by_name = {r["novel_name"]: r for r in result["rows"]}
    assert by_name["crocodile"]["access_state"] == "locked" and by_name["crocodile"]["unlock_at"] is None
    assert by_name["lookism"]["access_state"] == "free" and by_name["lookism"]["latest_chap"] == 632
    assert by_name["lookism"]["chapter_url"] == "https://hivetoons.org/series/lookism/chapter-632"
    assert by_name["lookism"]["display_title"] == "Lookism"
    assert result["title_updates"] == []             # every title is written by its chapter row instead
    assert all(r["web_id"] == "w-hive" for r in result["rows"])


def test_title_updates_are_only_for_novels_without_a_chapter_row(site):
    site["novels"] = hive_novels(behind=0)            # up to date: no chapter rows, but titles are still NULL
    result = main.process_website("w-hive", HIVE_URL, REAL_RULES)
    assert result["rows"] == []
    assert len(result["title_updates"]) == 6          # eleceed has no title anchor on the page
    assert all(set(u) == {"id", "display_title"} and u["display_title"] for u in result["title_updates"])


def test_already_stored_titles_produce_no_title_updates(site):
    site["novels"] = hive_novels(behind=0, with_titles=True)
    result = main.process_website("w-hive", HIVE_URL, REAL_RULES)
    assert result["rows"] == [] and result["title_updates"] == []


def test_untracked_links_are_listed_in_skipped(site):
    site["novels"] = [n for n in hive_novels() if n["novel_name"] == "lookism"]
    result = main.process_website("w-hive", HIVE_URL, REAL_RULES)
    assert len(result["rows"]) == 1 and len(result["skipped"]) == 6


def test_skipped_links_are_printed_but_capped(site, capsys):
    page = "".join(f'<a href="/series/untracked-{i}/chapter-1">x</a>' for i in range(60)) + \
        '<a href="/series/lookism/chapter-5">x</a><a class="line-clamp-2" title="Lookism" href="/series/lookism">t</a>'
    site["html"] = page
    site["novels"] = [{"id": "n1", "novel_name": "lookism", "latest_chap": 1, "chapter_url": "old", "display_title": "Lookism", "web_id": "w"}]
    result = main.process_website("w-hive", HIVE_URL, REAL_RULES)
    printed = capsys.readouterr().out.splitlines()
    assert len(result["skipped"]) == 60
    assert len([line for line in printed if "untracked-" in line]) == main.MAX_PRINTED_SKIPPED
    assert any("and 35 more links not tracked" in line for line in printed)


def test_fetch_failure_is_a_failed_result_with_no_rows(site):
    site["fetch_error"] = ConnectionError("site down")
    result = main.process_website("w-hive", HIVE_URL, REAL_RULES)
    assert (result["status"], result["error"], result["rows"]) == ("failed", "fetch failed", [])
    assert site["fetch_calls"] == [HIVE_URL] * 3      # retried 3 times


def test_empty_page_is_a_failed_result(site):
    site["html"] = ""
    assert main.process_website("w-hive", HIVE_URL, REAL_RULES)["status"] == "failed"


def test_page_with_no_chapter_items_is_failed_and_the_database_is_not_even_asked(site, monkeypatch):
    site["html"] = "<html><body><p>Just a moment...</p></body></html>"
    monkeypatch.setattr(main.database, "fetch_novels", lambda site_id: pytest.fail("fetch_novels must not be called"))
    result = main.process_website("w-hive", HIVE_URL, REAL_RULES)
    assert (result["status"], result["error"]) == ("failed", "no chapter items found")


def test_a_site_with_no_tracked_novels_is_still_a_healthy_scrape(site):
    site["novels"] = []
    result = main.process_website("w-hive", HIVE_URL, REAL_RULES)
    assert (result["status"], result["rows"], result["title_updates"]) == ("ok", [], [])


def test_missing_titles_make_the_site_degraded_but_alerts_still_flow(site):
    site["html"] = fixture_html("hive_latest.html").replace('line-clamp-2', 'renamed-clamp')
    result = main.process_website("w-hive", HIVE_URL, REAL_RULES)
    assert (result["status"], result["error"]) == ("degraded", "no titles found")
    assert len(result["rows"]) == 7 and all(r["display_title"] is None for r in result["rows"])


def test_renamed_lock_class_makes_the_site_degraded(site):
    site["html"] = fixture_html("hive_latest.html").replace("paid-chapter-row", "premium-row")
    result = main.process_website("w-hive", HIVE_URL, REAL_RULES)
    assert (result["status"], result["error"]) == ("degraded", "lock detection missing")
    assert len(result["rows"]) == 7


def test_asura_island_is_read_for_locks_and_unlock_times(site):
    site["html"] = fixture_html("asura_latest.html")
    page = parsing.parse_page(site["html"], REAL_RULES[ASURA_URL])
    site["novels"] = [{"id": f"a{i}", "novel_name": slug, "latest_chap": 0, "chapter_url": "old", "display_title": None, "web_id": "w-asura"}
                      for i, slug in enumerate(sorted({it["slug"] for it in page["items"]}))]
    result = main.process_website("w-asura", ASURA_URL, REAL_RULES)

    assert result["status"] == "ok" and len(result["rows"]) == 5
    by_name = {r["novel_name"]: r for r in result["rows"]}
    assert by_name["emperor-of-solo-play"]["latest_chap"] == 92
    assert by_name["emperor-of-solo-play"]["access_state"] == "locked"
    assert by_name["emperor-of-solo-play"]["unlock_at"] == "2026-11-10T02:41:27.273007+00:00"
    assert by_name["emperor-of-solo-play"]["chapter_url"] == "https://asurascans.com/comics/emperor-of-solo-play-f212c582/chapter/92"
    assert by_name["cyberpunk-chivalry"]["access_state"] == "free"


def test_asura_without_its_island_still_alerts_but_is_degraded_and_unknown(site):
    site["html"] = fixture_html("asura_latest.html").replace("astro-island", "astro-gone")
    site["novels"] = [{"id": "a1", "novel_name": "emperor-of-solo-play", "latest_chap": 0, "chapter_url": "old", "display_title": None, "web_id": "w"}]
    result = main.process_website("w-asura", ASURA_URL, REAL_RULES)

    assert (result["status"], result["error"]) == ("degraded", "no titles found; lock detection missing")
    assert [(r["latest_chap"], r["access_state"], r["unlock_at"]) for r in result["rows"]] == [(92, "unknown", None)]


def test_vortex_and_kunmanga_fixtures_produce_locked_and_free_rows(site):
    site["html"] = fixture_html("vortex_latest.html")
    page = parsing.parse_page(site["html"], REAL_RULES[VORTEX_URL])
    site["novels"] = [{"id": f"v{i}", "novel_name": it["slug"], "latest_chap": it["chapter"] - 1, "chapter_url": "old",
                       "display_title": None, "web_id": "w-vortex"} for i, it in enumerate(page["items"])]
    result = main.process_website("w-vortex", VORTEX_URL, REAL_RULES)
    states = {r["novel_name"]: r["access_state"] for r in result["rows"]}
    assert states["sovereign-of-a-hundred-blades:-heir-to-the-strongest-style"] == "locked"
    assert states["the-hero's-ballad"] == "free" and len(states) == 7

    site["html"] = fixture_html("kun_latest.html")
    kun_page = parsing.parse_page(site["html"], REAL_RULES[KUN_URL])
    site["novels"] = [{"id": f"k{i}", "novel_name": it["slug"], "latest_chap": 0, "chapter_url": "old", "display_title": None, "web_id": "w"}
                      for i, it in enumerate(kun_page["items"])]
    kun = main.process_website("w-kun", KUN_URL, REAL_RULES)
    assert kun["status"] == "ok" and len(kun["rows"]) == 6 and all(r["access_state"] == "free" for r in kun["rows"])


def test_rules_without_segment_count_find_no_items_and_the_site_fails_cleanly(site):
    rules = {"https://bad.org/": {"mode": "html", "chapter_regex": CHAPTER_REGEX, "base_url": "https://bad.org"}}
    site["html"] = '<a href="/series/x/chapter-1">x</a>'
    assert main.process_website(1, "https://bad.org/", rules)["status"] == "failed"


# ---------------------------------------------------------------------------
# finding_updated_novels
# ---------------------------------------------------------------------------

def test_finding_updated_novels_returns_one_result_per_site(monkeypatch):
    monkeypatch.setattr(main, "process_website", lambda site_id, url, rules: {"site_id": site_id, "rows": [{"id": site_id}]})
    results = main.finding_updated_novels([1, 2, 3], ["a", "b", "c"], {})
    assert [r["site_id"] for r in results] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Real data: links captured from live sites, through the REAL parse_page and the REAL rules file
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("site_url", list(REAL_LINKS), ids=lambda u: u.split("//")[1].strip("/"))
def test_real_site_links_produce_expected_updates(site_url):
    links = REAL_LINKS[site_url]["links"]
    expected = REAL_LINKS[site_url]["expected_latest"]
    rules = REAL_RULES[site_url]

    # DB pretends to be one chapter behind everywhere (chapter-0 novels start at 0: nothing newer exists)
    fetched = {
        slug: {"id": i, "latest_chap": max(chap - 1, 0), "chapter_url": "old", "web_id": "w"}
        for i, (slug, chap) in enumerate(expected.items(), start=1)
    }

    rows, skipped = main.compilation_fnct(items_from_links(links, rules), fetched, rules)

    assert skipped == []  # every real link should be understood
    assert {row["novel_name"]: row["latest_chap"] for row in rows} == {
        slug: chap for slug, chap in expected.items() if chap >= 1
    }


def test_real_hivetoons_part_chapter_picks_next_whole_chapter():
    # real link "chapter-630_01" is a part-chapter; the regex reads it as 630, so 631 still wins
    rules = REAL_RULES[HIVE_URL]
    fetched = {"lookism": {"id": 1, "latest_chap": 629, "chapter_url": "old", "web_id": "w"}}

    rows, _ = main.compilation_fnct(items_from_links(REAL_LINKS[HIVE_URL]["links"], rules), fetched, rules)

    assert rows[0]["latest_chap"] == 631
    assert rows[0]["chapter_url"] == "https://hivetoons.org/series/lookism/chapter-631"


# ---------------------------------------------------------------------------
# group_claimed / QueueDelivery / dispatch_queue
# ---------------------------------------------------------------------------

def claimed_row(notification_id, telegram_id, name="lookism", chapter=5.0):
    return {"notification_id": notification_id, "telegram_user_id": telegram_id, "novel_name": name, "chapter": chapter,
            "chapter_url": f"https://x/{name}/{chapter}", "access_state": "free", "unlock_at": None}


def test_group_claimed_batches_rows_per_telegram_user_in_order():
    rows = [claimed_row("q1", 111), claimed_row("q2", 222), claimed_row("q3", 111, "solo")]
    grouped = main.group_claimed(rows)
    assert list(grouped) == [111, 222]
    assert [r["notification_id"] for r in grouped[111]] == ["q1", "q3"]
    assert [r["notification_id"] for r in grouped[222]] == ["q2"]


def test_group_claimed_skips_rows_without_a_telegram_id_and_handles_empty():
    assert main.group_claimed([]) == {}
    assert main.group_claimed([claimed_row("q1", None), {"notification_id": "q2"}]) == {}


def test_group_claimed_does_not_mutate_the_rows():
    rows = [claimed_row("q1", 111)]
    before = copy.deepcopy(rows)
    main.group_claimed(rows)
    assert rows == before


def test_queue_delivery_maps_each_callback_to_one_database_call(monkeypatch):
    calls = []
    monkeypatch.setattr(main.database, "ack_sent", lambda ids: calls.append(("ack_sent", ids)) or 2)
    monkeypatch.setattr(main.database, "release", lambda ids, text: calls.append(("release", ids, text)) or 1)
    monkeypatch.setattr(main.database, "mark_dead", lambda ids, text: calls.append(("mark_dead", ids, text)) or 1)
    monkeypatch.setattr(main.database, "handle_blocked_users", lambda chat: calls.append(("blocked", chat)) or {"ok": True})

    delivery = main.QueueDelivery()
    assert delivery.ack_sent(["a", "b"]) == 2
    assert delivery.release(["a"], "network") == 1
    assert delivery.mark_dead(["b"], "bad_request") == 1
    assert delivery.blocked(555, ["c"]) == {"ok": True}
    assert calls == [("ack_sent", ["a", "b"]), ("release", ["a"], "network"), ("mark_dead", ["b"], "bad_request"), ("blocked", 555)]


@pytest.fixture
def queue(monkeypatch):
    """claim_due hands out scripted passes; send_all records what it was given."""
    state = {"passes": [], "sent": [], "claim_limits": [], "summary": {"sent": 1, "blocked": 0, "bad_request": 0, "failed": 0, "retried": 0}}

    def claim_due(limit=500):
        state["claim_limits"].append(limit)
        return state["passes"].pop(0) if state["passes"] else []

    def send_all(batches, delivery, rules=None, now=None):
        state["sent"].append((batches, delivery))
        state["rules"] = rules
        state["now"] = now
        return state["summary"]

    monkeypatch.setattr(main.database, "claim_due", claim_due)
    monkeypatch.setattr(main.telegramMessenger, "send_all", send_all)
    return state


def test_dispatch_with_nothing_due_sends_nothing(queue):
    total = main.dispatch_queue()
    assert queue["sent"] == [] and queue["claim_limits"] == [main.CLAIM_BATCH]
    assert total == {"sent": 0, "blocked": 0, "bad_request": 0, "failed": 0, "retried": 0}


def test_dispatch_groups_by_user_and_passes_the_delivery_object(queue):
    queue["passes"] = [[claimed_row("q1", 111), claimed_row("q2", 222)]]
    delivery = main.QueueDelivery()
    main.dispatch_queue(delivery)
    batches, passed = queue["sent"][0]
    assert list(batches) == [111, 222] and passed is delivery


def test_dispatch_hands_the_site_rules_to_the_messenger_for_display_names(queue):
    queue["passes"] = [[claimed_row("q1", 111)]]
    rules = {"https://hivetoons.org/": {"display_name": "Hive Toons"}}
    main.dispatch_queue(rules=rules)
    assert queue["rules"] is rules


def test_dispatch_gives_the_messenger_the_runs_clock(queue, monkeypatch):
    from datetime import datetime, timezone
    fixed = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "utcnow", lambda: fixed)
    queue["passes"] = [[claimed_row("q1", 111)]]
    main.dispatch_queue()
    assert queue["now"] == fixed


def test_dispatch_defaults_to_a_queue_delivery(queue):
    queue["passes"] = [[claimed_row("q1", 111)]]
    main.dispatch_queue()
    assert isinstance(queue["sent"][0][1], main.QueueDelivery)


def test_dispatch_keeps_claiming_until_the_queue_is_empty_and_adds_up_the_summaries(queue):
    queue["passes"] = [[claimed_row("q1", 111)], [claimed_row("q2", 222)], [claimed_row("q3", 333)]]
    queue["summary"] = {"sent": 2, "blocked": [1], "bad_request": 0, "failed": [("x", "network")], "retried": 1}
    total = main.dispatch_queue()
    assert len(queue["sent"]) == 3 and len(queue["claim_limits"]) == 4
    assert total == {"sent": 6, "blocked": 3, "bad_request": 0, "failed": 3, "retried": 3}


def test_dispatch_is_capped_at_ten_passes(queue):
    queue["passes"] = [[claimed_row(f"q{i}", i)] for i in range(25)]
    main.dispatch_queue()
    assert len(queue["sent"]) == main.MAX_DISPATCH_PASSES == 10
    assert len(queue["passes"]) == 15            # the rest waits for the next run


def test_dispatch_lets_messenger_errors_propagate(queue, monkeypatch):
    queue["passes"] = [[claimed_row("q1", 111)]]
    monkeypatch.setattr(main.telegramMessenger, "send_all", lambda b, d, rules=None, now=None: (_ for _ in ()).throw(RuntimeError("no token")))
    with pytest.raises(RuntimeError):
        main.dispatch_queue()


# ---------------------------------------------------------------------------
# summaries
# ---------------------------------------------------------------------------

def test_merge_summaries_accepts_counts_and_collections():
    total = {}
    main.merge_summaries(total, {"sent": 2, "blocked": ["a", "b"], "failed": 1})
    main.merge_summaries(total, {"sent": 3, "bad_request": [("x", "y")]})
    assert total == {"sent": 5, "blocked": 2, "bad_request": 1, "failed": 1, "retried": 0}


def test_print_send_summary_prints_counts_only(capsys):
    main.print_send_summary({
        "sent": 5, "blocked": ["111", "222"], "bad_request": [("333", "Chat not found")],
        "failed": [("444", "network", "timeout detail")],
    })
    out = capsys.readouterr().out
    assert "sent=5" in out and "blocked=2" in out and "bad_request=1" in out and "failed=1" in out
    for private in ("111", "222", "333", "444", "Chat not found", "timeout detail"):
        assert private not in out


def test_print_send_summary_includes_retried_when_present(capsys):
    main.print_send_summary({"sent": 1, "blocked": 0, "bad_request": 0, "failed": 0, "retried": 4})
    assert "retried=4" in capsys.readouterr().out
