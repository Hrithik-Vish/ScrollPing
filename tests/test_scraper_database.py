"""Phase 3: scraper-side database.py functions (titles, subscribers, queue, health, app state).

A scripted fake Supabase client stands in for the network. Each .execute() pops the next scripted
response (rows / an Exception to raise / a callable that receives the call), and every call is
logged, so the tests can check exactly what was sent. time.sleep is patched out.
"""

from datetime import datetime, timezone

import pytest

from src import database


class Result:
    def __init__(self, data):
        self.data = data


class Call:
    def __init__(self, kind, name, ops=None, params=None):
        self.kind, self.name, self.ops, self.params = kind, name, ops or [], params

    def op(self, op_name):
        return [(a, k) for n, a, k in self.ops if n == op_name]

    def first(self, op_name):
        found = self.op(op_name)
        assert found, f"{op_name} was not called; ops were {[n for n, _, _ in self.ops]}"
        return found[0]


class FakeQuery:
    def __init__(self, client, kind, name, params=None):
        self.client, self.call = client, Call(kind, name, params=params)

    def __getattr__(self, op_name):
        def method(*args, **kwargs):
            self.call.ops.append((op_name, args, kwargs))
            return self
        return method

    def execute(self):
        self.client.calls.append(self.call)
        assert self.client.responses, f"unexpected extra database call: {self.call.kind} {self.call.name}"
        response = self.client.responses.pop(0)
        if callable(response):
            response = response(self.call)
        if isinstance(response, Exception):
            raise response
        return Result(response)


class FakeSupabase:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def table(self, name):
        return FakeQuery(self, "table", name)

    def rpc(self, name, params):          # params is REQUIRED, exactly like postgrest-py's rpc(func, params)
        assert isinstance(params, dict), "rpc() needs a params dict (postgrest-py has no default)"
        return FakeQuery(self, "rpc", name, params=params)


@pytest.fixture
def db(monkeypatch):
    sleeps = []
    monkeypatch.setattr(database.time, "sleep", lambda seconds: sleeps.append(seconds))

    def install(*responses):
        client = FakeSupabase(responses)
        monkeypatch.setattr(database, "supabase", client)
        client.sleeps = sleeps
        return client
    return install


class Boom(Exception):
    pass


# --------------------------------------------------------------------------- update_novels_table

ROWS = [
    {"id": "n1", "novel_name": "a", "latest_chap": 10.0, "chapter_url": "https://x/a/10", "display_title": "A"},
    {"id": "n2", "novel_name": "b", "latest_chap": 5.5, "chapter_url": "https://x/b/5.5"},
    {"id": "n3", "novel_name": "c", "latest_chap": 7.0, "chapter_url": "https://x/c/7", "display_title": None},
]


def test_update_novels_table_with_nothing_makes_no_call(db):
    client = db()
    assert database.update_novels_table([]) == []
    assert database.update_novels_table(None) == []
    assert client.calls == []


def test_update_novels_table_sends_one_rpc_with_only_the_needed_fields(db):
    client = db([{"updated_id": "n1"}, {"updated_id": "n2"}, {"updated_id": "n3"}])
    assert database.update_novels_table(ROWS) == []
    assert len(client.calls) == 1
    call = client.calls[0]
    assert (call.kind, call.name) == ("rpc", "update_novels_batch")
    assert call.params == {"items": [
        {"id": "n1", "latest_chap": 10.0, "chapter_url": "https://x/a/10", "display_title": "A"},
        {"id": "n2", "latest_chap": 5.5, "chapter_url": "https://x/b/5.5", "display_title": None},
        {"id": "n3", "latest_chap": 7.0, "chapter_url": "https://x/c/7", "display_title": None},
    ]}
    assert all("updated_at" not in item for item in call.params["items"])      # the SQL sets it


def test_update_novels_table_reports_ids_missing_from_the_result(db):
    db([{"updated_id": "n1"}, {"updated_id": "n3"}])
    assert database.update_novels_table(ROWS) == ["n2"]


def test_update_novels_table_retries_the_batch_once_before_falling_back(db):
    client = db(Boom("blip"), [{"updated_id": "n1"}, {"updated_id": "n2"}, {"updated_id": "n3"}])
    assert database.update_novels_table(ROWS) == []
    assert len(client.calls) == 2 and client.sleeps == [database.RETRY_SLEEP_SECONDS]


def test_update_novels_table_isolates_the_bad_row_when_the_batch_keeps_failing(db):
    client = db(
        Boom("numeric overflow"), Boom("numeric overflow"),          # whole batch, both attempts
        [{"updated_id": "n1"}],                                      # n1 ok
        Boom("still bad"),                                           # n2 fails on its own
        [],                                                          # n3 returns nothing = not written
    )
    assert database.update_novels_table(ROWS) == ["n2", "n3"]
    singles = client.calls[2:]
    assert [c.params["items"][0]["id"] for c in singles] == ["n1", "n2", "n3"]
    assert all(len(c.params["items"]) == 1 for c in singles)


def test_update_novels_table_everything_failing_returns_every_id_without_raising(db):
    db(*([Boom("down")] * 5))
    assert database.update_novels_table(ROWS) == ["n1", "n2", "n3"]


# --------------------------------------------------------------------------- sync_novel_titles

def test_sync_novel_titles_sends_only_real_titles_and_returns_changed_ids(db):
    client = db([{"updated_id": "n1"}])
    updates = [
        {"id": "n1", "display_title": "New Title"},
        {"id": "n2", "display_title": None},
        {"id": "n3", "display_title": ""},
        {"id": None, "display_title": "orphan"},
    ]
    assert database.sync_novel_titles(updates) == ["n1"]
    assert client.calls[0].name == "sync_novel_titles"
    assert client.calls[0].params == {"items": [{"id": "n1", "display_title": "New Title"}]}


def test_sync_novel_titles_with_nothing_to_do_makes_no_call(db):
    client = db()
    assert database.sync_novel_titles([]) == []
    assert database.sync_novel_titles(None) == []
    assert database.sync_novel_titles([{"id": "n1", "display_title": None}]) == []
    assert client.calls == []


def test_sync_novel_titles_chunks_and_survives_a_failed_chunk(db):
    updates = [{"id": f"n{i}", "display_title": f"T{i}"} for i in range(250)]
    client = db(Boom("x"), Boom("x"), [{"updated_id": "n200"}])      # chunk 1 fails twice, chunk 2 works
    assert database.sync_novel_titles(updates) == ["n200"]
    assert [len(c.params["items"]) for c in client.calls] == [200, 200, 50]


# --------------------------------------------------------------------------- fetch_subscribers_with_preferences

def sub(novel, user, tg, timing=None):
    return {"novel_id": novel, "user_id": user, "alert_timing": timing, "users": {"telegram_user_id": tg}}


def test_subscribers_query_shape_and_flattened_result(db):
    client = db(
        [sub("n1", "u1", 111, "early"), sub("n1", "u2", 222), {"novel_id": "n1", "user_id": "u3", "alert_timing": None, "users": None}],
        [{"user_id": "u1", "web_id": "w1", "tier": "member"}],
    )
    result = database.fetch_subscribers_with_preferences(["n1"], {"n1": "w1"})

    first = client.calls[0]
    assert first.name == "user_subscriptions"
    assert first.first("select")[0] == ("novel_id, user_id, alert_timing, users!inner(telegram_user_id)",)
    assert first.first("in_")[0] == ("novel_id", ["n1"])
    assert ("reading_status", "reading") in [a for a, _ in first.op("eq")]
    assert ("users.status", "active") in [a for a, _ in first.op("eq")]

    assert result.recipients == [
        {"novel_id": "n1", "user_id": "u1", "alert_timing": "early", "telegram_user_id": 111},
        {"novel_id": "n1", "user_id": "u2", "alert_timing": None, "telegram_user_id": 222},
    ]
    assert result.tiers == {("u1", "w1"): "member"}
    assert result.failed_novel_ids == set() and result.ok is True


def test_tier_query_is_limited_to_those_users_and_the_sites_involved(db):
    client = db([sub("n1", "u1", 1), sub("n2", "u2", 2)], [])
    database.fetch_subscribers_with_preferences(["n1", "n2"], {"n1": "w1", "n2": "w1", "n9": "w9"})
    tier_call = client.calls[1]
    assert tier_call.name == "user_site_tiers"
    assert tier_call.first("select")[0] == ("user_id, web_id, tier",)
    ins = dict(a for a, _ in tier_call.op("in_"))
    assert ins["user_id"] == ["u1", "u2"] and ins["web_id"] == ["w1"]       # w9 belongs to an unrelated novel


def test_without_a_web_map_tiers_are_read_for_the_users_only(db):
    client = db([sub("n1", "u1", 1)], [{"user_id": "u1", "web_id": "w1", "tier": "member"}])
    result = database.fetch_subscribers_with_preferences(["n1"])
    assert [a[0] for a, _ in client.calls[1].op("in_")] == ["user_id"]
    assert result.tiers == {("u1", "w1"): "member"}


def test_no_recipients_means_no_tier_query(db):
    client = db([])
    result = database.fetch_subscribers_with_preferences(["n1"], {"n1": "w1"})
    assert result.recipients == [] and result.tiers == {} and result.ok
    assert len(client.calls) == 1


def test_empty_or_duplicate_novel_ids(db):
    client = db()
    result = database.fetch_subscribers_with_preferences([], {})
    assert result == ([], {}, set()) and client.calls == []

    client = db([])
    database.fetch_subscribers_with_preferences(["n1", "n1", "n1"], {})
    assert client.calls[0].first("in_")[0] == ("novel_id", ["n1"])


def test_novel_ids_are_chunked_and_chunk_results_are_combined(db):
    ids = [f"n{i}" for i in range(250)]
    client = db([sub("n0", "u1", 1)], [sub("n100", "u2", 2)], [sub("n200", "u3", 3)], [])
    result = database.fetch_subscribers_with_preferences(ids, {})
    sizes = [len(c.first("in_")[0][1]) for c in client.calls[:3]]
    assert sizes == [100, 100, 50]
    assert [r["user_id"] for r in result.recipients] == ["u1", "u2", "u3"]


def test_transient_subscriber_failure_is_retried(db):
    client = db(Boom("blip"), Boom("blip"), [sub("n1", "u1", 1)], [])
    result = database.fetch_subscribers_with_preferences(["n1"], {})
    assert result.ok and len(result.recipients) == 1
    assert client.sleeps == [database.RETRY_SLEEP_SECONDS] * 2


def test_exhausted_retries_hold_the_novels_back_instead_of_pretending_nobody_reads_them(db):
    client = db(*([Boom("down")] * 5))
    result = database.fetch_subscribers_with_preferences(["n1", "n2"], {})
    assert result.recipients == [] and result.failed_novel_ids == {"n1", "n2"} and result.ok is False
    assert len(client.calls) == 5 and len(client.sleeps) == 4


def test_one_failed_chunk_only_holds_back_its_own_novels(db):
    ids = [f"n{i}" for i in range(150)]
    db([sub("n0", "u1", 1)], *([Boom("down")] * 5), [])
    result = database.fetch_subscribers_with_preferences(ids, {})
    assert [r["novel_id"] for r in result.recipients] == ["n0"]
    assert result.failed_novel_ids == set(ids[100:])


def test_failed_tier_lookup_holds_back_those_users_novels_rather_than_guessing_standard(db):
    db([sub("n1", "u1", 1), sub("n2", "u2", 2)], *([Boom("down")] * 3))
    result = database.fetch_subscribers_with_preferences(["n1", "n2"], {"n1": "w1", "n2": "w1"})
    assert result.recipients == [] and result.failed_novel_ids == {"n1", "n2"}


def test_result_unpacks_as_a_triple(db):
    db([sub("n1", "u1", 1)], [])
    recipients, tiers, failed = database.fetch_subscribers_with_preferences(["n1"], {"n1": "w1"})
    assert len(recipients) == 1 and tiers == {} and failed == set()


# --------------------------------------------------------------------------- insert_queue_records

def record(i=1, **overrides):
    base = {"user_id": f"u{i}", "novel_id": "n1", "chapter": 10.0, "chapter_url": f"https://x/{i}"}
    base.update(overrides)
    return base


def test_insert_queue_records_upserts_with_ignore_duplicates_and_uniform_keys(db):
    client = db([{"id": "q1"}, {"id": "q2"}])
    due = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    result = database.insert_queue_records([
        record(1),
        record(2, access_state="locked", unlock_at=due, due_at=due, status="pending"),
    ])
    call = client.calls[0]
    assert call.name == "pending_notifications"
    args, kwargs = call.first("upsert")
    assert kwargs == {"on_conflict": "user_id,novel_id,chapter", "ignore_duplicates": True}
    batch = args[0]
    assert all(set(r) == set(database.QUEUE_RECORD_KEYS) for r in batch)       # PostgREST needs identical keys
    assert batch[0]["access_state"] == "free" and batch[0]["unlock_at"] is None and batch[0]["status"] == "pending"
    assert datetime.fromisoformat(batch[0]["due_at"]).tzinfo is not None
    assert batch[1]["unlock_at"] == batch[1]["due_at"] == due.isoformat() and batch[1]["access_state"] == "locked"
    assert result == {"attempted": 2, "inserted": 2, "failed": 0, "failed_novel_ids": set()}


def test_insert_queue_records_batches_of_500(db):
    client = db([{"id": 1}] * 500, [{"id": 1}] * 500, [{"id": 1}] * 201)
    result = database.insert_queue_records([record(i) for i in range(1201)])
    assert [len(c.first("upsert")[0][0]) for c in client.calls] == [500, 500, 201]
    assert result["attempted"] == 1201 and result["inserted"] == 1201


def test_duplicate_rows_inserted_zero_is_not_an_error(db):
    db([])                                                   # everything already queued
    result = database.insert_queue_records([record(1)])
    assert result["inserted"] == 0 and result["failed"] == 0


def test_insert_queue_records_rejects_a_record_missing_required_fields_before_any_call(db):
    client = db()
    for missing in ("user_id", "novel_id", "chapter", "chapter_url"):
        bad = record(1)
        del bad[missing]
        with pytest.raises(ValueError, match=missing):
            database.insert_queue_records([record(2), bad])
    assert client.calls == []


def test_a_failed_batch_is_reported_and_the_next_batch_is_still_tried(db):
    records = [record(i, novel_id="n_a") for i in range(500)] + [record(i + 500, novel_id="n_b") for i in range(5)]
    client = db(Boom("x"), Boom("x"), Boom("x"), [{"id": 1}] * 5)
    result = database.insert_queue_records(records)
    assert result["failed"] == 500 and result["failed_novel_ids"] == {"n_a"} and result["inserted"] == 5
    assert len(client.calls) == 4


def test_insert_queue_records_retries_a_transient_failure(db):
    client = db(Boom("blip"), [{"id": 1}])
    result = database.insert_queue_records([record(1)])
    assert result["inserted"] == 1 and result["failed"] == 0 and len(client.sleeps) == 1


def test_insert_queue_records_with_nothing_makes_no_call(db):
    client = db()
    assert database.insert_queue_records([]) == {"attempted": 0, "inserted": 0, "failed": 0, "failed_novel_ids": set()}
    assert database.insert_queue_records(None)["attempted"] == 0
    assert client.calls == []


# --------------------------------------------------------------------------- maintain / claim

def test_maintain_queue_returns_the_counts(db):
    client = db([{"recovered": 2, "cancelled": 1, "expired": 3}])
    assert database.maintain_queue() == {"ok": True, "recovered": 2, "cancelled": 1, "expired": 3}
    assert (client.calls[0].kind, client.calls[0].name) == ("rpc", "maintain_notification_queue")
    assert client.calls[0].params == {}


def test_maintain_queue_failure_is_soft(db):
    db(*([Boom("down")] * 3))
    assert database.maintain_queue() == {"ok": False, "recovered": 0, "cancelled": 0, "expired": 0}


def test_maintain_queue_handles_an_empty_response(db):
    db([])
    assert database.maintain_queue() == {"ok": True, "recovered": 0, "cancelled": 0, "expired": 0}


def test_claim_due_passes_the_limit_and_returns_rows(db):
    rows = [{"notification_id": "q1", "telegram_user_id": 5, "novel_name": "a", "chapter": 3.0}]
    client = db(rows, [])
    assert database.claim_due() == rows
    assert client.calls[0].params == {"batch_limit": 500}
    assert database.claim_due(25) == []
    assert client.calls[1].params == {"batch_limit": 25}


def test_claim_due_failure_returns_nothing_instead_of_raising(db):
    client = db(Boom("down"), Boom("down"))
    assert database.claim_due() == []
    assert len(client.calls) == 2


# --------------------------------------------------------------------------- ack / release / dead

@pytest.mark.parametrize(
    "function, rpc_name, extra",
    [
        (lambda ids: database.ack_sent(ids), "mark_notifications_sent", {}),
        (lambda ids: database.release(ids, "timeout"), "release_failed_notifications", {"err_text": "timeout"}),
        (lambda ids: database.mark_dead(ids, "bad_request"), "mark_notifications_dead", {"err_text": "bad_request"}),
    ],
)
def test_ack_family_calls_the_right_function_with_the_right_parameters(db, function, rpc_name, extra):
    client = db(2)
    assert function(["q1", "q2"]) == 2
    assert (client.calls[0].kind, client.calls[0].name) == ("rpc", rpc_name)
    assert client.calls[0].params == {"notification_ids": ["q1", "q2"], **extra}


@pytest.mark.parametrize("function", [database.ack_sent, lambda ids: database.release(ids, "x"), lambda ids: database.mark_dead(ids, "x")])
def test_ack_family_with_no_ids_makes_no_call(db, function):
    client = db()
    assert function([]) == 0 and function(None) == 0
    assert client.calls == []


def test_ack_sent_dedupes_chunks_and_sums(db):
    ids = [f"q{i}" for i in range(450)] + ["q0", "q1"]
    client = db(200, 200, 50)
    assert database.ack_sent(ids) == 450
    assert [len(c.params["notification_ids"]) for c in client.calls] == [200, 200, 50]


def test_ack_accepts_a_scalar_or_a_wrapped_value(db):
    db(1, [{"mark_notifications_sent": 3}], [], None)
    assert database.ack_sent(["a"]) == 1
    assert database.ack_sent(["a"]) == 3
    assert database.ack_sent(["a"]) == 0
    assert database.ack_sent(["a"]) == 0


def test_ack_failure_is_soft_and_later_chunks_still_go(db):
    ids = [f"q{i}" for i in range(250)]
    client = db(Boom("x"), Boom("x"), Boom("x"), 50)
    assert database.ack_sent(ids) == 50
    assert len(client.calls) == 4


def test_release_error_text_is_collapsed_cut_and_never_empty(db):
    client = db(1, 1, 1)
    database.release(["q1"], "line one\n\n  line   two " + "x" * 600)
    sent = client.calls[0].params["err_text"]
    assert len(sent) == database.MAX_ERROR_TEXT and sent.startswith("line one line two x")
    database.release(["q1"], None)
    database.release(["q1"], "   ")
    assert client.calls[1].params["err_text"] == client.calls[2].params["err_text"] == "unknown"


# --------------------------------------------------------------------------- handle_blocked_users

def test_blocked_user_is_paused_then_their_waiting_rows_are_cancelled(db):
    client = db([{"id": "u1"}], [{"id": "q1"}, {"id": "q2"}])
    result = database.handle_blocked_users(555)
    assert result == {"ok": True, "paused": True, "cancelled": 2}

    pause, cancel = client.calls
    assert pause.name == "users"
    assert pause.first("update")[0] == ({"status": "paused"},)
    assert pause.first("eq")[0] == ("telegram_user_id", 555)

    assert cancel.name == "pending_notifications"
    assert cancel.first("update")[0] == ({"status": "cancelled", "error_detail": "blocked bot"},)
    assert cancel.first("eq")[0] == ("user_id", "u1")
    assert cancel.first("in_")[0] == ("status", ["pending", "sending"])


def test_blocked_unknown_user_cancels_nothing(db):
    client = db([])
    assert database.handle_blocked_users(555) == {"ok": True, "paused": False, "cancelled": 0}
    assert len(client.calls) == 1


def test_blocked_user_with_no_waiting_rows(db):
    db([{"id": "u1"}], [])
    assert database.handle_blocked_users(555) == {"ok": True, "paused": True, "cancelled": 0}


def test_blocked_user_failure_is_soft_and_reported(db):
    db(*([Boom("down")] * 3))
    assert database.handle_blocked_users(555) == {"ok": False, "paused": False, "cancelled": 0}


def test_blocked_user_pause_succeeds_but_cancel_fails(db):
    db([{"id": "u1"}], *([Boom("down")] * 3))
    result = database.handle_blocked_users(555)
    assert result["ok"] is False and result["paused"] is True


# --------------------------------------------------------------------------- queue_stats / health

def test_queue_stats(db):
    stats = {"sent_24h": 5, "retried_24h": 1, "dead_24h": 0, "cancelled_24h": 2, "pending_now": 0, "sending_now": 0}
    client = db([stats], [], Boom("x"), Boom("x"))
    assert database.queue_stats() == stats and client.calls[0].name == "queue_stats_24h"
    assert client.calls[0].params == {}
    assert database.queue_stats() is None
    assert database.queue_stats() is None


def test_record_site_result_parameters_and_result(db):
    client = db([{"o_consecutive_failures": 3, "o_failure_alerted": False}])
    assert database.record_site_result("w1", "failed", "Timeout\nafter 3 tries") == {"consecutive_failures": 3, "failure_alerted": False}
    assert client.calls[0].name == "record_site_result"
    assert client.calls[0].params == {"p_web_id": "w1", "p_status": "failed", "p_error": "Timeout after 3 tries"}


@pytest.mark.parametrize("status", ["ok", "degraded", "failed"])
def test_record_site_result_accepts_the_three_states_with_no_error(db, status):
    client = db([{"o_consecutive_failures": 0, "o_failure_alerted": False}])
    database.record_site_result("w1", status)
    assert client.calls[0].params["p_error"] is None


def test_record_site_result_rejects_an_unknown_status_without_calling(db):
    client = db()
    for bad in ("skipped", "OK", "", None):
        with pytest.raises(ValueError):
            database.record_site_result("w1", bad)
    assert client.calls == []


def test_record_site_result_failure_is_soft(db):
    db(Boom("x"), Boom("x"), [])
    assert database.record_site_result("w1", "ok") is None
    assert database.record_site_result("w1", "ok") is None


def test_fetch_site_alerts_asks_for_unalerted_sites_at_the_threshold(db):
    row = {"web_id": "w1", "last_status": "failed", "consecutive_failures": 3, "failure_alerted": False,
           "failing_since": "2026-11-10T01:00:00+00:00", "last_failed_at": None, "last_resolved_at": None,
           "last_error": "Timeout", "websites": {"web_domain_name": "https://asurascans.com/"}}
    client = db([row])
    alerts = database.fetch_site_alerts()
    call = client.calls[0]
    assert call.name == "site_health"
    assert call.first("gte")[0] == ("consecutive_failures", database.ALERT_FAILURE_THRESHOLD) == ("consecutive_failures", 3)
    assert call.first("eq")[0] == ("failure_alerted", False)
    assert alerts == [{
        "web_id": "w1", "web_domain_name": "https://asurascans.com/", "last_status": "failed",
        "consecutive_failures": 3, "failure_alerted": False, "failing_since": "2026-11-10T01:00:00+00:00",
        "last_failed_at": None, "last_resolved_at": None, "last_error": "Timeout",
    }]


def test_fetch_site_alerts_survives_a_missing_join_and_failures(db):
    db([{"web_id": "w1", "consecutive_failures": 4}], Boom("x"), Boom("x"))
    assert database.fetch_site_alerts()[0]["web_domain_name"] is None
    assert database.fetch_site_alerts() == []


def test_ack_site_alerts(db):
    client = db([{"web_id": "w1"}, {"web_id": "w2"}], Boom("x"), Boom("x"), Boom("x"))
    assert database.ack_site_alerts(["w1", "w2", "w1"]) == 2
    call = client.calls[0]
    assert call.name == "site_health" and call.first("update")[0] == ({"failure_alerted": True},)
    assert call.first("in_")[0] == ("web_id", ["w1", "w2"])
    assert database.ack_site_alerts(["w3"]) == 0                      # failure is soft
    assert database.ack_site_alerts([]) == 0 and len(client.calls) == 4


def test_fetch_site_health_rows(db):
    rows = [{"web_id": "w1", "last_status": "ok", "consecutive_failures": 0, "websites": {"web_domain_name": "d"}}]
    client = db(rows, Boom("x"), Boom("x"))
    result = database.fetch_site_health_rows()
    assert result[0]["web_domain_name"] == "d" and result[0]["last_status"] == "ok"
    assert client.calls[0].first("select")[0] == (database.SITE_HEALTH_COLUMNS,)
    assert database.fetch_site_health_rows() == []


# --------------------------------------------------------------------------- app_state

def test_get_app_state_uses_limit_one_not_maybe_single(db):
    client = db([{"value": "2026-11-10T18:05:00+00:00"}])
    assert database.get_app_state("last_digest_sent_at") == "2026-11-10T18:05:00+00:00"
    call = client.calls[0]
    assert call.name == "app_state" and call.first("select")[0] == ("value",)
    assert call.first("eq")[0] == ("key", "last_digest_sent_at") and call.first("limit")[0] == (1,)
    assert not call.op("maybe_single") and not call.op("single")


def test_get_app_state_returns_none_for_a_missing_row_or_a_null_value(db):
    db([], [{"value": None}])
    assert database.get_app_state("k") is None
    assert database.get_app_state("k") is None


def test_get_app_state_raises_when_the_database_cannot_be_read(db):
    client = db(*([Boom("down")] * 3))
    with pytest.raises(Boom):
        database.get_app_state("last_digest_sent_at")
    assert len(client.calls) == 3


def test_set_app_state_upserts_with_a_fresh_updated_at(db):
    client = db([{"key": "k"}])
    before = datetime.now(timezone.utc)
    assert database.set_app_state("k", "v") is True
    args, kwargs = client.calls[0].first("upsert")
    assert kwargs == {"on_conflict": "key"}
    assert args[0]["key"] == "k" and args[0]["value"] == "v"
    assert datetime.fromisoformat(args[0]["updated_at"]) >= before


def test_set_app_state_failure_returns_false(db):
    db(*([Boom("x")] * 3))
    assert database.set_app_state("k", "v") is False


# --------------------------------------------------------------------------- fetch_novels (unchanged, still returns the title)

def test_fetch_novels_returns_display_title_because_it_selects_everything(db):
    rows = [{"id": "n1", "novel_name": "a", "latest_chap": 1, "display_title": "A Title"}]
    client = db(rows)
    assert database.fetch_novels("w1") == rows
    assert client.calls[0].first("select")[0] == ("*",)
