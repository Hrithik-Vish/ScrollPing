"""Manual smoke test: sends REAL messages through your bot to YOUR chat.

It is skipped unless RUN_SMOKE_TESTS=1 is set in the shell, so it never runs
in a normal `pytest` run or in GitHub Actions.

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your .env.

Run it (Docker):
    docker exec -e RUN_SMOKE_TESTS=1 -w /app scrollping-dev python -m pytest tests/test_smokeTest.py -v -s

Run only one of the two tests by adding:  -k hero   or   -k long
"""

import os

import pytest
from dotenv import load_dotenv

from src import telegramMessenger

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SMOKE_TESTS") != "1",
    reason="manual smoke test: set RUN_SMOKE_TESTS=1 to send real Telegram messages",
)


@pytest.fixture
def my_chat_id():
    load_dotenv()
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        pytest.skip("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing from .env")
    return os.getenv("TELEGRAM_CHAT_ID")


def test_smoke_hero_ballad_message(my_chat_id):
    rows = [
        {   # apostrophe in the slug, fractional chapter
            "novel_name": "the-hero's-ballad",
            "latest_chap": 12.5,
            "chapter_url": "https://vortexscans.org/series/the-hero's-ballad/chapter-12.5",
        },
        {   # & in the slug and in the URL query string
            "novel_name": "tom-&-jerry",
            "latest_chap": 3,
            "chapter_url": "https://example.com/series/tom-&-jerry/chapter-3?a=1&b=2",
        },
        {   # whole-number chapter stored as a float
            "novel_name": "lookism",
            "latest_chap": 631.0,
            "chapter_url": "https://hivetoons.org/series/lookism/chapter-631",
        },
    ]

    summary = telegramMessenger.send_all({my_chat_id: rows})

    print("\nsummary:", summary)
    print(
        "CHECK TELEGRAM. Expected one message:\n"
        "  bold header 'New chapters'\n"
        "  3 lines, each title is a tappable link: the-hero's-ballad \u2014 Ch. 12.5,\n"
        "  tom-&-jerry \u2014 Ch. 3, lookism \u2014 Ch. 631 (no '.0')\n"
        "  no link preview cards under the message; tap each link to confirm it opens."
    )
    assert summary == {"sent": 1, "blocked": [], "bad_request": [], "failed": []}


def test_smoke_long_message_is_split_into_numbered_chunks(my_chat_id):
    rows = [
        {
            "novel_name": f"smoke-test-novel-{i}",
            "latest_chap": i,
            "chapter_url": f"https://example.com/series/smoke-test-novel-{i}/chapter-{i}",
        }
        for i in range(1, 121)
    ]
    expected_chunks = len(telegramMessenger.build_message(rows))
    assert expected_chunks > 1

    summary = telegramMessenger.send_all({my_chat_id: rows})

    print("\nsummary:", summary)
    print(
        f"CHECK TELEGRAM. Expected {expected_chunks} messages headed "
        f"'New chapters (1/{expected_chunks})' ... '({expected_chunks}/{expected_chunks})', "
        "arriving about a second apart, covering novels 1 to 120 in order."
    )
    assert summary["sent"] == expected_chunks
    assert summary["blocked"] == [] and summary["bad_request"] == [] and summary["failed"] == []
