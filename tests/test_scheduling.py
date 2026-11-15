"""Pure scheduling logic (spec sections 8 and 10.3): resolve_due_at, build_queue_records, badge_for."""

from datetime import datetime, timedelta, timezone

import pytest

from src import scheduling

NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
FUTURE = NOW + timedelta(hours=5)
PAST = NOW - timedelta(hours=1)


# ----------------------------------------------------------------------------------- parse_utc

def test_parse_utc_accepts_datetimes_and_iso_text():
    assert scheduling.parse_utc(NOW) == NOW
    assert scheduling.parse_utc("2026-11-10T12:00:00+00:00") == NOW
    assert scheduling.parse_utc("2026-11-10T12:00:00Z") == NOW
    assert scheduling.parse_utc("2026-11-10T17:30:00+05:30") == NOW
    assert scheduling.parse_utc("2026-11-10T12:00:00.123456+00:00").microsecond == 123456


def test_parse_utc_treats_naive_as_utc_and_converts_zones():
    assert scheduling.parse_utc(datetime(2026, 11, 10, 12, 0)) == NOW
    ist = datetime(2026, 11, 10, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert scheduling.parse_utc(ist) == NOW and scheduling.parse_utc(ist).utcoffset() == timedelta(0)


@pytest.mark.parametrize("bad", [None, "", "   ", "garbage", 12345, [], "2026-13-45T00:00:00Z"])
def test_parse_utc_returns_none_for_unusable_values(bad):
    assert scheduling.parse_utc(bad) is None


# ----------------------------------------------------------------------------------- resolve_due_at

@pytest.mark.parametrize("access_state", ["free", "unknown", None, "anything-else"])
@pytest.mark.parametrize("tier", ["standard", "member"])
@pytest.mark.parametrize("timing", [None, "early", "when_free"])
def test_anything_not_locked_is_delivered_now(access_state, tier, timing):
    assert scheduling.resolve_due_at(access_state, FUTURE, timing, tier, NOW) == NOW


@pytest.mark.parametrize("unlock", [None, PAST, NOW, "garbage", ""])
@pytest.mark.parametrize("timing", [None, "early", "when_free"])
def test_locked_without_a_usable_future_unlock_is_delivered_now(unlock, timing):
    assert scheduling.resolve_due_at("locked", unlock, timing, "standard", NOW) == NOW


@pytest.mark.parametrize(
    "tier, timing, delivered_early",
    [
        ("standard", None, False),        # default for standard: wait until it is free
        ("member", None, True),           # default for members: early
        ("standard", "early", True),      # per-novel override wins over the tier
        ("member", "when_free", False),
        ("standard", "when_free", False),
        ("member", "early", True),
        (None, None, False),              # unknown tier behaves like standard
        ("platinum", None, False),
    ],
)
def test_locked_with_a_future_unlock_follows_the_override_then_the_tier(tier, timing, delivered_early):
    due = scheduling.resolve_due_at("locked", FUTURE, timing, tier, NOW)
    assert due == (NOW if delivered_early else FUTURE)


def test_an_unrecognised_timing_value_falls_back_to_the_tier_default():
    assert scheduling.resolve_due_at("locked", FUTURE, "sometime", "member", NOW) == NOW
    assert scheduling.resolve_due_at("locked", FUTURE, "sometime", "standard", NOW) == FUTURE


def test_resolve_due_at_accepts_iso_text_and_returns_aware_utc():
    due = scheduling.resolve_due_at("locked", "2026-11-10T17:00:00Z", None, "standard", "2026-11-10T12:00:00Z")
    assert due == FUTURE and due.tzinfo is not None and due.utcoffset() == timedelta(0)


# ----------------------------------------------------------------------------------- build_queue_records

def row(novel_id="n1", chapter=10.0, access_state="free", unlock_at=None, web_id="w1", url=None):
    return {"id": novel_id, "novel_name": "slug", "latest_chap": chapter, "chapter_url": url or f"https://x/{novel_id}/{chapter}",
            "display_title": None, "access_state": access_state, "unlock_at": unlock_at, "web_id": web_id}


def who(novel_id="n1", user_id="u1", timing=None, tg=111):
    return {"novel_id": novel_id, "user_id": user_id, "alert_timing": timing, "telegram_user_id": tg}


KEYS = {"user_id", "novel_id", "chapter", "chapter_url", "access_state", "unlock_at", "due_at", "status"}


def test_one_record_per_row_and_recipient_with_uniform_keys():
    records = scheduling.build_queue_records(
        [row("n1"), row("n2", 5.0)], [who("n1", "u1"), who("n1", "u2"), who("n2", "u1")], {}, NOW)
    assert [(r["novel_id"], r["user_id"]) for r in records] == [("n1", "u1"), ("n1", "u2"), ("n2", "u1")]
    assert all(set(r) == KEYS for r in records)
    assert all(r["status"] == "pending" and r["due_at"] == NOW.isoformat() for r in records)
    assert records[2]["chapter"] == 5.0 and records[2]["chapter_url"] == "https://x/n2/5.0"


def test_recipients_of_other_novels_and_rows_nobody_reads_produce_nothing():
    assert scheduling.build_queue_records([row("n1")], [who("n9")], {}, NOW) == []
    assert scheduling.build_queue_records([row("n1")], [], {}, NOW) == []
    assert scheduling.build_queue_records([], [who("n1")], {}, NOW) == []
    assert scheduling.build_queue_records(None, None, None, NOW) == []


def test_a_locked_chapter_gets_a_different_due_time_per_recipient():
    locked = row("n1", access_state="locked", unlock_at=FUTURE.isoformat())
    recipients = [who("n1", "standard-user"), who("n1", "member-user"), who("n1", "override-user", "early"), who("n1", "member-waits", "when_free")]
    tiers = {("member-user", "w1"): "member", ("member-waits", "w1"): "member"}
    due = {r["user_id"]: r["due_at"] for r in scheduling.build_queue_records([locked], recipients, tiers, NOW)}
    assert due == {
        "standard-user": FUTURE.isoformat(),
        "member-user": NOW.isoformat(),
        "override-user": NOW.isoformat(),
        "member-waits": FUTURE.isoformat(),
    }


def test_the_tier_is_looked_up_for_the_site_of_that_novel():
    locked_w1 = row("n1", access_state="locked", unlock_at=FUTURE.isoformat(), web_id="w1")
    locked_w2 = row("n2", access_state="locked", unlock_at=FUTURE.isoformat(), web_id="w2")
    tiers = {("u1", "w1"): "member"}                         # member on site 1 only
    records = scheduling.build_queue_records([locked_w1, locked_w2], [who("n1"), who("n2")], tiers, NOW)
    assert [r["due_at"] for r in records] == [NOW.isoformat(), FUTURE.isoformat()]


def test_locked_records_keep_the_unlock_time_and_free_records_drop_it():
    locked = row("n1", access_state="locked", unlock_at="2026-11-10T17:00:00Z")
    free_with_old_unlock = row("n2", access_state="free", unlock_at="2026-11-06T06:04:28+00:00")
    records = scheduling.build_queue_records([locked, free_with_old_unlock], [who("n1"), who("n2")], {}, NOW)
    assert records[0]["unlock_at"] == FUTURE.isoformat() and records[0]["access_state"] == "locked"
    assert records[1]["unlock_at"] is None and records[1]["access_state"] == "free"


def test_unknown_lock_state_is_delivered_now_and_recorded_as_unknown():
    records = scheduling.build_queue_records([row("n1", access_state="unknown")], [who("n1")], {}, NOW)
    assert records[0]["access_state"] == "unknown" and records[0]["due_at"] == NOW.isoformat()


def test_locked_without_an_unlock_time_is_delivered_now_with_no_stored_unlock():
    records = scheduling.build_queue_records([row("n1", access_state="locked", unlock_at=None)], [who("n1")], {}, NOW)
    assert records[0]["due_at"] == NOW.isoformat() and records[0]["unlock_at"] is None


def test_a_missing_access_state_counts_as_free():
    r = row("n1")
    del r["access_state"]
    assert scheduling.build_queue_records([r], [who("n1")], {}, NOW)[0]["access_state"] == "free"


def test_a_duplicate_recipient_gives_one_record():
    records = scheduling.build_queue_records([row("n1")], [who("n1", "u1"), who("n1", "u1")], {}, NOW)
    assert len(records) == 1


def test_rows_without_a_url_or_chapter_are_ignored():
    bad_url = row("n1")
    bad_url["chapter_url"] = None
    bad_chapter = row("n2")
    bad_chapter["latest_chap"] = None
    assert scheduling.build_queue_records([bad_url, bad_chapter], [who("n1"), who("n2")], {}, NOW) == []


def test_chapter_is_a_float_and_inputs_are_not_mutated():
    rows, recipients, tiers = [row("n1", chapter=7)], [who("n1")], {("u1", "w1"): "member"}
    snapshot = (repr(rows), repr(recipients), repr(tiers))
    records = scheduling.build_queue_records(rows, recipients, tiers, NOW)
    assert records[0]["chapter"] == 7.0 and isinstance(records[0]["chapter"], float)
    assert (repr(rows), repr(recipients), repr(tiers)) == snapshot


def test_a_row_without_web_id_uses_the_standard_tier():
    r = row("n1", access_state="locked", unlock_at=FUTURE.isoformat())
    del r["web_id"]
    records = scheduling.build_queue_records([r], [who("n1")], {("u1", "w1"): "member"}, NOW)
    assert records[0]["due_at"] == FUTURE.isoformat()


# ----------------------------------------------------------------------------------- format_remaining / badge_for

@pytest.mark.parametrize(
    "seconds, expected",
    [
        (1, "<1h"), (59 * 60, "<1h"), (3599, "<1h"),
        (3600, "1h"), (3601, "2h"), (5 * 3600, "5h"), (5 * 3600 + 1, "6h"),
        (47 * 3600, "47h"), (47 * 3600 + 1, "48h"), (48 * 3600 - 1, "48h"),
        (48 * 3600, "2d"), (71 * 3600, "2d"), (72 * 3600, "3d"), (10 * 86400, "10d"),
    ],
)
def test_format_remaining(seconds, expected):
    assert scheduling.format_remaining(seconds) == expected


def badge(access_state="locked", unlock_at=None, now=NOW):
    return scheduling.badge_for({"access_state": access_state, "unlock_at": unlock_at}, now)


def test_locked_with_a_future_unlock_shows_the_time_left():
    assert badge(unlock_at=(NOW + timedelta(hours=5)).isoformat()) == " 🔒 free in ~5h"
    assert badge(unlock_at=(NOW + timedelta(minutes=20)).isoformat()) == " 🔒 free in ~<1h"
    assert badge(unlock_at=(NOW + timedelta(days=3)).isoformat()) == " 🔒 free in ~3d"


def test_the_badge_is_computed_from_the_time_of_sending_not_of_queueing():
    unlock = (NOW + timedelta(hours=5)).isoformat()
    assert badge(unlock_at=unlock, now=NOW) == " 🔒 free in ~5h"
    assert badge(unlock_at=unlock, now=NOW + timedelta(hours=4, minutes=30)) == " 🔒 free in ~<1h"
    assert badge(unlock_at=unlock, now=NOW + timedelta(hours=5)) == ""           # it is free now
    assert badge(unlock_at=unlock, now=NOW + timedelta(hours=9)) == ""


def test_locked_with_an_unknown_unlock_says_early_access():
    assert badge(unlock_at=None) == " 🔒 early access"
    assert badge(unlock_at="garbage") == " 🔒 early access"


@pytest.mark.parametrize("state", ["free", "unknown", None, ""])
def test_free_and_unknown_chapters_have_no_badge(state):
    assert badge(access_state=state, unlock_at=(NOW + timedelta(hours=5)).isoformat()) == ""


def test_a_missing_access_state_key_is_no_badge_and_datetimes_are_accepted():
    assert scheduling.badge_for({}, NOW) == ""
    assert scheduling.badge_for({"access_state": "locked", "unlock_at": NOW + timedelta(hours=2)}, NOW) == " 🔒 free in ~2h"
