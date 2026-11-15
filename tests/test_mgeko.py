"""mgeko support (html_li_block mode):
  * parsing.parse_html_li_block pulls (href, chapter text) pairs out of <li> blocks
  * parsing.parse_page turns the same blocks into items (slug, chapter, title)
  * main.compilation_fnct_li_block turns those items into update rows with reader URLs
  * process_website dispatches html_li_block sites to those functions
"""

import json
from pathlib import Path

import pytest

import main
from src import parsing

ROOT = Path(__file__).resolve().parent.parent
MGEKO_URL = "https://www.mgeko.cc/jumbo/manga/"
RULES = json.loads((ROOT / "site_parse_rules.json").read_text(encoding="utf-8"))[MGEKO_URL]


def li(href, chapter, title="Some Title"):
    return (
        f'<li class="novel-item"><a class="list-body" href="{href}">'
        f'<h4 class="novel-title">{title}</h4>'
        f'<h5 class="chapter-title">{chapter}</h5></a></li>'
    )


def page(*items):
    return f"<html><body><ul>{''.join(items)}</ul></body></html>"


@pytest.fixture
def novels():
    return {
        "the-archmages-restaurant": {"id": 1, "latest_chap": 156, "chapter_url": "old", "web_id": "w"},
        "counterattack-of-the-ugly-orcs": {"id": 2, "latest_chap": 1, "chapter_url": "old", "web_id": "w"},
    }


def items_of(*blocks):
    return parsing.parse_page(page(*blocks), RULES)["items"]


# ------------------------------------------------------------------ parse_html_li_block (unchanged)

def test_parse_returns_href_and_chapter_text():
    html = page(li("/manga/the-archmages-restaurant/", "Chapter 157-eng-li"))
    assert parsing.parse_html_li_block(html, RULES) == [
        {"identity": "/manga/the-archmages-restaurant/", "chapter_text": "Chapter 157-eng-li"}
    ]


def test_parse_keeps_every_block_in_order():
    html = page(li("/manga/a/", "Chapter 1-eng-li"), li("/manga/b/", "Chapter 2-eng-li"))
    assert [b["identity"] for b in parsing.parse_html_li_block(html, RULES)] == ["/manga/a/", "/manga/b/"]


def test_parse_skips_blocks_missing_a_part():
    no_chapter = '<li class="novel-item"><a class="list-body" href="/manga/x/"></a></li>'
    no_link = '<li class="novel-item"><h5 class="chapter-title">Chapter 5-eng-li</h5></li>'
    empty_text = li("/manga/y/", "   ")
    html = page(no_chapter, no_link, empty_text, li("/manga/ok/", "Chapter 9-eng-li"))
    assert [b["identity"] for b in parsing.parse_html_li_block(html, RULES)] == ["/manga/ok/"]


def test_parse_empty_and_unrelated_html_returns_empty_list():
    assert parsing.parse_html_li_block("", RULES) == []
    assert parsing.parse_html_li_block("<html><body><p>nothing</p></body></html>", RULES) == []


# ------------------------------------------------------------------ compilation_fnct_li_block

def test_new_chapter_produces_update_with_reader_url(novels):
    rows, skipped = main.compilation_fnct_li_block(
        items_of(li("/manga/the-archmages-restaurant/", "Chapter 157-eng-li", "The Archmage's Restaurant")), novels, RULES
    )
    assert skipped == []
    assert rows == [{
        "id": 1,
        "novel_name": "the-archmages-restaurant",
        "latest_chap": 157.0,
        "chapter_url": "https://www.mgeko.cc/reader/en/the-archmages-restaurant-chapter-157-eng-li/",
        "display_title": "The Archmage's Restaurant",
        "access_state": "free",
        "unlock_at": None,
        "web_id": "w",
    }]


def test_sub_chapter_dash_becomes_a_decimal_but_url_keeps_the_dash(novels):
    rows, _ = main.compilation_fnct_li_block(
        items_of(li("/manga/counterattack-of-the-ugly-orcs/", "Chapter 1-1-eng-li")), novels, RULES
    )
    assert rows[0]["latest_chap"] == 1.1
    assert rows[0]["chapter_url"].endswith("/reader/en/counterattack-of-the-ugly-orcs-chapter-1-1-eng-li/")


def test_no_update_when_chapter_is_not_newer(novels):
    rows, skipped = main.compilation_fnct_li_block(
        items_of(li("/manga/the-archmages-restaurant/", "Chapter 156-eng-li")), novels, RULES
    )
    assert rows == [] and skipped == []


def test_highest_chapter_wins_when_a_novel_appears_twice(novels):
    rows, _ = main.compilation_fnct_li_block(
        items_of(
            li("/manga/the-archmages-restaurant/", "Chapter 158-eng-li"),
            li("/manga/the-archmages-restaurant/", "Chapter 157-eng-li"),
        ),
        novels,
        RULES,
    )
    assert [r["latest_chap"] for r in rows] == [158.0]


def test_untracked_novels_are_skipped_not_updated(novels):
    rows, skipped = main.compilation_fnct_li_block(
        items_of(li("/manga/someone-elses-novel/", "Chapter 40-eng-li")), novels, RULES
    )
    assert rows == []
    assert len(skipped) == 1 and "someone-elses-novel" in skipped[0] and "Chapter 40-eng-li" in skipped[0]


@pytest.mark.parametrize(
    "block",
    [
        li("/manga/the-archmages-restaurant/", "Prologue"),
        li("/manga/the-archmages-restaurant/", ""),
        li("", "Chapter 157-eng-li"),
        '<li class="novel-item"><a class="list-body" href="/manga/the-archmages-restaurant/"></a></li>',
    ],
    ids=["no-number", "empty-text", "empty-href", "no-chapter-row"],
)
def test_malformed_blocks_never_become_updates_and_never_crash(novels, block):
    rows, _ = main.compilation_fnct_li_block(items_of(block), novels, RULES)
    assert rows == []


def test_absolute_href_still_yields_the_slug(novels):
    rows, _ = main.compilation_fnct_li_block(
        items_of(li("https://www.mgeko.cc/manga/the-archmages-restaurant/", "Chapter 157-eng-li")), novels, RULES
    )
    assert rows[0]["novel_name"] == "the-archmages-restaurant"


def test_empty_input_returns_nothing(novels):
    assert main.compilation_fnct_li_block([], novels, RULES) == ([], [])


# ------------------------------------------------------------------ process_website dispatch

def test_process_website_runs_the_li_block_pipeline_end_to_end(monkeypatch):
    html = page(
        li("/manga/the-archmages-restaurant/", "Chapter 157-eng-li", "The Archmage's Restaurant"),
        li("/manga/untracked/", "Chapter 3-eng-li"),
    )
    monkeypatch.setattr(main.fetching, "fetch_html", lambda url: html)
    monkeypatch.setattr(
        main.database,
        "fetch_novels",
        lambda site_id: [
            {"id": 1, "novel_name": "the-archmages-restaurant", "latest_chap": 156, "chapter_url": "old", "web_id": "w-mgeko"}
        ],
    )

    result = main.process_website(5, MGEKO_URL, {MGEKO_URL: RULES})

    assert result["status"] == "ok"
    assert [(r["novel_name"], r["latest_chap"]) for r in result["rows"]] == [("the-archmages-restaurant", 157.0)]
    assert result["rows"][0]["display_title"] == "The Archmage's Restaurant"
    assert result["rows"][0]["web_id"] == "w-mgeko"
    assert len(result["skipped"]) == 1


def test_process_website_with_a_page_that_has_no_blocks_is_failed(monkeypatch):
    monkeypatch.setattr(main.fetching, "fetch_html", lambda url: "<html></html>")
    result = main.process_website(5, MGEKO_URL, {MGEKO_URL: RULES})
    assert result["status"] == "failed" and result["rows"] == []
