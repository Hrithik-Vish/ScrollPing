"""The whole scraper pipeline over the real trimmed fixtures, with an in-memory stand-in for the
database queue. Only the network (fetch_html), the database and Telegram are faked; parsing, compile,
scheduling, the run order and dispatch are the real code.

Story: asura publishes a locked (early access) chapter. A member is alerted at once, a standard user
waits until it unlocks, nobody is alerted twice, and a second run changes nothing.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import main
from src import database, parsing
from src import telegramMessenger as tm
from telegram.error import Forbidden, NetworkError

REAL_SEND_ALL = tm.send_all          # captured before the fixture replaces it with a fake

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RULES = json.loads((ROOT / "site_parse_rules.json").read_text(encoding="utf-8"))

ASURA = "https://asurascans.com/"
HIVE = "https://hivetoons.org/"
START = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)          # before every unlock time in the fixture (22:47 to 02:41)

FIXTURE_OF = {ASURA: "asura_latest.html", HIVE: "hive_latest.html"}


class World:
    """A tiny stand-in for Supabase: websites, novels, subscribers, tiers and the queue."""

    def __init__(self):
        self.now = START
        self.websites = [{"id": "w-asura", "web_domain_name": ASURA}, {"id": "w-hive", "web_domain_name": HIVE}]
        asura = parsing.parse_page((FIXTURES / "asura_latest.html").read_text(encoding="utf-8"), RULES[ASURA])
        self.novels = {}
        for slug in {i["slug"] for i in asura["items"]}:
            self.novels[slug] = {"id": f"n-{slug}", "novel_name": slug, "latest_chap": 0, "chapter_url": "old",
                                 "display_title": None, "web_id": "w-asura"}
        self.novels["lookism"] = {"id": "n-lookism", "novel_name": "lookism", "latest_chap": 631, "chapter_url": "old",
                                  "display_title": None, "web_id": "w-hive"}
        # two readers of everything: a member of asura and a standard user
        self.users = {"member": 1001, "standard": 1002}
        self.tiers = {("member", "w-asura"): "member"}
        self.queue = []              # dicts: user_id, novel_id, chapter, status, due_at, access_state, unlock_at, id
        self.sent = []               # (telegram_id, novel_name, chapter, access_state) in delivery order
        self.send_calls = 0

    # ---- database stand-ins
    def fetch_websites(self):
        return self.websites

    def fetch_novels(self, site_id):
        return [dict(n) for n in self.novels.values() if n["web_id"] == site_id]

    def update_novels_table(self, rows):
        for r in rows:
            n = self.novels[r["novel_name"]]
            n["latest_chap"], n["chapter_url"] = r["latest_chap"], r["chapter_url"]
            n["display_title"] = r.get("display_title") or n["display_title"]
        return []

    def sync_novel_titles(self, updates):
        return [u["id"] for u in updates]

    def fetch_subscribers_with_preferences(self, novel_ids, web_by_novel=None):
        recipients = [{"novel_id": n, "user_id": u, "alert_timing": None, "telegram_user_id": tg}
                      for n in novel_ids for u, tg in self.users.items()]
        return database.SubscriberFetch(recipients, dict(self.tiers), set())

    def insert_queue_records(self, records):
        inserted = 0
        for r in records:
            key = (r["user_id"], r["novel_id"], r["chapter"])
            if any((q["user_id"], q["novel_id"], q["chapter"]) == key for q in self.queue):
                continue
            self.queue.append({**r, "id": f"q{len(self.queue) + 1}"})
            inserted += 1
        return {"attempted": len(records), "inserted": inserted, "failed": 0, "failed_novel_ids": set()}

    def claim_due(self, limit=500):
        by_id = {n["id"]: n for n in self.novels.values()}
        domain_of = {w["id"]: w["web_domain_name"] for w in self.websites}
        claimed = []
        for q in self.queue:
            if q["status"] == "pending" and datetime.fromisoformat(q["due_at"]) <= self.now:
                q["status"] = "sending"
                claimed.append({"notification_id": q["id"], "telegram_user_id": self.users[q["user_id"]],
                                "novel_name": by_id[q["novel_id"]]["novel_name"], "display_title": by_id[q["novel_id"]]["display_title"],
                                "web_domain_name": domain_of[by_id[q["novel_id"]]["web_id"]],
                                "chapter": q["chapter"], "chapter_url": q["chapter_url"],
                                "access_state": q["access_state"], "unlock_at": q["unlock_at"]})
        return claimed

    # ---- delivery stand-ins
    def send_all(self, batches, delivery, rules=None, now=None):
        self.send_calls += 1
        sent = 0
        for telegram_id, rows in batches.items():
            for row in rows:
                self.sent.append((telegram_id, row["novel_name"], row["chapter"], row["access_state"]))
                sent += 1
            delivery.ack_sent([r["notification_id"] for r in rows])
        return {"sent": sent, "blocked": 0, "bad_request": 0, "failed": 0, "retried": 0}

    def ack_sent(self, ids):
        n = 0
        for q in self.queue:
            if q["id"] in ids and q["status"] == "sending":
                q["status"] = "sent"
                n += 1
        return n


@pytest.fixture
def world(monkeypatch):
    monkeypatch.chdir(ROOT)
    w = World()
    monkeypatch.setattr(main, "utcnow", lambda: w.now)
    monkeypatch.setattr(main.fetching, "fetch_html", lambda url: (FIXTURES / FIXTURE_OF[url]).read_text(encoding="utf-8"))
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    patches = {
        "insert_scraper_logs": lambda: (True, 1), "update_scraper_logs": lambda row_id: None,
        "maintain_queue": lambda: {"ok": True, "recovered": 0, "cancelled": 0, "expired": 0},
        "record_site_result": lambda *a, **k: {"consecutive_failures": 0, "failure_alerted": False},
        "fetch_websites": w.fetch_websites, "fetch_novels": w.fetch_novels, "update_novels_table": w.update_novels_table,
        "sync_novel_titles": w.sync_novel_titles, "fetch_subscribers_with_preferences": w.fetch_subscribers_with_preferences,
        "insert_queue_records": w.insert_queue_records, "claim_due": w.claim_due, "ack_sent": w.ack_sent,
    }
    for name, fn in patches.items():
        monkeypatch.setattr(main.database, name, fn)
    monkeypatch.setattr(main.telegramMessenger, "send_all", w.send_all)
    return w


def delivered(world, user):
    return [(n, c, a) for tg, n, c, a in world.sent if tg == world.users[user]]


def test_a_locked_asura_chapter_reaches_the_member_now_and_the_standard_user_when_it_unlocks(world):
    main.run()

    member, standard = delivered(world, "member"), delivered(world, "standard")
    # run 1 (20:00, before any chapter unlocks): the member gets everything, the standard user only what is free
    assert ("emperor-of-solo-play", 92.0, "locked") in member
    assert ("emperor-of-solo-play", 92.0, "locked") not in standard
    assert ("emperor-of-solo-play", 92.0, "free") not in standard
    assert ("lookism", 632.0, "free") in standard and ("lookism", 632.0, "free") in member
    assert ("cyberpunk-chivalry", 6.0, "free") in standard
    assert len(member) == len(standard) + 4                     # the four locked chapters

    # run 2, eight hours later (all four have unlocked): the standard user receives them now, once
    world.now = START + timedelta(hours=8)
    before = len(world.sent)
    main.run()
    late = world.sent[before:]
    assert sorted((n, c) for tg, n, c, a in late if tg == world.users["standard"]) == sorted(
        [("emperor-of-solo-play", 92.0), ("i-am-the-fated-villain", 369.0), ("reincarnator", 159.0), ("limitless-predation", 30.0)])
    assert not [x for x in late if x[0] == world.users["member"]]    # the member already had them


def test_nothing_is_delivered_twice_and_a_repeat_run_is_quiet(world):
    main.run()
    first = list(world.sent)
    main.run()                                                    # same instant: nothing new, nothing due
    assert world.sent == first
    assert len(world.sent) == len({(tg, n, c) for tg, n, c, a in world.sent})


def test_the_novels_table_advances_only_after_the_queue_has_the_rows(world):
    seen = {}
    real_update = world.update_novels_table

    def checked_update(rows):
        seen["queued_before_update"] = len(world.queue)
        return real_update(rows)

    main.database.update_novels_table = checked_update
    main.run()
    assert seen["queued_before_update"] > 0
    assert world.novels["lookism"]["latest_chap"] == 632


def test_a_queued_row_survives_when_the_novels_update_never_happens(world):
    def crash(rows):
        raise RuntimeError("process died after the queue insert")

    main.database.update_novels_table = crash
    with pytest.raises(RuntimeError):
        main.run()
    assert world.queue and world.novels["lookism"]["latest_chap"] == 631        # not advanced

    main.database.update_novels_table = world.update_novels_table                # next run
    main.run()
    keys = [(q["user_id"], q["novel_id"], q["chapter"]) for q in world.queue]
    assert len(keys) == len(set(keys))                                           # re-detection did not duplicate
    assert world.novels["lookism"]["latest_chap"] == 632
    assert ("lookism", 632.0, "free") in delivered(world, "standard")


class FakeTelegram:
    """A bot that records messages; script maps chat id -> outcomes (exceptions are raised)."""

    def __init__(self, script=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.messages = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_me(self):
        return None

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        outcomes = self.script.get(kwargs["chat_id"])
        if outcomes:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome


@pytest.fixture
def real_messenger(world, monkeypatch):
    """The real messenger and the real QueueDelivery, a fake Telegram, and recorders for the database calls."""
    telegram = FakeTelegram()
    calls = {"released": [], "dead": [], "blocked": []}

    async def instant_sleep(seconds):
        return None

    monkeypatch.setattr(tm, "load_dotenv", lambda: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake")
    monkeypatch.setattr(tm, "Bot", lambda token: telegram)
    monkeypatch.setattr(tm.asyncio, "sleep", instant_sleep)
    monkeypatch.setattr(main.telegramMessenger, "send_all", REAL_SEND_ALL)
    monkeypatch.setattr(main.database, "release", lambda ids, text: calls["released"].append((list(ids), text)) or len(ids))
    monkeypatch.setattr(main.database, "mark_dead", lambda ids, text: calls["dead"].append((list(ids), text)) or len(ids))
    monkeypatch.setattr(main.database, "handle_blocked_users", lambda chat: calls["blocked"].append(chat) or {"ok": True})
    return telegram, calls


def test_the_real_messenger_delivers_acknowledges_and_shows_the_right_text(world, real_messenger):
    telegram, calls = real_messenger
    main.run()

    by_chat = {}
    for message in telegram.messages:
        by_chat.setdefault(message["chat_id"], []).append(message["text"])
    member, standard = "\n".join(by_chat[1001]), "\n".join(by_chat[1002])
    assert "<b>Asura Scans</b>" in member and "<b>Hive Toons</b>" in member            # names come from the rules file
    assert "\U0001F512 free in ~" in member                                             # early access, with time left
    assert "Emperor Of Solo Play" in member or "Emperor of Solo Play" in member
    assert "\U0001F512" not in standard and "Lookism" in standard                        # the standard user waits for unlocks
    assert calls == {"released": [], "dead": [], "blocked": []}
    assert all(q["status"] == "sent" for q in world.queue if q["user_id"] == "member")      # acknowledged in the queue
    assert all(q["status"] in ("sent", "pending") for q in world.queue)


def test_a_failing_and_a_blocking_user_are_handled_through_the_real_delivery(world, real_messenger):
    telegram, calls = real_messenger
    telegram.script = {1001: [NetworkError("down")] * 10, 1002: [Forbidden("blocked by user")]}

    main.run()

    assert [chat for chat in calls["blocked"]] == [1002]                       # standard user blocked the bot
    assert len(calls["released"]) == 1 and calls["released"][0][1] == "network"   # member's rows given back for the next run
    assert calls["dead"] == []
    assert not [q for q in world.queue if q["status"] == "sent"]               # nothing was acknowledged as sent
