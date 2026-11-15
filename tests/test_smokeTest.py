"""Manual smoke test: sends REAL messages through your bot to YOUR chat.

It is skipped unless RUN_SMOKE_TESTS=1 is set in the shell, so it never runs
in a normal `pytest` run or in GitHub Actions.

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your .env. No database is touched: the delivery
object is a recorder that remembers what the messenger acknowledged.

Run it (Docker):
    docker exec -e RUN_SMOKE_TESTS=1 -w /app scrollping-dev python -m pytest tests/test_smokeTest.py -v -s

Run only one of the tests by adding:  -k layout   or   -k long
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from dotenv import load_dotenv

from src import telegramMessenger

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SMOKE_TESTS") != "1",
    reason="manual smoke test: set RUN_SMOKE_TESTS=1 to send real Telegram messages",
)


class RecordingDelivery:
    def __init__(self):
        self.acked, self.released, self.dead, self.blocked = [], [], [], []

    def ack_sent(self, ids):
        self.acked.extend(ids)

    def release(self, ids, error_text):
        self.released.append((list(ids), error_text))

    def mark_dead(self, ids, error_text):
        self.dead.append((list(ids), error_text))

    def blocked(self, chat_id, ids):
        self.blocked.append((chat_id, list(ids)))


@pytest.fixture
def my_chat_id():
    load_dotenv()
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        pytest.skip("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing from .env")
    return os.getenv("TELEGRAM_CHAT_ID")


@pytest.fixture
def rules():
    return json.loads((ROOT / "site_parse_rules.json").read_text(encoding="utf-8"))


def claimed(i, domain, name, title, chapter, url, access_state="free", unlock_at=None):
    return {"notification_id": f"smoke-{i}", "novel_name": name, "display_title": title, "web_domain_name": domain,
            "chapter": chapter, "chapter_url": url, "access_state": access_state, "unlock_at": unlock_at}


def test_smoke_layout_titles_badges_and_escaping(my_chat_id, rules):
    now = datetime.now(timezone.utc)
    rows = [
        claimed(1, "https://asurascans.com/", "killer-pietro", "Killer Pietro", 156.0,
                "https://asurascans.com/comics/killer-pietro-f212c582/chapter/156",
                "locked", (now + timedelta(hours=5)).isoformat()),
        claimed(2, "https://asurascans.com/", "cyberpunk-chivalry", "Cyberpunk Chivalry", 7,
                "https://asurascans.com/comics/cyberpunk-chivalry-a1b2c3d4/chapter/7"),
        claimed(3, "https://vortexscans.org/", "the-hero's-ballad", None, 12.5,
                "https://vortexscans.org/series/the-hero's-ballad/chapter-12.5", "locked", None),
        claimed(4, "https://hivetoons.org/", "tom-&-jerry", "Tom & <Jerry> \"Special\"", 3,
                "https://example.com/series/tom-&-jerry/chapter-3?a=1&b=2"),
        claimed(5, "https://hivetoons.org/", "lookism", "Lookism", 631.0,
                "https://hivetoons.org/series/lookism/chapter-631"),
    ]
    delivery = RecordingDelivery()

    summary = telegramMessenger.send_all({my_chat_id: rows}, delivery, rules=rules)

    print("\nsummary:", summary)
    print(
        "CHECK TELEGRAM. Expected ONE message:\n"
        "  bold header, then three bold site headings (Asura Scans, Hive Toons, Vortex Scans), alphabetical,\n"
        "  each novel a tappable link with a bullet. Asura: Cyberpunk Chivalry Ch. 7, Killer Pietro Ch. 156\n"
        "  with '\U0001F512 free in ~5h'. Hive: Lookism Ch. 631 (no '.0'), 'Tom & <Jerry> \"Special\"' Ch. 3\n"
        "  shown literally. Vortex: 'The Hero's Ballad' (title guessed from the slug) Ch. 12.5 with\n"
        "  '\U0001F512 early access'. No link-preview cards under the message."
    )
    assert summary == {"sent": 5, "blocked": 0, "bad_request": 0, "failed": 0, "retried": 0}
    assert sorted(delivery.acked) == [f"smoke-{i}" for i in range(1, 6)]
    assert delivery.released == [] and delivery.dead == [] and delivery.blocked == []


def test_smoke_long_message_is_split_into_numbered_silent_chunks(my_chat_id, rules):
    rows = [
        claimed(i, "https://hivetoons.org/", f"smoke-test-novel-{i}", f"Smoke Test Novel {i:03d}", i,
                f"https://example.com/series/smoke-test-novel-{i}/chapter-{i}")
        for i in range(1, 121)
    ]
    expected_chunks = len(telegramMessenger.build_message(rows, rules, datetime.now(timezone.utc)))
    assert expected_chunks > 1
    delivery = RecordingDelivery()

    summary = telegramMessenger.send_all({my_chat_id: rows}, delivery, rules=rules)

    print("\nsummary:", summary)
    print(
        f"CHECK TELEGRAM. Expected {expected_chunks} messages headed "
        f"'New chapters (1/{expected_chunks})' ... '({expected_chunks}/{expected_chunks})', about a second apart,\n"
        "each with the 'Hive Toons' heading repeated, covering novels 001 to 120 in order. Only the first one\n"
        "should make a notification sound; the rest arrive silently."
    )
    assert summary["sent"] == 120 and summary["failed"] == 0 and summary["bad_request"] == 0 and summary["blocked"] == 0
    assert len(delivery.acked) == 120


def test_smoke_admin_alert_and_digest_look_right(my_chat_id, rules):
    from src import admin

    now = datetime.now(timezone.utc)
    alert = admin.build_alert({
        "web_domain_name": "https://asurascans.com/", "consecutive_failures": 4,
        "failing_since": (now - timedelta(hours=4)).isoformat(), "last_error": "fetch failed <403 & blocked>",
    }, rules)
    rows = [
        {"web_domain_name": "https://asurascans.com/", "last_status": "failed", "consecutive_failures": 4,
         "failing_since": (now - timedelta(hours=4)).isoformat(), "last_error": "fetch failed <403 & blocked>"},
        {"web_domain_name": "https://hivetoons.org/", "last_status": "ok", "consecutive_failures": 0,
         "last_failed_at": (now - timedelta(hours=9)).isoformat(), "last_resolved_at": (now - timedelta(hours=8)).isoformat()},
        {"web_domain_name": "https://vortexscans.org/", "last_status": "degraded", "last_error": "no titles found"},
        {"web_domain_name": "https://www.kunmanga.online/", "last_status": "ok", "consecutive_failures": 0},
    ]
    digest = admin.build_digest(rows, {"sent_24h": 41, "retried_24h": 2, "dead_24h": 0, "cancelled_24h": 1,
                                       "pending_now": 3, "sending_now": 0}, now, rules)

    first = telegramMessenger.send_admin([int(my_chat_id)], alert)
    second = telegramMessenger.send_admin([int(my_chat_id)], digest)

    print("\nresults:", first, second)
    print(
        "CHECK TELEGRAM. Two messages. (1) a siren alert: 'Asura Scans has failed 4 runs in a row', the start time in IST,\n"
        "and the error in a monospaced box showing the characters '<403 & blocked>' literally.\n"
        "(2) a digest: header with the IST time, then Asura Scans failing, Hive Toons 'Issue resolved', Vortex Scans\n"
        "degraded (KunManga is not listed because only problems are listed), and the two queue lines at the bottom."
    )
    assert first == {"delivered": 1, "failed": 0} and second == {"delivered": 1, "failed": 0}
