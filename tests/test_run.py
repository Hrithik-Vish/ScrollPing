"""Tests for run() in main.py: how the Telegram step is wired into the pipeline.

Everything outside run() is faked (database, finding_updated_novels,
telegramMessenger.send_all). group_subscribers is the real function.
The `events` list records the order in which the pipeline steps were called.
"""

from pathlib import Path

import pytest
from telegram.error import InvalidToken

import main

ROOT = Path(__file__).resolve().parent.parent

UPDATED_ROW = {
    "id": 3,
    "novel_name": "lookism",
    "latest_chap": 631,
    "chapter_url": "https://hivetoons.org/series/lookism/chapter-631",
}
SUBSCRIBERS = [{"novel_id": 3, "users": {"telegram_user_id": "111"}}]
OK_SUMMARY = {"sent": 1, "blocked": [], "bad_request": [], "failed": []}


@pytest.fixture
def pipeline(monkeypatch):
    """Fake every collaborator of run(); tests tweak `state` to change behaviour."""
    monkeypatch.chdir(ROOT)  # run() opens site_parse_rules.json by relative path

    state = {
        "events": [],
        "insert_result": (True, 42),
        "updated_novels": [UPDATED_ROW],
        "subscribers": SUBSCRIBERS,
        "send_effect": OK_SUMMARY,  # a summary dict to return, or an Exception to raise
        "flipped_with": [],
        "subscribers_asked_for": [],
        "send_args": [],
        "updated_table_with": [],
    }
    events = state["events"]

    def insert_scraper_logs():
        events.append("insert_scraper_logs")
        return state["insert_result"]

    def fetch_websites():
        events.append("fetch_websites")
        return [{"id": 1, "web_domain_name": "https://example.org/"}]

    def finding_updated_novels(site_ids, urls, rules):
        events.append("finding_updated_novels")
        return state["updated_novels"]

    def update_novels_table(rows):
        events.append("update_novels_table")
        state["updated_table_with"].append(rows)

    def fetch_subscribers(novel_ids):
        events.append("fetch_subscribers")
        state["subscribers_asked_for"].append(novel_ids)
        return state["subscribers"]

    def send_all(user_tele_dict):
        events.append("send_all")
        state["send_args"].append(user_tele_dict)
        effect = state["send_effect"]
        if isinstance(effect, BaseException):
            raise effect
        return effect

    def update_scraper_logs(row_id):
        events.append("update_scraper_logs")
        state["flipped_with"].append(row_id)

    monkeypatch.setattr(main.database, "insert_scraper_logs", insert_scraper_logs)
    monkeypatch.setattr(main.database, "fetch_websites", fetch_websites)
    monkeypatch.setattr(main, "finding_updated_novels", finding_updated_novels)
    monkeypatch.setattr(main.database, "update_novels_table", update_novels_table)
    monkeypatch.setattr(main.database, "fetch_subscribers", fetch_subscribers)
    monkeypatch.setattr(main.telegramMessenger, "send_all", send_all)
    monkeypatch.setattr(main.database, "update_scraper_logs", update_scraper_logs)
    return state


# ---------------------------------------------------------------- happy path

def test_run_sends_after_db_update_and_before_the_final_flip(pipeline):
    main.run()  # must finish without SystemExit

    events = pipeline["events"]
    assert events.index("update_novels_table") < events.index("fetch_subscribers")
    assert events.index("fetch_subscribers") < events.index("send_all")
    assert events.index("send_all") < events.index("update_scraper_logs")
    assert events[-1] == "update_scraper_logs"


def test_run_passes_grouped_users_to_send_all(pipeline):
    main.run()

    assert pipeline["subscribers_asked_for"] == [[3]]
    assert pipeline["send_args"] == [{"111": [UPDATED_ROW]}]


def test_run_flips_the_log_for_the_inserted_row(pipeline):
    main.run()

    assert pipeline["flipped_with"] == [42]


def test_run_still_calls_send_all_on_a_quiet_run(pipeline):
    pipeline["updated_novels"] = []
    pipeline["subscribers"] = []

    main.run()

    # Documents current behaviour: send_all({}) is still called (token health check).
    assert pipeline["send_args"] == [{}]
    assert pipeline["flipped_with"] == [42]


def test_run_partial_send_failures_do_not_fail_the_run(pipeline, capsys):
    pipeline["send_effect"] = {
        "sent": 3,
        "blocked": ["111"],
        "bad_request": [("222", "Chat not found")],
        "failed": [("333", "network", "timeout")],
    }

    main.run()  # no SystemExit

    assert pipeline["flipped_with"] == [42]
    assert "sent=3" in capsys.readouterr().out


# ---------------------------------------------------------------- fatal send problems

@pytest.mark.parametrize(
    "error",
    [
        InvalidToken("rejected"),
        RuntimeError("TELEGRAM_BOT_TOKEN is missing or empty"),
        ValueError("unexpected bug inside the messenger"),
    ],
    ids=["invalid-token", "missing-token", "unexpected-error"],
)
def test_run_send_errors_still_flip_the_log_then_exit_1(pipeline, error):
    pipeline["send_effect"] = error

    with pytest.raises(SystemExit) as exc_info:
        main.run()

    assert exc_info.value.code == 1
    events = pipeline["events"]
    assert "update_scraper_logs" in events  # the scrape itself succeeded
    assert events.index("send_all") < events.index("update_scraper_logs")
    assert pipeline["flipped_with"] == [42]


def test_run_invalid_token_message_does_not_leak_the_token(pipeline, capsys):
    pipeline["send_effect"] = InvalidToken("The token `123456:SECRET-VALUE` was rejected")

    with pytest.raises(SystemExit):
        main.run()

    captured = capsys.readouterr()
    assert "SECRET-VALUE" not in captured.out + captured.err
    assert "token was rejected" in captured.out


def test_run_missing_token_prints_a_clear_message(pipeline, capsys):
    pipeline["send_effect"] = RuntimeError("TELEGRAM_BOT_TOKEN is missing or empty")

    with pytest.raises(SystemExit):
        main.run()

    assert "not configured" in capsys.readouterr().out


# ---------------------------------------------------------------- scraper_logs insert fails

def test_run_stops_when_scraper_log_cannot_be_inserted(pipeline, capsys):
    pipeline["insert_result"] = (False, None)

    with pytest.raises(SystemExit) as exc_info:
        main.run()

    assert exc_info.value.code == 1
    assert pipeline["events"] == ["insert_scraper_logs"]  # nothing else ran
    assert "could not insert" in capsys.readouterr().out


# ---------------------------------------------------------------- print_send_summary

def test_print_send_summary_prints_counts_only(capsys):
    main.print_send_summary(
        {
            "sent": 5,
            "blocked": ["111", "222"],
            "bad_request": [("333", "Chat not found")],
            "failed": [("444", "network", "timeout detail")],
        }
    )

    out = capsys.readouterr().out
    assert "sent=5" in out
    assert "blocked=2" in out
    assert "bad_request=1" in out
    assert "failed=1" in out
    for private in ("111", "222", "333", "444", "Chat not found", "timeout detail"):
        assert private not in out
