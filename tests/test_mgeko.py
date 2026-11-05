"""Tests for mgeko support (html_li_block mode):
  * parsing.parse_html_li_block pulls (href, chapter text) pairs out of <li> blocks
  * main.compilation_fnct_li_block turns them into update rows with reader URLs
  * process_website dispatches html_li_block sites to those two functions
"""

import json
from pathlib import Path

import pytest

import main
from src import parsing

ROOT = Path(__file__).resolve().parent.parent
MGEKO_URL = "https://www.mgeko.cc/jumbo/manga/"
RULES = json.loads((ROOT / "site_parse_rules.json").read_text(encoding="utf-8"))[MGEKO_URL]


def li(href, chapter):
    return (
        f'<li class="novel-item"><a class="list-body" href="{href}">'
        f'<h4 class="novel-title">ignored title</h4>'
        f'<h5 class="chapter-title">{chapter}</h5></a></li>'
    )


def page(*items):
    return f"<html><body><ul>{''.join(items)}</ul></body></html>"


@pytest.fixture
def novels():
    return {
        "the-archmages-restaurant": {"id": 1, "latest_chap": 156, "chapter_url": "old"},
        "counterattack-of-the-ugly-orcs": {"id": 2, "latest_chap": 1, "chapter_url": "old"},
    }


# ------------------------------------------------------------------ parse_html_li_block

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

def block(href, chapter):
    return {"identity": href, "chapter_text": chapter}


def test_new_chapter_produces_update_with_reader_url(novels):
    rows, skipped = main.compilation_fnct_li_block(
        [block("/manga/the-archmages-restaurant/", "Chapter 157-eng-li")], novels, RULES
    )
    assert skipped == []
    assert rows == [{
        "id": 1,
        "novel_name": "the-archmages-restaurant",
        "latest_chap": 157.0,
        "chapter_url": "https://www.mgeko.cc/reader/en/the-archmages-restaurant-chapter-157-eng-li/",
    }]


def test_sub_chapter_dash_becomes_a_decimal_but_url_keeps_the_dash(novels):
    rows, _ = main.compilation_fnct_li_block(
        [block("/manga/counterattack-of-the-ugly-orcs/", "Chapter 1-1-eng-li")], novels, RULES
    )
    assert rows[0]["latest_chap"] == 1.1
    assert rows[0]["chapter_url"].endswith("/reader/en/counterattack-of-the-ugly-orcs-chapter-1-1-eng-li/")


def test_no_update_when_chapter_is_not_newer(novels):
    rows, skipped = main.compilation_fnct_li_block(
        [block("/manga/the-archmages-restaurant/", "Chapter 156-eng-li")], novels, RULES
    )
    assert rows == [] and skipped == []


def test_highest_chapter_wins_when_a_novel_appears_twice(novels):
    rows, _ = main.compilation_fnct_li_block(
        [
            block("/manga/the-archmages-restaurant/", "Chapter 158-eng-li"),
            block("/manga/the-archmages-restaurant/", "Chapter 157-eng-li"),
        ],
        novels,
        RULES,
    )
    assert [r["latest_chap"] for r in rows] == [158.0]


def test_untracked_novels_are_skipped_not_updated(novels):
    rows, skipped = main.compilation_fnct_li_block(
        [block("/manga/someone-elses-novel/", "Chapter 40-eng-li")], novels, RULES
    )
    assert rows == []
    assert len(skipped) == 1


@pytest.mark.parametrize(
    "item",
    [
        block("/manga/the-archmages-restaurant/", "Prologue"),
        block("/manga/the-archmages-restaurant/", ""),
        block("", "Chapter 157-eng-li"),
        block(None, "Chapter 157-eng-li"),
        block("/manga/the-archmages-restaurant/", None),
    ],
)
def test_malformed_blocks_are_skipped_without_crashing(novels, item):
    rows, skipped = main.compilation_fnct_li_block([item], novels, RULES)
    assert rows == []
    assert len(skipped) == 1


def test_absolute_href_still_yields_the_slug(novels):
    rows, _ = main.compilation_fnct_li_block(
        [block("https://www.mgeko.cc/manga/the-archmages-restaurant/", "Chapter 157-eng-li")], novels, RULES
    )
    assert rows[0]["novel_name"] == "the-archmages-restaurant"


def test_empty_input_returns_nothing(novels):
    assert main.compilation_fnct_li_block([], novels, RULES) == ([], [])


# ------------------------------------------------------------------ process_website dispatch

def test_process_website_runs_the_li_block_pipeline_end_to_end(monkeypatch):
    html = page(
        li("/manga/the-archmages-restaurant/", "Chapter 157-eng-li"),
        li("/manga/untracked/", "Chapter 3-eng-li"),
    )
    monkeypatch.setattr(main.fetching, "fetch_html", lambda url: html)
    monkeypatch.setattr(
        main.database,
        "fetch_novels",
        lambda site_id: [
            {"id": 1, "novel_name": "the-archmages-restaurant", "latest_chap": 156, "chapter_url": "old"}
        ],
    )

    rows = main.process_website(5, MGEKO_URL, {MGEKO_URL: RULES})

    assert [(r["novel_name"], r["latest_chap"]) for r in rows] == [("the-archmages-restaurant", 157.0)]


def test_process_website_returns_empty_when_page_has_no_blocks(monkeypatch):
    monkeypatch.setattr(main.fetching, "fetch_html", lambda url: "<html></html>")
    assert main.process_website(5, MGEKO_URL, {MGEKO_URL: RULES}) == []
