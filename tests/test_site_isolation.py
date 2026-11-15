"""Per-site isolation in main.py:
  * process_website returns a 'skipped' result for sites with no rules or an unsupported mode,
    BEFORE any fetching
  * finding_updated_novels keeps going when one site raises, turning it into a 'failed' result
    that carries only the error class name
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
    "title_selector": "a.title",
    "novel_link_patterns": [r"^/series/(?P<slug>[^/]+)/?$"],
}
GOOD_PAGE = (
    '<html><body><a href="/series/solo-leveling/chapter-11">x</a>'
    '<a class="title" href="/series/solo-leveling">Solo Leveling</a></body></html>'
)


@pytest.fixture
def pipeline(monkeypatch):
    """Happy-path fakes for fetch/DB (parsing is real). `calls` records every URL that was fetched."""
    calls = {"fetch": []}
    monkeypatch.setattr(main.fetching, "fetch_html", lambda url: calls["fetch"].append(url) or GOOD_PAGE)
    monkeypatch.setattr(
        main.database,
        "fetch_novels",
        lambda site_id: [{"id": 1, "novel_name": "solo-leveling", "latest_chap": 10, "chapter_url": "old", "web_id": "w"}],
    )
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)
    return calls


# ---------------------------------------------------------------- process_website guard

def test_process_website_still_handles_a_supported_html_site(pipeline):
    result = main.process_website(1, GOOD_URL, {GOOD_URL: GOOD_RULES})

    assert result["status"] == "ok"
    assert [row["latest_chap"] for row in result["rows"]] == [11]
    assert pipeline["fetch"] == [GOOD_URL]


def test_process_website_skips_a_site_with_no_rules_before_fetching(pipeline, capsys):
    result = main.process_website(1, "https://unknown.org/", {GOOD_URL: GOOD_RULES})

    assert result["status"] == "skipped" and result["rows"] == [] and result["error"] is None
    assert pipeline["fetch"] == []  # no network call was made
    assert "no parse rules" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["json", None], ids=["json", "no-mode"])
def test_process_website_skips_unsupported_modes_before_fetching(pipeline, capsys, mode):
    rules = {"https://x.org/": {"chapter_regex": "x", "base_url": "https://x.org"}}
    if mode is not None:
        rules["https://x.org/"]["mode"] = mode

    result = main.process_website(1, "https://x.org/", rules)

    assert result["status"] == "skipped" and result["rows"] == []
    assert pipeline["fetch"] == []
    assert "not supported yet" in capsys.readouterr().out


def test_real_non_html_sites_are_skipped_without_any_fetch(pipeline):
    skipped_urls = [url for url, rules in REAL_RULES.items() if rules["mode"] not in main.SUPPORTED_MODES]
    assert skipped_urls  # mangadex, until it is implemented

    for url in skipped_urls:
        assert main.process_website(1, url, REAL_RULES)["status"] == "skipped"
    assert pipeline["fetch"] == []


def test_real_supported_sites_are_not_skipped(pipeline):
    html_urls = [url for url, rules in REAL_RULES.items() if rules["mode"] in main.SUPPORTED_MODES]
    assert len(html_urls) == 5  # four html sites + mgeko (html_li_block)

    for url in html_urls:
        assert main.process_website(1, url, REAL_RULES)["status"] != "skipped"
    assert pipeline["fetch"] == html_urls


# ---------------------------------------------------------------- finding_updated_novels isolation

def result_for(site_id):
    return {"site_id": site_id, "status": "ok", "error": None, "rows": [{"id": site_id}], "title_updates": [], "skipped": []}


def test_one_crashing_site_does_not_stop_the_others(monkeypatch, capsys):
    def fake_process_website(site_id, url, rules):
        if site_id == 2:
            raise RuntimeError("boom from site 2")
        return result_for(site_id)

    monkeypatch.setattr(main, "process_website", fake_process_website)

    results = main.finding_updated_novels([1, 2, 3], ["a", "b", "c"], {})

    assert [r["site_id"] for r in results] == [1, 2, 3]
    assert [r["status"] for r in results] == ["ok", "failed", "ok"]
    captured = capsys.readouterr()
    assert "2: b: unexpected error" in captured.out
    assert "boom from site 2" in captured.err  # the traceback is still printed to the log, never stored


def test_a_crashed_site_result_carries_only_the_error_class_name(monkeypatch):
    def crash(site_id, url, rules):
        raise ValueError("secret detail https://private.example/token=abc")

    monkeypatch.setattr(main, "process_website", crash)

    [result] = main.finding_updated_novels([7], ["https://x/"], {})

    assert result["status"] == "failed" and result["error"] == "ValueError"
    assert result["site_id"] == 7 and result["web_url"] == "https://x/"
    assert result["rows"] == [] and result["title_updates"] == [] and result["skipped"] == []
    assert "secret" not in json.dumps(result) and "private.example" not in json.dumps(result)


def test_every_site_crashing_gives_a_failed_result_for_each(monkeypatch):
    def always_crash(site_id, url, rules):
        raise ValueError("nope")

    monkeypatch.setattr(main, "process_website", always_crash)

    results = main.finding_updated_novels([1, 2], ["a", "b"], {})

    assert [r["status"] for r in results] == ["failed", "failed"]


def test_keyboard_interrupt_is_not_swallowed(monkeypatch):
    def interrupted(site_id, url, rules):
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "process_website", interrupted)

    with pytest.raises(KeyboardInterrupt):
        main.finding_updated_novels([1], ["a"], {})


def test_a_site_with_broken_rules_no_longer_kills_the_run(pipeline):
    """html mode but no chapter_regex: parsing raises KeyError for this site only."""
    bad_url = "https://bad.org/"
    rules = {
        bad_url: {"mode": "html", "segment_count": 3, "base_url": "https://bad.org"},
        GOOD_URL: GOOD_RULES,
    }

    results = main.finding_updated_novels([1, 2], [bad_url, GOOD_URL], rules)

    assert [r["status"] for r in results] == ["failed", "ok"]
    assert results[0]["error"] == "KeyError"
    assert [row["latest_chap"] for row in results[1]["rows"]] == [11]   # the good site still delivered
