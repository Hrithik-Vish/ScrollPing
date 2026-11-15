"""Manual smoke test: runs the Phase 3 database functions against your REAL Supabase project.

Skipped unless RUN_SMOKE_TESTS=1 is set, so it never runs in a normal `pytest` run or in GitHub Actions.

Needs SUPABASE_URL and SUPABASE_SECRET_KEY in your .env (real values). For the queue-insert and
novel-write tests it also needs SMOKE_USER_ID and SMOKE_NOVEL_ID (any real user and novel ids; the same
ids you used in the SQL tests are fine). Without them those two tests are skipped.

What it touches, and how it cleans up:
  - read-only: maintain_queue (harmless housekeeping), queue_stats, site_health reads, subscribers read
  - app_state: writes a scratch key 'smoke_test' and deletes it
  - queue insert: one row for chapter 99999.99 due in the year 2099, inserted twice (the second must be
    ignored), then deleted. A real scraper run cannot pick it up (it is not due).
  - novel write: re-sends the novel's CURRENT values through update_novels_table, then puts updated_at back.

Run it (Docker):
    docker exec -e RUN_SMOKE_TESTS=1 -w /app scrollping-dev python -m pytest tests/test_live_database_smoke.py -v -s
"""

import os

import pytest
from dotenv import load_dotenv
from supabase import create_client

from src import database

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SMOKE_TESTS") != "1",
    reason="manual smoke test: set RUN_SMOKE_TESTS=1 to talk to the real database",
)

SCRATCH_CHAPTER = 99999.99
FAR_FUTURE = "2099-01-01T00:00:00+00:00"


@pytest.fixture
def live(monkeypatch):
    # conftest.py puts dummy Supabase values in the environment; the real .env must win here.
    load_dotenv(override=True)
    url, key = os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_SECRET_KEY", "")
    if not url or not key or "dummy" in url or "dummy" in key:
        pytest.skip("real SUPABASE_URL / SUPABASE_SECRET_KEY missing from .env")
    client = create_client(url, key)
    monkeypatch.setattr(database, "supabase", client)
    return client


@pytest.fixture
def ids():
    load_dotenv(override=True)
    user_id, novel_id = os.getenv("SMOKE_USER_ID"), os.getenv("SMOKE_NOVEL_ID")
    if not user_id or not novel_id:
        pytest.skip("SMOKE_USER_ID / SMOKE_NOVEL_ID missing from .env")
    return user_id, novel_id


def test_live_maintain_and_stats(live):
    maintained = database.maintain_queue()
    print("\nmaintain_queue:", maintained)
    assert maintained["ok"] is True
    assert all(isinstance(maintained[k], int) for k in ("recovered", "cancelled", "expired"))

    stats = database.queue_stats()
    print("queue_stats:", stats)
    assert stats is not None
    assert set(stats) == {"sent_24h", "retried_24h", "dead_24h", "cancelled_24h", "pending_now", "sending_now"}


def test_live_site_health_reads_work_with_the_website_join(live):
    # The database functions are fail-soft (they return [] on any error), so also run the raw query:
    # that one raises if the table, a column or the embedded websites(...) join is wrong.
    raw = live.table("site_health").select(database.SITE_HEALTH_COLUMNS).limit(5).execute()
    print("\nsite_health rows:", len(raw.data))
    rows = database.fetch_site_health_rows()
    assert len(rows) == len(live.table("site_health").select("web_id").execute().data)
    assert all("web_domain_name" in r for r in rows)
    assert isinstance(database.fetch_site_alerts(), list)


def test_live_subscribers_read(live):
    novels = live.table("novels").select("id, web_id").limit(5).execute().data
    assert novels, "no novels in the database to test with"
    result = database.fetch_subscribers_with_preferences([n["id"] for n in novels], {n["id"]: n["web_id"] for n in novels})
    print("\nrecipients:", len(result.recipients), "tiers:", len(result.tiers), "failed:", result.failed_novel_ids)
    assert result.ok
    for r in result.recipients:
        assert set(r) == {"novel_id", "user_id", "alert_timing", "telegram_user_id"}
        assert isinstance(r["telegram_user_id"], int)


def test_live_app_state_round_trip(live):
    try:
        assert database.set_app_state("smoke_test", "hello") is True
        assert database.get_app_state("smoke_test") == "hello"
        assert database.set_app_state("smoke_test", "again") is True
        assert database.get_app_state("smoke_test") == "again"
        assert database.get_app_state("smoke_test_missing") is None
    finally:
        live.table("app_state").delete().eq("key", "smoke_test").execute()


def test_live_queue_insert_ignores_a_duplicate_then_cleans_up(live, ids):
    user_id, novel_id = ids
    record = {"user_id": user_id, "novel_id": novel_id, "chapter": SCRATCH_CHAPTER,
              "chapter_url": "https://smoke.example/chapter/99999", "due_at": FAR_FUTURE}
    try:
        first = database.insert_queue_records([record])
        second = database.insert_queue_records([record])
        print("\nfirst insert:", first, "| second insert:", second)
        assert first["failed"] == 0 and first["inserted"] == 1
        assert second["failed"] == 0 and second["inserted"] == 0          # ON CONFLICT DO NOTHING
        row = live.table("pending_notifications").select("status, access_state, due_at").eq("user_id", user_id).eq("novel_id", novel_id).eq("chapter", SCRATCH_CHAPTER).execute().data
        assert len(row) == 1 and row[0]["status"] == "pending" and row[0]["access_state"] == "free"
    finally:
        live.table("pending_notifications").delete().eq("user_id", user_id).eq("novel_id", novel_id).eq("chapter", SCRATCH_CHAPTER).execute()


def test_live_novel_write_with_its_own_current_values(live, ids):
    _, novel_id = ids
    original = live.table("novels").select("id, latest_chap, chapter_url, display_title, updated_at").eq("id", novel_id).execute().data[0]
    try:
        failed = database.update_novels_table([{
            "id": novel_id, "novel_name": "smoke", "latest_chap": original["latest_chap"],
            "chapter_url": original["chapter_url"], "display_title": None,
        }])
        print("\nupdate_novels_table failed ids:", failed)
        assert failed == []
        after = live.table("novels").select("latest_chap, chapter_url, display_title").eq("id", novel_id).execute().data[0]
        assert after == {k: original[k] for k in ("latest_chap", "chapter_url", "display_title")}   # nothing but updated_at moved

        if original["display_title"]:
            assert database.sync_novel_titles([{"id": novel_id, "display_title": original["display_title"]}]) == []   # same title: no-op
    finally:
        live.table("novels").update({"updated_at": original["updated_at"]}).eq("id", novel_id).execute()
