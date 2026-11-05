"""Tests for per-site isolation in main.py:
  * process_website skips sites with no rules or an unsupported mode BEFORE fetching
  * finding_updated_novels keeps going when one site raises an unexpected error
"""

import json
from pathlib import Path

import pytest

import main

ROOT = Path(__file__).resolve().parent.parent
REAL_RULES = json.loads((ROOT / "site_parse_rules.json").read_text(encoding="utf-8"))

GOOD_URL = "https://example.org/"
GOOD_RULES = {
    "mode": "html",
    "segment_count": 3,
    "chapter_regex": r"chapter[/-](\d+(\.\d+)?)",
    "base_url": "https://example.org",
}


@pytest.fixture
def pipeline(monkeypatch):
    """Happy-path fakes for fetch/parse/DB. `calls` records every URL that was fetched."""
    calls = {"fetch": []}
    monkeypatch.setattr(
        main.fetching, "fetch_html", lambda url: calls["fetch"].append(url) or "<html>"
    )
    monkeypatch.setattr(
        main.parsing, "parse_html", lambda html: ["/series/solo-leveling/chapter-11"]
    )
    monkeypatch.setattr(
        main.database,
        "fetch_novels",
        lambda site_id: [
            {"id": 1, "novel_name": "solo-leveling", "latest_chap": 10, "chapter_url": "old"}
        ],
    )
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)
    return calls


# ---------------------------------------------------------------- process_website guard

def test_process_website_still_handles_a_supported_html_site(pipeline):
    rows = main.process_website(1, GOOD_URL, {GOOD_URL: GOOD_RULES})

    assert [row["latest_chap"] for row in rows] == [11]
    assert pipeline["fetch"] == [GOOD_URL]


def test_process_website_skips_a_site_with_no_rules_before_fetching(pipeline, capsys):
    rows = main.process_website(1, "https://unknown.org/", {GOOD_URL: GOOD_RULES})

    assert rows == []
    assert pipeline["fetch"] == []  # no network call was made
    assert "no parse rules" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["json", None], ids=["json", "no-mode"])
def test_process_website_skips_unsupported_modes_before_fetching(pipeline, capsys, mode):
    rules = {"https://x.org/": {"chapter_regex": "x", "base_url": "https://x.org"}}
    if mode is not None:
        rules["https://x.org/"]["mode"] = mode

    assert main.process_website(1, "https://x.org/", rules) == []
    assert pipeline["fetch"] == []
    assert "not supported yet" in capsys.readouterr().out


def test_real_non_html_sites_are_skipped_without_any_fetch(pipeline):
    skipped_urls = [url for url, rules in REAL_RULES.items() if rules["mode"] not in main.SUPPORTED_MODES]
    assert skipped_urls  # mangadex, until it is implemented

    for url in skipped_urls:
        assert main.process_website(1, url, REAL_RULES) == []
    assert pipeline["fetch"] == []


def test_real_supported_sites_are_not_skipped(pipeline):
    html_urls = [url for url, rules in REAL_RULES.items() if rules["mode"] in main.SUPPORTED_MODES]
    assert len(html_urls) == 5  # four html sites + mgeko (html_li_block)

    for url in html_urls:
        main.process_website(1, url, REAL_RULES)
    assert pipeline["fetch"] == html_urls


# ---------------------------------------------------------------- finding_updated_novels isolation

def test_one_crashing_site_does_not_stop_the_others(monkeypatch, capsys):
    def fake_process_website(site_id, url, rules):
        if site_id == 2:
            raise RuntimeError("boom from site 2")
        return [{"id": site_id}]

    monkeypatch.setattr(main, "process_website", fake_process_website)

    result = main.finding_updated_novels([1, 2, 3], ["a", "b", "c"], {})

    assert result == [{"id": 1}, {"id": 3}]
    captured = capsys.readouterr()
    assert "2: b: unexpected error" in captured.out
    assert "boom from site 2" in captured.err  # the traceback is still printed


def test_every_site_crashing_returns_an_empty_list(monkeypatch):
    def always_crash(site_id, url, rules):
        raise ValueError("nope")

    monkeypatch.setattr(main, "process_website", always_crash)

    assert main.finding_updated_novels([1, 2], ["a", "b"], {}) == []


def test_keyboard_interrupt_is_not_swallowed(monkeypatch):
    def interrupted(site_id, url, rules):
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "process_website", interrupted)

    with pytest.raises(KeyboardInterrupt):
        main.finding_updated_novels([1], ["a"], {})


def test_a_site_with_broken_rules_no_longer_kills_the_run(pipeline):
    """html mode but no segment_count: compilation_fnct raises KeyError for this site only."""
    bad_url = "https://bad.org/"
    rules = {
        bad_url: {"mode": "html", "chapter_regex": r"chapter[/-](\d+)", "base_url": "https://bad.org"},
        GOOD_URL: GOOD_RULES,
    }

    rows = main.finding_updated_novels([1, 2], [bad_url, GOOD_URL], rules)

    assert [row["latest_chap"] for row in rows] == [11]  # the good site still delivered
