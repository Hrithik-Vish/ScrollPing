"""Pure admin helpers (spec section 12): admin ids, the digest schedule, alert and digest text."""

from datetime import datetime, timedelta, timezone

import pytest

from src import admin

UTC = timezone.utc
ASURA = "https://asurascans.com/"
HIVE = "https://hivetoons.org/"
RULES = {ASURA: {"display_name": "Asura Scans"}, HIVE: {"display_name": "Hive Toons"}}
# 23:35 IST is 18:05 UTC
NOW = datetime(2026, 11, 10, 18, 5, tzinfo=UTC)


def utc(*args):
    return datetime(*args, tzinfo=UTC)


# ---------------------------------------------------------------- admin ids

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("123", [123]),
        ("123,456", [123, 456]),
        (" 123 , 456 ,, 789 ", [123, 456, 789]),
        ("-100123456", [-100123456]),                 # a group chat id
        ("123,abc,456", [123, 456]),
        ("12abc,45", [45]),
        ("+5,5.5,0x1F", []),
        ("--5,-", []),                                 # the spec's lstrip("-") check would crash int() on "--5"
        ("123,123,456,123", [123, 456]),
        ("", []),
        ("   ", []),
        (",,,", []),
        (None, []),
    ],
)
def test_get_admin_chat_ids(raw, expected):
    env = {} if raw is None else {"ADMIN_TELEGRAM_USER_IDS": raw}
    assert admin.get_admin_chat_ids(env) == expected


def test_get_admin_chat_ids_reads_the_real_environment_by_default(monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_USER_IDS", "7, 8")
    assert admin.get_admin_chat_ids() == [7, 8]
    monkeypatch.delenv("ADMIN_TELEGRAM_USER_IDS")
    assert admin.get_admin_chat_ids() == []


# ---------------------------------------------------------------- digest schedule

def test_the_target_is_the_latest_2335_ist_not_in_the_future():
    assert admin.latest_digest_target(NOW) == datetime(2026, 11, 10, 23, 35, tzinfo=admin.IST)
    assert admin.latest_digest_target(NOW - timedelta(seconds=1)) == datetime(2026, 11, 9, 23, 35, tzinfo=admin.IST)
    assert admin.latest_digest_target(NOW + timedelta(hours=5)) == datetime(2026, 11, 10, 23, 35, tzinfo=admin.IST)


def test_after_midnight_the_target_is_still_yesterdays_2335():
    # 06:00 IST on the 11th: the digest that was due at 23:35 IST on the 10th is still the latest target
    assert admin.latest_digest_target(utc(2026, 11, 10, 18, 30)) == datetime(2026, 11, 10, 23, 35, tzinfo=admin.IST)
    assert admin.latest_digest_target(utc(2026, 11, 11, 0, 30)) == datetime(2026, 11, 10, 23, 35, tzinfo=admin.IST)


def test_the_target_follows_the_ist_date_not_the_utc_date():
    # 19:00 UTC on the 10th is already 00:30 IST on the 11th, but the latest 23:35 IST is still the 10th's
    assert admin.latest_digest_target(utc(2026, 11, 10, 19, 0)).date() == datetime(2026, 11, 10).date()
    # 17:00 UTC on the 10th is 22:30 IST on the 10th: the latest target is the 9th's
    assert admin.latest_digest_target(utc(2026, 11, 10, 17, 0)).date() == datetime(2026, 11, 9).date()


def test_naive_times_are_taken_as_utc():
    assert admin.latest_digest_target(datetime(2026, 11, 10, 18, 5)) == admin.latest_digest_target(NOW)


@pytest.mark.parametrize(
    "last_sent, due",
    [
        (None, True),
        ("", True),
        ("garbage", True),                                         # unreadable counts as never sent
        ("2026-11-09T18:05:00+00:00", True),                       # yesterday's digest
        ("2026-11-10T18:04:59+00:00", True),                       # one second before the target
        ("2026-11-10T18:05:00+00:00", False),                      # exactly at the target
        ("2026-11-10T18:05:12+00:00", False),                      # the normal case: sent a few seconds late
        ("2026-11-10T23:59:00+00:00", False),
        ("2026-11-10T18:05:12Z", False),
        ("2026-11-10T23:35:30+05:30", False),                      # the same instant written in IST
        ("2026-11-10T23:34:00+05:30", True),
    ],
)
def test_digest_due(last_sent, due):
    assert admin.digest_due(NOW + timedelta(seconds=30), last_sent) is due


def test_a_delayed_run_after_midnight_still_sends_that_days_digest():
    assert admin.digest_due(utc(2026, 11, 11, 1, 0), "2026-11-09T18:05:10+00:00") is True


def test_simulated_week_sends_exactly_one_digest_per_day_at_the_first_run_after_2335_ist():
    """cron-job.org fires at minute 05 and GitHub's backup at minute 35 of every hour."""
    state, sends = {"last": None}, []
    moment = utc(2026, 11, 9, 0, 5)
    while moment < utc(2026, 11, 16, 0, 5):
        for minute in (5, 35):
            run_time = moment.replace(minute=minute) + timedelta(seconds=20)
            if admin.digest_due(run_time, state["last"]):
                sends.append(run_time)
                state["last"] = run_time.isoformat()
        moment += timedelta(hours=1)
    first = sends[0]                                    # the very first run ever sends at once
    later = [s for s in sends[1:]]
    assert len(later) == 7 and all(s.hour == 18 and s.minute == 5 for s in later)    # 23:35 IST each day, from the minute-05 run
    assert len({s.date() for s in later}) == 7
    assert first == utc(2026, 11, 9, 0, 5, 20)


def test_a_missed_slot_is_caught_up_by_the_next_run():
    state, sends = {"last": "2026-11-09T18:05:10+00:00"}, []
    for run_time in (utc(2026, 11, 10, 18, 35), utc(2026, 11, 10, 19, 5), utc(2026, 11, 10, 19, 35)):
        if admin.digest_due(run_time, state["last"]):
            sends.append(run_time)
            state["last"] = run_time.isoformat()
    assert sends == [utc(2026, 11, 10, 18, 35)]         # sent once, at the first run after the target, and not again


def test_a_failed_send_leaves_the_marker_alone_so_the_next_run_retries():
    last = "2026-11-09T18:05:10+00:00"
    assert admin.digest_due(utc(2026, 11, 10, 18, 5, 20), last) is True
    assert admin.digest_due(utc(2026, 11, 10, 19, 5, 20), last) is True      # still due: nothing was stored


# ---------------------------------------------------------------- formatting

@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-11-10T04:05:00+00:00", "09:35 AM"),          # 04:05 UTC = 09:35 IST, same day as NOW in IST
        ("2026-11-10T18:30:00+00:00", "12:00 AM"),          # midnight IST (on the 11th, so with a date)
    ],
)
def test_format_ist_clock_and_date(value, expected):
    result = admin.format_ist(value, NOW)
    assert result.endswith(expected)


def test_format_ist_omits_the_date_only_for_today_in_ist():
    assert admin.format_ist("2026-11-10T04:05:00+00:00", NOW) == "09:35 AM"
    assert admin.format_ist("2026-11-09T04:05:00+00:00", NOW) == "9 Nov, 09:35 AM"
    assert admin.format_ist("2026-11-10T04:05:00+00:00") == "10 Nov, 09:35 AM"      # no clock given: always with the date


@pytest.mark.parametrize(
    "hour_utc, minute, expected",
    [(18, 30, "12:00 AM"), (6, 30, "12:00 PM"), (18, 5, "11:35 PM"), (0, 0, "05:30 AM"), (7, 31, "01:01 PM")],
)
def test_format_ist_twelve_hour_clock_edges(hour_utc, minute, expected):
    moment = utc(2026, 11, 10, hour_utc, minute)
    assert admin.format_ist(moment).endswith(expected)


@pytest.mark.parametrize("bad", [None, "", "garbage", 12])
def test_format_ist_unreadable_values(bad):
    assert admin.format_ist(bad) == "?"


def test_site_label_uses_the_display_name_then_the_host():
    assert admin.site_label(ASURA, RULES) == "Asura Scans"
    assert admin.site_label("https://www.example.org/x", {}) == "example.org"
    assert admin.site_label(None, RULES) == "Unknown site"


def test_short_error():
    assert admin.short_error("  a \n  b ") == "a b"
    assert admin.short_error(None) == "unknown" and admin.short_error("   ") == "unknown"
    assert len(admin.short_error("x" * 1000)) == admin.MAX_ERROR_SHOWN and admin.short_error("x" * 1000).endswith("\u2026")


# ---------------------------------------------------------------- the alert

def alert_row(**overrides):
    row = {"web_id": "w1", "web_domain_name": ASURA, "consecutive_failures": 4,
           "failing_since": "2026-11-10T14:05:00+00:00", "last_error": "fetch failed"}
    row.update(overrides)
    return row


def test_alert_layout():
    assert admin.build_alert(alert_row(), RULES) == (
        "\U0001F6A8 <b>ScrollPing alert</b>\n"
        "<b>Asura Scans</b> has failed 4 runs in a row.\n"
        "Since: 10 Nov, 07:35 PM IST\n"
        "Error: <code>fetch failed</code>"
    )


def test_alert_escapes_everything_that_is_not_literal_text():
    evil = '<b>&"</b>'
    text = admin.build_alert(alert_row(last_error=evil, web_domain_name="https://x.example/"), {"https://x.example/": {"display_name": evil}})
    assert "<b>&" not in text.replace("<b>&lt;", "")
    assert text.count("&lt;b&gt;&amp;\"&lt;/b&gt;") == 2
    assert text.count("<code>") == 1 and text.count("</code>") == 1


def test_alert_handles_missing_details():
    text = admin.build_alert(alert_row(failing_since=None, last_error=None, web_domain_name="https://www.vortexscans.org/"), {})
    assert "Since: unknown" in text and "<code>unknown</code>" in text and "<b>vortexscans.org</b>" in text


def test_alert_truncates_a_huge_error():
    text = admin.build_alert(alert_row(last_error="e" * 5000), RULES)
    assert len(text) < 600


# ---------------------------------------------------------------- the digest

def health(domain=HIVE, **overrides):
    row = {"web_id": "w", "web_domain_name": domain, "last_status": "ok", "consecutive_failures": 0, "failure_alerted": False,
           "failing_since": None, "last_failed_at": None, "last_resolved_at": None, "last_error": None}
    row.update(overrides)
    return row


STATS = {"sent_24h": 41, "retried_24h": 2, "dead_24h": 0, "cancelled_24h": 1, "pending_now": 3, "sending_now": 0}


def digest(rows, stats=STATS, now=NOW):
    return admin.build_digest(rows, stats, now, RULES)


def test_a_healthy_week_is_one_line_plus_the_queue_block():
    text = digest([health(ASURA), health(HIVE), health("https://vortexscans.org/")])
    assert text.split("\n") == [
        "\U0001F4CA <b>ScrollPing daily digest</b> \u00B7 10 Nov, 11:35 PM IST",
        "",
        "\u2705 All 3 sites healthy",
        "",
        "\U0001F4EC Last 24h: delivered 41 \u00B7 retried 2 \u00B7 dead 0 \u00B7 cancelled 1",
        "Waiting now: 3 \u00B7 in flight: 0",
    ]


def test_one_site_wording():
    assert "\u2705 1 site healthy" in digest([health(HIVE)])


def test_currently_failing_site():
    text = digest([health(ASURA, last_status="failed", consecutive_failures=5, failing_since="2026-11-10T14:05:00+00:00", last_error="fetch failed"), health(HIVE)])
    assert "\u2022 <b>Asura Scans</b> \u2014 \u274C Currently failing since 07:35 PM (5 runs) \u00B7 fetch failed" in text
    assert "\u2022 <b>Hive Toons</b> \u2014 \u2705 Healthy" in text
    assert "All" not in text


def test_a_resolved_issue_within_24_hours():
    row = health(HIVE, last_failed_at="2026-11-10T03:05:00+00:00", last_resolved_at="2026-11-10T04:05:00+00:00")
    assert "\u2022 <b>Hive Toons</b> \u2014 \u26A0\uFE0F Issue resolved \u00B7 failed 08:35 AM, resolved 09:35 AM" in digest([row, health(ASURA, last_status="degraded", last_error="x")])


def test_a_failure_older_than_24_hours_is_forgotten():
    row = health(HIVE, last_failed_at=(NOW - timedelta(hours=25)).isoformat(), last_resolved_at=(NOW - timedelta(hours=24)).isoformat())
    assert "All 1 sites healthy" not in digest([row]) and "\u2705 1 site healthy" in digest([row])
    row = health(HIVE, last_failed_at=(NOW - timedelta(hours=23, minutes=59)).isoformat())
    assert "Issue resolved" in digest([row])


def test_resolved_issues_show_the_date_when_it_was_not_today():
    # failed 23:45 IST on the 9th (within 24h of the digest), resolved 00:15 IST on the 10th (today)
    row = health(HIVE, last_failed_at="2026-11-09T18:15:00+00:00", last_resolved_at="2026-11-09T18:45:00+00:00")
    assert "failed 9 Nov, 11:45 PM, resolved 12:15 AM" in digest([row])


def test_a_resolved_issue_without_a_resolved_time_still_renders():
    text = digest([health(HIVE, last_failed_at="2026-11-10T03:05:00+00:00"), health(ASURA, last_status="degraded", last_error="x")])
    assert "Issue resolved \u00B7 failed 08:35 AM" in text and "resolved 09" not in text


def test_degraded_site():
    text = digest([health(HIVE, last_status="degraded", last_error="no titles found")])
    assert "\u2022 <b>Hive Toons</b> \u2014 \U0001F7E1 Degraded \u00B7 no titles found" in text


def test_the_first_matching_case_wins():
    failing_and_recent = health(HIVE, last_status="failed", consecutive_failures=3, failing_since="2026-11-10T14:05:00+00:00",
                                last_failed_at=NOW.isoformat(), last_error="x")
    assert "Currently failing" in digest([failing_and_recent]) and "Issue resolved" not in digest([failing_and_recent])
    recent_and_degraded = health(HIVE, last_status="degraded", last_failed_at=(NOW - timedelta(hours=3)).isoformat(), last_error="y")
    assert "Issue resolved" in digest([recent_and_degraded]) and "Degraded" not in digest([recent_and_degraded])


def test_sites_are_listed_alphabetically_by_display_name():
    rows = [health(HIVE, last_status="degraded", last_error="a"), health(ASURA, last_status="degraded", last_error="b"),
            health("https://zzz.example/", last_status="degraded", last_error="c")]
    text = digest(rows)
    assert text.index("Asura Scans") < text.index("Hive Toons") < text.index("zzz.example")


def test_digest_escapes_errors_and_names():
    evil = '<i>&"</i>'
    rows = [health(HIVE, last_status="degraded", last_error=evil)]
    text = admin.build_digest(rows, STATS, NOW, {HIVE: {"display_name": evil}})
    assert "<i>" not in text and text.count("&lt;i&gt;&amp;\"&lt;/i&gt;") == 2


def test_missing_queue_numbers_are_reported_not_crashed_on():
    assert "Queue numbers are unavailable" in digest([health()], stats=None)


def test_a_partial_stats_row_does_not_crash():
    assert "delivered 0" in digest([health()], stats={"pending_now": 2})


def test_no_site_rows_yet():
    assert "No site health recorded yet." in digest([]) and "No site health recorded yet." in digest(None)


def test_the_digest_stays_well_under_telegrams_limit_with_many_failing_sites():
    rows = [health(f"https://site{i}.example/", last_status="failed", consecutive_failures=9, failing_since=NOW.isoformat(), last_error="e" * 1000) for i in range(10)]
    assert len(digest(rows)) < 4000


def test_build_functions_do_not_mutate_their_input():
    rows = [health(HIVE, last_status="degraded", last_error="x")]
    snapshot = [dict(r) for r in rows]
    admin.build_digest(rows, dict(STATS), NOW, RULES)
    assert rows == snapshot
