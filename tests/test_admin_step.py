"""main.run_admin_tasks (step 11): immediate failure alerts and the daily digest, with the database and
Telegram faked. The pure text/schedule logic is covered in test_admin.py."""

from datetime import datetime, timezone

import pytest

import main

NOW = datetime(2026, 11, 10, 18, 5, 20, tzinfo=timezone.utc)          # just after 23:35 IST
ASURA, HIVE = "https://asurascans.com/", "https://hivetoons.org/"
RULES = {ASURA: {"display_name": "Asura Scans"}, HIVE: {"display_name": "Hive Toons"}}
STATS = {"sent_24h": 5, "retried_24h": 0, "dead_24h": 0, "cancelled_24h": 0, "pending_now": 0, "sending_now": 0}


def alert(web_id, domain=ASURA, failures=3):
    return {"web_id": web_id, "web_domain_name": domain, "consecutive_failures": failures,
            "failing_since": "2026-11-10T14:05:00+00:00", "last_error": "fetch failed"}


def health_row(domain=HIVE):
    return {"web_id": "w", "web_domain_name": domain, "last_status": "ok", "consecutive_failures": 0, "failing_since": None,
            "last_failed_at": None, "last_resolved_at": None, "last_error": None}


@pytest.fixture
def world(monkeypatch):
    state = {
        "alerts": [], "last_digest": None, "state_error": None, "health": [health_row()], "stats": STATS,
        "deliver": lambda ids, text: {"delivered": len(ids), "failed": 0},
        "sent": [], "acked": [], "state_writes": [], "events": [], "set_returns": True,
    }

    def fetch_site_alerts():
        state["events"].append("fetch_site_alerts")
        return state["alerts"]

    def ack_site_alerts(ids):
        state["events"].append("ack_site_alerts")
        state["acked"].append(list(ids))
        return len(ids)

    def get_app_state(key):
        state["events"].append("get_app_state")
        assert key == "last_digest_sent_at"
        if state["state_error"]:
            raise state["state_error"]
        return state["last_digest"]

    def set_app_state(key, value):
        state["events"].append("set_app_state")
        state["state_writes"].append((key, value))
        return state["set_returns"]

    def send_admin(ids, text):
        state["events"].append("send_admin")
        state["sent"].append((list(ids), text))
        return state["deliver"](ids, text)

    monkeypatch.setenv("ADMIN_TELEGRAM_USER_IDS", "111, 222")
    monkeypatch.setattr(main.database, "fetch_site_alerts", fetch_site_alerts)
    monkeypatch.setattr(main.database, "ack_site_alerts", ack_site_alerts)
    monkeypatch.setattr(main.database, "get_app_state", get_app_state)
    monkeypatch.setattr(main.database, "set_app_state", set_app_state)
    monkeypatch.setattr(main.database, "fetch_site_health_rows", lambda: state["health"])
    monkeypatch.setattr(main.database, "queue_stats", lambda: state["stats"])
    monkeypatch.setattr(main.telegramMessenger, "send_admin", send_admin)
    return state


def go():
    main.run_admin_tasks(RULES, NOW)


# ---------------------------------------------------------------- no admins

@pytest.mark.parametrize("value", ["", "   ", "abc", ",,"])
def test_without_admin_ids_everything_is_skipped_with_a_message(world, monkeypatch, capsys, value):
    monkeypatch.setenv("ADMIN_TELEGRAM_USER_IDS", value)
    go()
    assert world["events"] == [] and world["sent"] == []
    assert "ADMIN_TELEGRAM_USER_IDS is not set" in capsys.readouterr().out


def test_an_unset_variable_is_the_same_as_an_empty_one(world, monkeypatch, capsys):
    monkeypatch.delenv("ADMIN_TELEGRAM_USER_IDS")
    go()
    assert world["events"] == [] and "skipped" in capsys.readouterr().out


# ---------------------------------------------------------------- immediate alerts

def test_each_failing_site_is_sent_to_every_admin_and_then_marked_reported(world):
    world["alerts"] = [alert("w-asura", ASURA, 4), alert("w-hive", HIVE, 3)]
    world["last_digest"] = "2026-11-10T18:05:10+00:00"                    # digest already sent: alerts only
    go()
    assert [ids for ids, text in world["sent"]] == [[111, 222], [111, 222]]
    assert "<b>Asura Scans</b> has failed 4 runs in a row." in world["sent"][0][1]
    assert "<b>Hive Toons</b> has failed 3 runs in a row." in world["sent"][1][1]
    assert world["acked"] == [["w-asura", "w-hive"]]


def test_a_site_is_marked_reported_only_after_its_alert_was_delivered(world):
    world["alerts"] = [alert("w-asura", ASURA), alert("w-hive", HIVE)]
    world["last_digest"] = "2026-11-10T18:05:10+00:00"
    world["deliver"] = lambda ids, text: {"delivered": 0 if "Asura Scans" in text else 1, "failed": 1}
    go()
    assert world["acked"] == [["w-hive"]]                                  # Asura's alert will be tried again next run


def test_when_no_alert_could_be_delivered_nothing_is_marked(world):
    world["alerts"] = [alert("w-asura")]
    world["last_digest"] = "2026-11-10T18:05:10+00:00"
    world["deliver"] = lambda ids, text: {"delivered": 0, "failed": 2}
    go()
    assert world["acked"] == []


def test_one_delivered_admin_is_enough_to_mark_the_alert_reported(world):
    world["alerts"] = [alert("w-asura")]
    world["last_digest"] = "2026-11-10T18:05:10+00:00"
    world["deliver"] = lambda ids, text: {"delivered": 1, "failed": 1}
    go()
    assert world["acked"] == [["w-asura"]]


def test_no_alerts_means_no_acknowledgement_call(world, capsys):
    world["last_digest"] = "2026-11-10T18:05:10+00:00"
    go()
    assert "ack_site_alerts" not in world["events"]
    assert "0 site alert(s), 0 delivered" in capsys.readouterr().out


def test_the_alert_summary_line_is_counts_only(world, capsys):
    world["alerts"] = [alert("w-asura")]
    world["last_digest"] = "2026-11-10T18:05:10+00:00"
    go()
    out = capsys.readouterr().out
    assert "1 site alert(s), 1 delivered" in out and "fetch failed" not in out and "Asura" not in out


# ---------------------------------------------------------------- the digest

def test_a_due_digest_is_built_sent_and_then_marked_as_sent(world):
    go()
    ids, text = world["sent"][0]
    assert ids == [111, 222] and "ScrollPing daily digest" in text and "1 site healthy" in text and "delivered 5" in text
    assert world["state_writes"] == [("last_digest_sent_at", NOW.isoformat())]
    assert world["events"].index("send_admin") < world["events"].index("set_app_state")


def test_the_digest_is_not_sent_twice_in_one_day(world):
    world["last_digest"] = "2026-11-10T18:05:12+00:00"
    go()
    assert world["sent"] == [] and world["state_writes"] == []
    assert "fetch_site_health_rows" not in world["events"]


def test_yesterdays_marker_makes_todays_digest_due(world):
    world["last_digest"] = "2026-11-09T18:05:12+00:00"
    go()
    assert len(world["sent"]) == 1


def test_a_digest_nobody_received_leaves_the_marker_alone_so_the_next_run_retries(world, capsys):
    world["deliver"] = lambda ids, text: {"delivered": 0, "failed": 2}
    go()
    assert world["state_writes"] == []
    assert "will be tried again" in capsys.readouterr().out


def test_one_admin_receiving_the_digest_is_enough_to_store_the_marker(world):
    world["deliver"] = lambda ids, text: {"delivered": 1, "failed": 1}
    go()
    assert world["state_writes"] == [("last_digest_sent_at", NOW.isoformat())]


def test_a_marker_that_cannot_be_stored_is_reported_but_never_raises(world, capsys):
    world["set_returns"] = False
    go()
    assert "could not store the marker" in capsys.readouterr().out


def test_an_unreadable_marker_skips_the_digest_rather_than_risking_a_duplicate(world, capsys):
    world["state_error"] = RuntimeError("db down")
    go()
    assert world["sent"] == [] and world["state_writes"] == []
    assert "could not read the digest state" in capsys.readouterr().out


def test_a_broken_state_read_does_not_stop_the_alerts_that_come_first(world):
    world["alerts"] = [alert("w-asura")]
    world["state_error"] = RuntimeError("db down")
    go()
    assert world["acked"] == [["w-asura"]] and len(world["sent"]) == 1


def test_alerts_go_out_before_the_digest_in_the_same_run(world):
    world["alerts"] = [alert("w-asura")]
    go()
    assert [("ScrollPing alert" in text) for ids, text in world["sent"]] == [True, False]
    assert world["events"].index("ack_site_alerts") < world["events"].index("get_app_state")


def test_the_digest_works_without_queue_numbers(world):
    world["stats"] = None
    go()
    assert "Queue numbers are unavailable" in world["sent"][0][1]


# ---------------------------------------------------------------- inside run()

