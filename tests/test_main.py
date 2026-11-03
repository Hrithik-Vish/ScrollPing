import copy
import json
from pathlib import Path

import pytest

import main

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
# Fixtures: fresh copies per test, because compilation_fnct MUTATES fetched_novels
# ---------------------------------------------------------------------------

CHAPTER_REGEX = r"chapter[/-](\d+(\.\d+)?)"


@pytest.fixture
def hive_rules():
    # hivetoons / kunmanga / vortexscans style: series/{slug}/chapter-{n}
    return {"mode": "html", "segment_count": 3, "chapter_regex": CHAPTER_REGEX}


@pytest.fixture
def asura_rules():
    # asurascans style: comics/{slug}-{hash}/chapter/{n}
    return {
        "mode": "html",
        "segment_count": 4,
        "chapter_regex": CHAPTER_REGEX,
        "slug_hash_strip_regex": r"-[a-f0-9]{8}$",
    }


@pytest.fixture
def novels():
    return {
        "solo-leveling": {
            "id": 1,
            "latest_chap": 10,
            "chapter_url": "/series/solo-leveling/chapter-10",
        },
        "tower-of-god": {
            "id": 2,
            "latest_chap": 50,
            "chapter_url": "/series/tower-of-god/chapter-50",
        },
    }


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
# compilation_fnct
# ---------------------------------------------------------------------------

def test_compilation_no_update_when_nothing_newer(hive_rules, novels):
    before = copy.deepcopy(novels)
    parsing_data = ["/series/solo-leveling/chapter-10", "/series/solo-leveling/chapter-9"]

    updated, skipped = main.compilation_fnct(parsing_data, novels, hive_rules)

    assert updated == []
    assert skipped == []
    assert novels == before


def test_compilation_simple_update(hive_rules, novels):
    parsing_data = ["/series/solo-leveling/chapter-11"]

    updated, skipped = main.compilation_fnct(parsing_data, novels, hive_rules)

    assert len(updated) == 1
    assert updated[0]["novel_name"] == "solo-leveling"
    assert updated[0]["latest_chap"] == 11
    assert updated[0]["chapter_url"] == "/series/solo-leveling/chapter-11"
    assert novels["solo-leveling"]["latest_chap"] == 11  # mutated in place


@pytest.mark.parametrize(
    "parsing_data",
    [
        ["/series/solo-leveling/chapter-11", "/series/solo-leveling/chapter-12", "/series/solo-leveling/chapter-9"],
        ["/series/solo-leveling/chapter-12", "/series/solo-leveling/chapter-11", "/series/solo-leveling/chapter-9"],
        ["/series/solo-leveling/chapter-9", "/series/solo-leveling/chapter-11", "/series/solo-leveling/chapter-12"],
    ],
    ids=["max-in-middle", "max-first", "max-last"],
)
def test_compilation_picks_highest_chapter_regardless_of_order(hive_rules, novels, parsing_data):
    updated, _ = main.compilation_fnct(parsing_data, novels, hive_rules)

    assert len(updated) == 1
    assert updated[0]["latest_chap"] == 12
    assert updated[0]["chapter_url"] == "/series/solo-leveling/chapter-12"


def test_compilation_handles_fractional_chapters(hive_rules, novels):
    novels["solo-leveling"]["latest_chap"] = 12

    updated, _ = main.compilation_fnct(["/series/solo-leveling/chapter-12.5"], novels, hive_rules)

    assert len(updated) == 1
    assert updated[0]["latest_chap"] == 12.5


def test_compilation_leaves_unrelated_novel_alone(hive_rules, novels):
    updated, _ = main.compilation_fnct(["/series/solo-leveling/chapter-11"], novels, hive_rules)

    assert [row["novel_name"] for row in updated] == ["solo-leveling"]
    assert novels["tower-of-god"]["latest_chap"] == 50


def test_compilation_updates_multiple_novels(hive_rules, novels):
    parsing_data = ["/series/solo-leveling/chapter-11", "/series/tower-of-god/chapter-51"]

    updated, _ = main.compilation_fnct(parsing_data, novels, hive_rules)

    assert {row["novel_name"] for row in updated} == {"solo-leveling", "tower-of-god"}


@pytest.mark.parametrize(
    "bad_link",
    [
        "/series/solo-leveling",                    # too few segments
        "/series/solo-leveling/chapter-11/extra",   # too many segments
        "/series/solo-leveling/about-us",           # no chapter number
        "/series/unknown-novel/chapter-99",         # novel not in fetched_novels
    ],
    ids=["too-few-segments", "too-many-segments", "no-chapter-match", "unknown-novel"],
)
def test_compilation_skips_bad_links(hive_rules, novels, bad_link):
    before = copy.deepcopy(novels)

    updated, skipped = main.compilation_fnct([bad_link], novels, hive_rules)

    assert updated == []
    assert skipped == [bad_link]
    assert novels == before


def test_compilation_empty_inputs(hive_rules, novels):
    assert main.compilation_fnct([], novels, hive_rules) == ([], [])
    assert main.compilation_fnct(["/series/solo-leveling/chapter-11"], {}, hive_rules) == (
        [],
        ["/series/solo-leveling/chapter-11"],
    )


def test_compilation_row_has_exact_keys_and_original_id(hive_rules, novels):
    updated, _ = main.compilation_fnct(["/series/tower-of-god/chapter-51"], novels, hive_rules)

    assert set(updated[0].keys()) == {"id", "novel_name", "latest_chap", "chapter_url"}
    assert updated[0]["id"] == 2


def test_compilation_accepts_string_latest_chap_from_db(hive_rules, novels):
    novels["solo-leveling"]["latest_chap"] = "10"  # DB may hand back a string/numeric

    updated, _ = main.compilation_fnct(["/series/solo-leveling/chapter-11"], novels, hive_rules)

    assert len(updated) == 1


def test_compilation_asura_strips_hash_and_reads_chapter(asura_rules):
    fetched = {"solo-leveling": {"id": 1, "latest_chap": 10, "chapter_url": "old"}}

    updated, skipped = main.compilation_fnct(
        ["/comics/solo-leveling-1a2b3c4d/chapter/11"], fetched, asura_rules
    )

    assert skipped == []
    assert len(updated) == 1
    assert updated[0]["novel_name"] == "solo-leveling"
    assert updated[0]["latest_chap"] == 11


# ---------------------------------------------------------------------------
# process_website (fetching / parsing / database are all faked)
# ---------------------------------------------------------------------------

@pytest.fixture
def site_parse_rules(hive_rules):
    return {"https://example.org/": hive_rules}


@pytest.fixture
def fake_pipeline(monkeypatch, no_sleep):
    """Happy-path fakes; individual tests override one piece to simulate a failure."""
    monkeypatch.setattr(main.fetching, "fetch_html", lambda url: "<html>", raising=False)
    monkeypatch.setattr(
        main.parsing, "parse_html", lambda html: ["/series/solo-leveling/chapter-11"], raising=False
    )
    monkeypatch.setattr(
        main.database,
        "fetch_novels",
        lambda site_id: [
            {"id": 1, "novel_name": "solo-leveling", "latest_chap": 10, "chapter_url": "old"}
        ],
        raising=False,
    )


def test_process_website_happy_path(fake_pipeline, site_parse_rules):
    rows = main.process_website(1, "https://example.org/", site_parse_rules)

    assert len(rows) == 1
    assert rows[0]["novel_name"] == "solo-leveling"
    assert rows[0]["latest_chap"] == 11


def test_process_website_returns_empty_when_fetch_fails(monkeypatch, fake_pipeline, site_parse_rules):
    def boom(url):
        raise ConnectionError("site down")

    monkeypatch.setattr(main.fetching, "fetch_html", boom, raising=False)

    assert main.process_website(1, "https://example.org/", site_parse_rules) == []


def test_process_website_returns_empty_when_nothing_parsed(monkeypatch, fake_pipeline, site_parse_rules):
    monkeypatch.setattr(main.parsing, "parse_html", lambda html: [], raising=False)

    assert main.process_website(1, "https://example.org/", site_parse_rules) == []


def test_process_website_returns_empty_when_no_novels_for_site(monkeypatch, fake_pipeline, site_parse_rules):
    monkeypatch.setattr(main.database, "fetch_novels", lambda site_id: [], raising=False)

    assert main.process_website(1, "https://example.org/", site_parse_rules) == []


# ---------------------------------------------------------------------------
# finding_updated_novels
# ---------------------------------------------------------------------------

def test_finding_updated_novels_combines_all_sites(monkeypatch):
    per_site = {1: [{"id": 1}], 2: [], 3: [{"id": 7}, {"id": 8}]}
    monkeypatch.setattr(
        main, "process_website", lambda site_id, url, rules: per_site[site_id]
    )

    result = main.finding_updated_novels([1, 2, 3], ["a", "b", "c"], {})

    assert result == [{"id": 1}, {"id": 7}, {"id": 8}]


# ---------------------------------------------------------------------------
# Real data: links captured from live sites via parse_html, run through the
# REAL site_parse_rules.json from the repo (not a hand-built rules dict)
# ---------------------------------------------------------------------------

REAL_LINKS = json.loads((FIXTURES / "real_links.json").read_text(encoding="utf-8"))
REAL_RULES = json.loads((ROOT / "site_parse_rules.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("site_url", list(REAL_LINKS), ids=lambda u: u.split("//")[1].strip("/"))
def test_real_site_links_produce_expected_updates(site_url):
    links = REAL_LINKS[site_url]["links"]
    expected = REAL_LINKS[site_url]["expected_latest"]

    # DB pretends to be one chapter behind everywhere (chapter-0 novels start at 0: nothing newer exists)
    fetched = {
        slug: {"id": i, "latest_chap": max(chap - 1, 0), "chapter_url": "old"}
        for i, (slug, chap) in enumerate(expected.items(), start=1)
    }

    updated, skipped = main.compilation_fnct(links, fetched, REAL_RULES[site_url])

    assert skipped == []  # every real link should be understood
    assert {row["novel_name"]: row["latest_chap"] for row in updated} == {
        slug: chap for slug, chap in expected.items() if chap >= 1
    }


def test_real_hivetoons_part_chapter_picks_next_whole_chapter():
    # real link "chapter-630_01" is a part-chapter; the regex reads it as 630, so 631 still wins
    links = REAL_LINKS["https://hivetoons.org/"]["links"]
    fetched = {"lookism": {"id": 1, "latest_chap": 629, "chapter_url": "old"}}

    updated, _ = main.compilation_fnct(links, fetched, REAL_RULES["https://hivetoons.org/"])

    assert updated[0]["latest_chap"] == 631
    assert updated[0]["chapter_url"] == "/series/lookism/chapter-631"
