"""scripts/preview_run.py must be strictly read-only and must report what a run would do."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import main
from scripts import preview_run
from src import database

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RULES = json.loads((ROOT / "site_parse_rules.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)                 # before the fixture's unlock times
ASURA, HIVE = "https://asurascans.com/", "https://hivetoons.org/"

WRITES = ["insert_scraper_logs", "update_scraper_logs", "maintain_queue", "update_novels_table", "sync_novel_titles",
          "insert_queue_records", "claim_due", "ack_sent", "release", "mark_dead", "handle_blocked_users",
          "record_site_result", "ack_site_alerts", "set_app_state"]


@pytest.fixture
def world(monkeypatch):
    for name in WRITES:                                            # any write is a test failure
        monkeypatch.setattr(database, name, lambda *a, _n=name, **k: pytest.fail(f"preview called {_n}"))
    monkeypatch.setattr(main.telegramMessenger, "send_all", lambda *a, **k: pytest.fail("preview sent messages"))
    monkeypatch.setattr(main.telegramMessenger, "send_admin", lambda *a, **k: pytest.fail("preview messaged an admin"))

    state = {
        "novels": {
            "w-asura": [{"id": "n-a", "novel_name": "emperor-of-solo-play", "latest_chap": 91, "chapter_url": "old", "display_title": None, "web_id": "w-asura"},
                        {"id": "n-b", "novel_name": "reincarnator", "latest_chap": -1, "chapter_url": "old", "display_title": None, "web_id": "w-asura"},
                        {"id": "n-c", "novel_name": "cyberpunk-chivalry", "latest_chap": 6, "chapter_url": "old", "display_title": None, "web_id": "w-asura"}],
            "w-hive": [{"id": "n-h", "novel_name": "lookism", "latest_chap": 600, "chapter_url": "old", "display_title": "Lookism", "web_id": "w-hive"}],
        },
        "recipients": [
            {"novel_id": n, "user_id": u, "alert_timing": None, "telegram_user_id": tg}
            for n in ("n-a", "n-b", "n-c", "n-h") for u, tg in (("member", 1), ("standard", 2))
        ],
        "tiers": {("member", "w-asura"): "member"},
        "failed": set(),
    }
    pages = {ASURA: "asura_latest.html", HIVE: "hive_latest.html"}
    monkeypatch.setattr(main.fetching, "fetch_html", lambda url: (FIXTURES / pages[url]).read_text(encoding="utf-8"))
    monkeypatch.setattr(database, "fetch_novels", lambda site_id: state["novels"][site_id])
    monkeypatch.setattr(database, "fetch_subscribers_with_preferences",
                        lambda ids, web: database.SubscriberFetch(state["recipients"], state["tiers"], state["failed"]))
    return state


SITES = [{"id": "w-asura", "web_domain_name": ASURA}, {"id": "w-hive", "web_domain_name": HIVE}]


def report(world, only=None):
    lines, summary = preview_run.build_report(SITES, RULES, NOW, only)
    return "\n".join(lines), summary


def test_the_preview_reports_sites_chapters_and_a_total_without_writing_anything(world):
    text, summary = report(world)
    assert "ok        https://asurascans.com/" in text and "ok        https://hivetoons.org/" in text
    assert summary["rows"] == 3 and summary["records"] == 6 and len(summary["users"]) == 2     # cyberpunk is already up to date
    assert "WOULD QUEUE 6 record(s) for 2 user(s); 0 novel(s) held back." in text
    assert "Cyberpunk" not in text
    assert "Nothing was written and nothing was sent." in text


def test_locked_chapters_show_when_each_reader_would_be_told(world):
    text, _ = report(world)
    emperor = next(line for line in text.splitlines() if "Ch. 92" in line)
    assert "[locked until 2026-11-10T02:41:27.273007+00:00]" in emperor
    assert "1 now, 1 later" in emperor                                  # the member now, the standard reader at unlock
    lookism = next(line for line in text.splitlines() if "Ch. 632" in line)
    assert "2 now" in lookism and "[free]" in lookism


def test_first_sight_and_big_jumps_are_flagged(world):
    text, _ = report(world)
    reincarnator = next(line for line in text.splitlines() if "Ch. 159" in line)
    assert "FIRST SIGHT" in reincarnator
    lookism = next(line for line in text.splitlines() if "Ch. 632" in line)
    assert "JUMP +32" in lookism
    assert text.count("FIRST SIGHT") == 1                                # only the novel stored at -1


def test_a_quiet_site_set_says_nothing_would_be_sent(world):
    world["novels"] = {"w-asura": [], "w-hive": []}
    text, summary = report(world)
    assert "WOULD QUEUE 0 records: nothing new, nothing would be sent." in text and summary["records"] == 0


def test_novels_nobody_reads_are_listed_as_such(world):
    world["recipients"] = []
    text, summary = report(world)
    assert "nobody reads it" in text and summary["records"] == 0 and "WOULD QUEUE 0 record(s) for 0 user(s)" in text


def test_novels_with_unreadable_recipients_are_reported_as_held_back(world):
    world["failed"] = {"n-h"}
    text, summary = report(world)
    assert "[HELD BACK: recipients unreadable]" in text and "1 novel(s) held back" in text


def test_the_site_filter_limits_what_is_scraped(world):
    text, summary = report(world, only="hive")
    assert "asurascans.com" not in text and "hivetoons.org" in text and summary["rows"] == 1


def test_a_failed_site_is_shown_and_counted(world, monkeypatch):
    monkeypatch.setattr(main.fetching, "fetch_html", lambda url: "")
    text, summary = report(world)
    assert "failed    https://hivetoons.org/ (fetch failed)" in text and summary["sites_failed"] == 2


def test_the_command_line_entry_runs_from_the_repo_root(world, monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(database, "fetch_websites", lambda: SITES)
    monkeypatch.setattr(main, "utcnow", lambda: NOW)
    assert preview_run.main_cli(["--site", "asura"]) == 0
    out = capsys.readouterr().out
    assert "SITES" in out and "WOULD QUEUE" in out and "hivetoons" not in out
