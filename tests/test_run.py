"""run() in main.py: the order of the steps (spec section 9) and how each step's failure is handled.

Everything outside run() is faked (database, finding_updated_novels, claim_due, send_all). The pure
pieces run for real: scheduling.build_queue_records, parsing.validate_rules, dispatch_queue,
group_claimed. `events` records the order in which the pipeline steps were called.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from telegram.error import InvalidToken

import main
from src import database

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
UNLOCK = NOW + timedelta(hours=5)

FREE_ROW = {
    "id": "n-free", "novel_name": "lookism", "latest_chap": 631.0, "chapter_url": "https://hivetoons.org/series/lookism/chapter-631",
    "display_title": "Lookism", "access_state": "free", "unlock_at": None, "web_id": "w-hive",
}
LOCKED_ROW = {
    "id": "n-locked", "novel_name": "killer-pietro", "latest_chap": 156.0,
    "chapter_url": "https://asurascans.com/comics/killer-pietro-f212c582/chapter/156",
    "display_title": "Killer Pietro", "access_state": "locked", "unlock_at": UNLOCK.isoformat(), "web_id": "w-asura",
}


def site_result(site_id="w-hive", status="ok", error=None, rows=(), title_updates=()):
    return {"site_id": site_id, "web_url": f"https://{site_id}/", "status": status, "error": error,
            "rows": list(rows), "title_updates": list(title_updates), "skipped": []}


def who(novel_id, user_id, tg, timing=None):
    return {"novel_id": novel_id, "user_id": user_id, "alert_timing": timing, "telegram_user_id": tg}


OK_SUMMARY = {"sent": 1, "blocked": 0, "bad_request": 0, "failed": 0, "retried": 0}


@pytest.fixture
def pipeline(monkeypatch):
    """Fake every collaborator of run(); tests tweak `state` to change behaviour."""
    monkeypatch.chdir(ROOT)  # run() opens site_parse_rules.json by relative path

    state = {
        "events": [],
        "insert_log_result": (True, 42),
        "maintain_result": {"ok": True, "recovered": 0, "cancelled": 0, "expired": 0},
        "results": [site_result(rows=[FREE_ROW])],
        "subscribers": database.SubscriberFetch([who("n-free", "u1", 111)], {}, set()),
        "insert_override": None,                 # a dict to return instead of the computed insert result
        "update_failed_ids": [],
        "claim_passes": [[{"notification_id": "q1", "telegram_user_id": 111, "novel_name": "lookism", "chapter": 631.0,
                           "chapter_url": "x", "access_state": "free", "unlock_at": None}]],
        "send_effect": OK_SUMMARY,               # a summary dict to return, or an Exception to raise
        "record_site_returns": {"consecutive_failures": 0, "failure_alerted": False},
        # what the faked functions were called with
        "recorded": [], "synced": [], "subscribers_asked": [], "queue_records": [], "updated_rows": [],
        "sent_batches": [], "flipped_with": [], "admin_calls": [], "admin_error": None,
    }
    events = state["events"]

    def insert_scraper_logs():
        events.append("insert_scraper_logs")
        return state["insert_log_result"]

    def maintain_queue():
        events.append("maintain_queue")
        return state["maintain_result"]

    def fetch_websites():
        events.append("fetch_websites")
        return [{"id": "w-hive", "web_domain_name": "https://hivetoons.org/"}]

    def finding_updated_novels(site_ids, urls, rules):
        events.append("finding_updated_novels")
        return state["results"]

    def record_site_result(web_id, status, error=None):
        events.append("record_site_result")
        state["recorded"].append((web_id, status, error))
        return state["record_site_returns"]

    def sync_novel_titles(updates):
        events.append("sync_novel_titles")
        state["synced"].append(updates)
        return [u["id"] for u in updates]

    def fetch_subscribers_with_preferences(novel_ids, web_id_by_novel=None):
        events.append("fetch_subscribers_with_preferences")
        state["subscribers_asked"].append((list(novel_ids), dict(web_id_by_novel or {})))
        return state["subscribers"]

    def insert_queue_records(records):
        events.append("insert_queue_records")
        state["queue_records"].append(records)
        if state["insert_override"] is not None:
            return state["insert_override"]
        return {"attempted": len(records), "inserted": len(records), "failed": 0, "failed_novel_ids": set()}

    def update_novels_table(rows):
        events.append("update_novels_table")
        state["updated_rows"].append(rows)
        return state["update_failed_ids"]

    def claim_due(limit=500):
        events.append("claim_due")
        return state["claim_passes"].pop(0) if state["claim_passes"] else []

    def send_all(batches, delivery, rules=None, now=None):
        events.append("send_all")
        state["sent_batches"].append(batches)
        state["send_rules"] = rules
        effect = state["send_effect"]
        if isinstance(effect, BaseException):
            raise effect
        return effect

    def run_admin_tasks(rules, now):
        events.append("run_admin_tasks")
        state["admin_calls"].append((rules, now))
        if state["admin_error"]:
            raise state["admin_error"]

    def update_scraper_logs(row_id):
        events.append("update_scraper_logs")
        state["flipped_with"].append(row_id)

    monkeypatch.setattr(main, "utcnow", lambda: NOW)
    monkeypatch.setattr(main.database, "insert_scraper_logs", insert_scraper_logs)
    monkeypatch.setattr(main.database, "maintain_queue", maintain_queue)
    monkeypatch.setattr(main.database, "fetch_websites", fetch_websites)
    monkeypatch.setattr(main, "finding_updated_novels", finding_updated_novels)
    monkeypatch.setattr(main.database, "record_site_result", record_site_result)
    monkeypatch.setattr(main.database, "sync_novel_titles", sync_novel_titles)
    monkeypatch.setattr(main.database, "fetch_subscribers_with_preferences", fetch_subscribers_with_preferences)
    monkeypatch.setattr(main.database, "insert_queue_records", insert_queue_records)
    monkeypatch.setattr(main.database, "update_novels_table", update_novels_table)
    monkeypatch.setattr(main.database, "claim_due", claim_due)
    monkeypatch.setattr(main.telegramMessenger, "send_all", send_all)
    monkeypatch.setattr(main, "run_admin_tasks", run_admin_tasks)
    monkeypatch.setattr(main.database, "update_scraper_logs", update_scraper_logs)
    return state


# ---------------------------------------------------------------- the order of the steps

def test_run_follows_the_documented_step_order(pipeline):
    pipeline["results"] = [site_result(rows=[FREE_ROW], title_updates=[{"id": "n-other", "display_title": "Other"}])]
    main.run()   # must finish without SystemExit

    order = [e for e in pipeline["events"] if e != "fetch_websites"]
    assert order == [
        "insert_scraper_logs", "maintain_queue", "finding_updated_novels", "record_site_result", "sync_novel_titles",
        "fetch_subscribers_with_preferences", "insert_queue_records", "update_novels_table",
        "claim_due", "send_all", "claim_due", "run_admin_tasks", "update_scraper_logs",
    ]


def test_d1_queue_rows_are_inserted_before_the_novels_table_is_updated(pipeline):
    main.run()
    events = pipeline["events"]
    assert events.index("insert_queue_records") < events.index("update_novels_table")
    assert events.index("fetch_subscribers_with_preferences") < events.index("insert_queue_records")


def test_a_crash_between_queue_insert_and_novel_update_loses_nothing(pipeline, monkeypatch):
    def crash(rows):
        raise RuntimeError("process died")
    monkeypatch.setattr(main.database, "update_novels_table", crash)

    with pytest.raises(RuntimeError):
        main.run()

    # the queue already holds the alert, and the novels table was not advanced: next run re-detects
    assert [r["novel_id"] for r in pipeline["queue_records"][0]] == ["n-free"]
    assert "update_scraper_logs" not in pipeline["events"]


def test_delivery_comes_after_the_database_work_and_before_the_final_flip(pipeline):
    main.run()
    events = pipeline["events"]
    assert events.index("update_novels_table") < events.index("claim_due") < events.index("send_all") < events.index("update_scraper_logs")
    assert events[-1] == "update_scraper_logs"


def test_run_flips_the_log_for_the_inserted_row(pipeline):
    main.run()
    assert pipeline["flipped_with"] == [42]


def test_maintenance_runs_right_after_the_log_insert_and_a_failure_does_not_stop_the_run(pipeline, capsys):
    pipeline["maintain_result"] = {"ok": False, "recovered": 0, "cancelled": 0, "expired": 0}
    main.run()
    assert pipeline["events"][:2] == ["insert_scraper_logs", "maintain_queue"]
    assert "queue maintenance failed" in capsys.readouterr().out
    assert pipeline["flipped_with"] == [42]


def test_maintenance_counts_are_printed(pipeline, capsys):
    pipeline["maintain_result"] = {"ok": True, "recovered": 2, "cancelled": 1, "expired": 3}
    main.run()
    assert "recovered=2 cancelled=1 expired=3" in capsys.readouterr().out


# ---------------------------------------------------------------- queue records

def test_queue_records_are_built_per_recipient_with_the_resolved_due_time(pipeline):
    pipeline["results"] = [site_result("w-asura", rows=[LOCKED_ROW])]
    pipeline["subscribers"] = database.SubscriberFetch(
        [who("n-locked", "standard-user", 1), who("n-locked", "member-user", 2), who("n-locked", "override", 3, "early")],
        {("member-user", "w-asura"): "member"}, set())

    main.run()

    records = {r["user_id"]: r for r in pipeline["queue_records"][0]}
    assert records["standard-user"]["due_at"] == UNLOCK.isoformat()        # waits until it is free
    assert records["member-user"]["due_at"] == NOW.isoformat()             # member: early
    assert records["override"]["due_at"] == NOW.isoformat()                # per-novel override
    assert all(r["access_state"] == "locked" and r["unlock_at"] == UNLOCK.isoformat() for r in records.values())
    assert all(r["chapter"] == 156.0 and r["status"] == "pending" for r in records.values())


def test_subscribers_are_asked_for_every_updated_novel_with_its_site(pipeline):
    pipeline["results"] = [site_result("w-hive", rows=[FREE_ROW]), site_result("w-asura", rows=[LOCKED_ROW])]
    pipeline["subscribers"] = database.SubscriberFetch([], {}, set())
    main.run()
    assert pipeline["subscribers_asked"] == [(["n-free", "n-locked"], {"n-free": "w-hive", "n-locked": "w-asura"})]


def test_every_updated_row_is_written_to_the_novels_table(pipeline):
    pipeline["results"] = [site_result("w-hive", rows=[FREE_ROW]), site_result("w-asura", rows=[LOCKED_ROW])]
    pipeline["subscribers"] = database.SubscriberFetch([who("n-free", "u1", 1)], {}, set())
    main.run()
    assert [r["id"] for r in pipeline["updated_rows"][0]] == ["n-free", "n-locked"]


def test_novels_nobody_reads_are_still_updated_but_nothing_is_queued(pipeline):
    pipeline["subscribers"] = database.SubscriberFetch([], {}, set())
    main.run()
    assert "insert_queue_records" not in pipeline["events"]
    assert [r["id"] for r in pipeline["updated_rows"][0]] == ["n-free"]


# ---------------------------------------------------------------- holding novels back (D1 safety)

def test_a_novel_whose_recipients_could_not_be_read_is_held_back_not_advanced(pipeline, capsys):
    pipeline["results"] = [site_result(rows=[FREE_ROW, LOCKED_ROW])]
    pipeline["subscribers"] = database.SubscriberFetch([who("n-free", "u1", 1)], {}, {"n-locked"})

    main.run()

    assert [r["novel_id"] for r in pipeline["queue_records"][0]] == ["n-free"]
    assert [r["id"] for r in pipeline["updated_rows"][0]] == ["n-free"]          # n-locked is re-detected next run
    assert "1 novel(s) held back" in capsys.readouterr().out


def test_a_novel_whose_queue_rows_failed_to_insert_is_held_back(pipeline):
    pipeline["results"] = [site_result(rows=[FREE_ROW, LOCKED_ROW])]
    pipeline["subscribers"] = database.SubscriberFetch([who("n-free", "u1", 1), who("n-locked", "u2", 2)], {}, set())
    pipeline["insert_override"] = {"attempted": 2, "inserted": 1, "failed": 1, "failed_novel_ids": {"n-locked"}}

    main.run()

    assert [r["id"] for r in pipeline["updated_rows"][0]] == ["n-free"]


def test_when_everything_is_held_back_the_run_still_delivers_leftovers(pipeline):
    pipeline["subscribers"] = database.SubscriberFetch([], {}, {"n-free"})
    main.run()
    assert pipeline["updated_rows"] == [[]]
    assert "claim_due" in pipeline["events"] and pipeline["flipped_with"] == [42]


def test_failed_novel_writes_are_counted_and_never_block_delivery(pipeline, capsys):
    pipeline["update_failed_ids"] = ["n-free"]
    main.run()
    assert "novels: 0 of 1 updated" in capsys.readouterr().out
    assert "send_all" in pipeline["events"]


# ---------------------------------------------------------------- quiet runs and leftovers

def test_a_quiet_run_skips_the_database_steps_but_still_delivers_leftovers(pipeline):
    pipeline["results"] = [site_result()]
    main.run()
    events = pipeline["events"]
    for skipped in ("fetch_subscribers_with_preferences", "insert_queue_records", "update_novels_table", "sync_novel_titles"):
        assert skipped not in events
    assert pipeline["sent_batches"] == [{111: pipeline["sent_batches"][0][111]}]      # the leftover row was delivered
    assert pipeline["flipped_with"] == [42]


def test_a_quiet_run_with_an_empty_queue_sends_nothing(pipeline):
    pipeline["results"] = [site_result()]
    pipeline["claim_passes"] = []
    main.run()
    assert "send_all" not in pipeline["events"] and pipeline["flipped_with"] == [42]


def test_the_loaded_rules_reach_the_messenger(pipeline):
    main.run()
    assert "https://hivetoons.org/" in pipeline["send_rules"]
    assert pipeline["send_rules"]["https://hivetoons.org/"]["display_name"] == "Hive Toons"


def test_delivery_groups_claimed_rows_by_telegram_user(pipeline):
    pipeline["claim_passes"] = [[
        {"notification_id": "q1", "telegram_user_id": 111, "novel_name": "a", "chapter": 1.0},
        {"notification_id": "q2", "telegram_user_id": 222, "novel_name": "b", "chapter": 2.0},
        {"notification_id": "q3", "telegram_user_id": 111, "novel_name": "c", "chapter": 3.0},
    ]]
    main.run()
    batches = pipeline["sent_batches"][0]
    assert [r["notification_id"] for r in batches[111]] == ["q1", "q3"] and [r["notification_id"] for r in batches[222]] == ["q2"]


# ---------------------------------------------------------------- site health and titles

def test_site_health_is_recorded_for_every_scraped_site_but_not_for_skipped_ones(pipeline):
    pipeline["results"] = [
        site_result("w-1", "ok"),
        site_result("w-2", "degraded", "no titles found"),
        site_result("w-3", "failed", "fetch failed"),
        site_result("w-4", "skipped"),
    ]
    main.run()
    assert pipeline["recorded"] == [("w-1", "ok", None), ("w-2", "degraded", "no titles found"), ("w-3", "failed", "fetch failed")]


def test_a_failure_to_record_health_is_counted_and_never_stops_the_run(pipeline, capsys):
    pipeline["results"] = [site_result("w-1", "ok"), site_result("w-2", "failed", "x")]
    pipeline["record_site_returns"] = None
    main.run()
    assert "health not recorded for 2" in capsys.readouterr().out
    assert pipeline["flipped_with"] == [42]


def test_the_site_summary_line_shows_counts_only(pipeline, capsys):
    pipeline["results"] = [site_result("w-1", "ok"), site_result("w-2", "failed", "secret detail"), site_result("w-3", "skipped")]
    main.run()
    out = capsys.readouterr().out
    assert "sites: ok=1 degraded=0 failed=1 skipped=1" in out
    assert "secret detail" not in out


def test_title_updates_from_all_sites_are_synced_once(pipeline):
    pipeline["results"] = [
        site_result("w-1", title_updates=[{"id": "a", "display_title": "A"}]),
        site_result("w-2", title_updates=[{"id": "b", "display_title": "B"}]),
    ]
    main.run()
    assert pipeline["synced"] == [[{"id": "a", "display_title": "A"}, {"id": "b", "display_title": "B"}]]


def test_titles_are_synced_before_the_chapter_work_and_never_when_there_are_none(pipeline):
    pipeline["results"] = [site_result(rows=[FREE_ROW])]
    main.run()
    assert "sync_novel_titles" not in pipeline["events"]


# ---------------------------------------------------------------- rules validation

def test_rules_problems_are_printed_but_the_run_continues(pipeline, monkeypatch, capsys):
    monkeypatch.setattr(main.parsing, "validate_rules", lambda rules: ["https://x/: chapter_regex is missing"])
    main.run()
    assert "site_parse_rules.json: https://x/: chapter_regex is missing" in capsys.readouterr().out
    assert pipeline["flipped_with"] == [42]


def test_the_real_rules_file_produces_no_validation_messages(pipeline, capsys):
    main.run()
    assert "site_parse_rules.json:" not in capsys.readouterr().out


# ---------------------------------------------------------------- send results

def test_partial_send_failures_do_not_fail_the_run(pipeline, capsys):
    pipeline["send_effect"] = {"sent": 3, "blocked": 1, "bad_request": 1, "failed": 2, "retried": 1}
    main.run()  # no SystemExit
    assert pipeline["flipped_with"] == [42]
    out = capsys.readouterr().out
    assert "sent=3" in out and "retried=1" in out


@pytest.mark.parametrize(
    "error",
    [
        InvalidToken("rejected"),
        RuntimeError("TELEGRAM_BOT_TOKEN is missing or empty"),
        ValueError("unexpected bug inside the messenger"),
    ],
    ids=["invalid-token", "missing-token", "unexpected-error"],
)
def test_fatal_send_errors_still_flip_the_log_then_exit_1(pipeline, error):
    pipeline["send_effect"] = error

    with pytest.raises(SystemExit) as exc_info:
        main.run()

    assert exc_info.value.code == 1
    events = pipeline["events"]
    assert "update_scraper_logs" in events  # the scrape itself succeeded
    assert events.index("send_all") < events.index("update_scraper_logs")
    assert pipeline["flipped_with"] == [42]


def test_invalid_token_message_does_not_leak_the_token(pipeline, capsys):
    pipeline["send_effect"] = InvalidToken("The token `123456:SECRET-VALUE` was rejected")

    with pytest.raises(SystemExit):
        main.run()

    captured = capsys.readouterr()
    assert "SECRET-VALUE" not in captured.out + captured.err
    assert "token was rejected" in captured.out


def test_missing_token_prints_a_clear_message(pipeline, capsys):
    pipeline["send_effect"] = RuntimeError("TELEGRAM_BOT_TOKEN is missing or empty")

    with pytest.raises(SystemExit):
        main.run()

    assert "not configured" in capsys.readouterr().out


# ---------------------------------------------------------------- scraper_logs insert fails

def test_run_stops_when_scraper_log_cannot_be_inserted(pipeline, capsys):
    pipeline["insert_log_result"] = (False, None)

    with pytest.raises(SystemExit) as exc_info:
        main.run()

    assert exc_info.value.code == 1
    assert pipeline["events"] == ["insert_scraper_logs"]  # nothing else ran
    assert "could not insert" in capsys.readouterr().out


# ---------------------------------------------------------------- step 11: admin alerts and the digest

def test_step_11_runs_after_delivery_and_before_the_final_flip(pipeline):
    main.run()
    events = pipeline["events"]
    assert events.index("send_all") < events.index("run_admin_tasks") < events.index("update_scraper_logs")


def test_step_11_gets_the_loaded_rules_and_the_runs_clock(pipeline):
    main.run()
    [(rules, now)] = pipeline["admin_calls"]
    assert "https://asurascans.com/" in rules and now == NOW


def test_a_problem_in_the_admin_step_never_fails_the_run(pipeline, capsys):
    pipeline["admin_error"] = RuntimeError("secret detail that must not be printed")
    main.run()                                              # no SystemExit
    out = capsys.readouterr()
    assert "admin: step failed (RuntimeError)" in out.out and "secret detail" not in out.out
    assert pipeline["flipped_with"] == [42]


def test_the_admin_step_still_runs_when_delivery_fails_fatally(pipeline):
    pipeline["send_effect"] = InvalidToken("rejected")
    with pytest.raises(SystemExit) as exc_info:
        main.run()
    assert exc_info.value.code == 1
    events = pipeline["events"]
    assert "run_admin_tasks" in events and events.index("run_admin_tasks") < events.index("update_scraper_logs")


def test_a_quiet_run_still_checks_for_alerts_and_the_digest(pipeline):
    pipeline["results"] = [site_result()]
    pipeline["claim_passes"] = []
    main.run()
    assert "run_admin_tasks" in pipeline["events"]
