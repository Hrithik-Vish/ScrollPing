"""Tests for telegramMessenger.py.

No real network, no real Telegram: a FakeBot replays scripted outcomes, and
asyncio.sleep is replaced so nothing waits. Async code is driven with
asyncio.run, so no extra pytest plugin is needed.
"""

import asyncio
from datetime import timedelta

import pytest
from telegram.error import (
    BadRequest,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

from src import telegramMessenger as tm


# ---------------------------------------------------------------- helpers

@pytest.fixture
def sleeps(monkeypatch):
    """Replace asyncio.sleep with a recorder so tests never wait."""
    recorded = []

    async def fake_sleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(tm.asyncio, "sleep", fake_sleep)
    return recorded


class FakeBot:
    """Stands in for telegram.Bot.

    script maps chat_id -> list of outcomes consumed one per send_message call
    for that chat. An outcome that is an Exception is raised; anything else
    means success. When a chat's list runs out, sends succeed.
    """

    def __init__(self, script=None, get_me_error=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.get_me_error = get_me_error
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get_me(self):
        if self.get_me_error:
            raise self.get_me_error

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        queue = self.script.get(kwargs["chat_id"])
        if queue:
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome

    def calls_for(self, chat_id):
        return [c for c in self.calls if c["chat_id"] == chat_id]


@pytest.fixture
def env(monkeypatch):
    """A token is set and .env loading is neutralised."""
    monkeypatch.setattr(tm, "load_dotenv", lambda: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake-token")


def use_bot(monkeypatch, bot):
    monkeypatch.setattr(tm, "Bot", lambda token: bot)


def make_rows(count, name_len=40):
    return [
        {
            "novel_name": f"Novel {i} " + "x" * name_len,
            "latest_chap": i,
            "chapter_url": f"https://example.com/series/{i}/chapter-{i}",
        }
        for i in range(1, count + 1)
    ]


# ---------------------------------------------------------------- get_token

def test_get_token_missing_raises(monkeypatch):
    monkeypatch.setattr(tm, "load_dotenv", lambda: None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        tm.get_token()


def test_get_token_blank_raises(monkeypatch):
    monkeypatch.setattr(tm, "load_dotenv", lambda: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")
    with pytest.raises(RuntimeError):
        tm.get_token()


def test_get_token_returns_stripped_value(monkeypatch):
    monkeypatch.setattr(tm, "load_dotenv", lambda: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "  abc:def \n")
    assert tm.get_token() == "abc:def"


# ---------------------------------------------------------------- build_message

def test_build_message_empty_returns_empty_list():
    assert tm.build_message([]) == []
    assert tm.build_message(None) == []


def test_build_message_single_row_shape():
    rows = [{"novel_name": "Lookism", "latest_chap": 631,
             "chapter_url": "https://site.com/series/lookism/chapter-631"}]
    chunks = tm.build_message(rows)
    assert len(chunks) == 1
    assert chunks[0].startswith("<b>New chapters</b>")  # no numbering
    assert ('<a href="https://site.com/series/lookism/chapter-631">Lookism</a>'
            in chunks[0])
    assert "Ch. 631" in chunks[0]


def test_build_message_escapes_name_and_url():
    rows = [{"novel_name": "Tom & Jerry <3",
             "latest_chap": 1,
             "chapter_url": 'https://x.com/a?b=1&c="2"'}]
    text = tm.build_message(rows)[0]
    assert "Tom &amp; Jerry &lt;3" in text
    assert 'href="https://x.com/a?b=1&amp;c=&quot;2&quot;"' in text
    assert "<3" not in text


@pytest.mark.parametrize("value, expected", [
    (631.0, "Ch. 631"),
    (631, "Ch. 631"),
    (12.5, "Ch. 12.5"),
    ("40", "Ch. 40"),
    (None, "Ch. ?"),
])
def test_build_message_chapter_formatting(value, expected):
    rows = [{"novel_name": "N", "latest_chap": value, "chapter_url": "https://x.com/n"}]
    assert expected in tm.build_message(rows)[0]


def test_build_message_without_url_has_no_link():
    rows = [{"novel_name": "NoLink", "latest_chap": 3, "chapter_url": None}]
    text = tm.build_message(rows)[0]
    assert "<a " not in text
    assert "NoLink" in text


def test_build_message_truncates_very_long_names():
    rows = [{"novel_name": "a" * 1000, "latest_chap": 1, "chapter_url": "https://x.com"}]
    text = tm.build_message(rows)[0]
    assert "a" * (tm.MAX_NAME_LEN + 1) not in text
    assert "\u2026" in text


def test_build_message_chunks_respect_limit_and_keep_tags_intact():
    rows = make_rows(100)
    chunks = tm.build_message(rows)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= tm.MAX_MESSAGE_LEN
        assert chunk.count("<a ") == chunk.count("</a>")  # no tag cut in half
    joined = "\n".join(chunks)
    for i in range(1, 101):
        assert joined.count(f"Novel {i} ") == 1  # every novel exactly once


def test_build_message_numbers_chunks_only_when_split():
    chunks = tm.build_message(make_rows(100))
    total = len(chunks)
    assert chunks[0].startswith(f"<b>New chapters (1/{total})</b>")
    assert chunks[-1].startswith(f"<b>New chapters ({total}/{total})</b>")


def test_build_message_preserves_row_order():
    chunks = tm.build_message(make_rows(60))
    joined = "\n".join(chunks)
    positions = [joined.index(f"Novel {i} ") for i in range(1, 61)]
    assert positions == sorted(positions)


# ---------------------------------------------------------------- send_one

def test_bad_request_is_a_network_error_subclass():
    """Canary: this is why the except order in send_one matters."""
    assert issubclass(BadRequest, NetworkError)
    assert issubclass(TimedOut, NetworkError)


def test_send_one_success_uses_html_and_disables_previews():
    bot = FakeBot()
    assert asyncio.run(tm.send_one(bot, "111", "hi")) == ("sent", None)
    call = bot.calls[0]
    assert call["chat_id"] == "111"
    assert call["text"] == "hi"
    assert call["parse_mode"] == "HTML"
    assert call["link_preview_options"].is_disabled is True


@pytest.mark.parametrize("error, expected_status", [
    (Forbidden("bot was blocked by the user"), "blocked"),
    (BadRequest("Chat not found"), "bad_request"),   # must NOT become "network"
    (BadRequest("Message is too long"), "bad_request"),
    (TimedOut(), "network"),
    (NetworkError("connection failed"), "network"),
    (TelegramError("something odd"), "unknown"),
])
def test_send_one_classifies_errors(error, expected_status):
    bot = FakeBot({"111": [error]})
    status, detail = asyncio.run(tm.send_one(bot, "111", "hi"))
    assert status == expected_status
    assert isinstance(detail, str)


def test_send_one_reraises_invalid_token():
    bot = FakeBot({"111": [InvalidToken("rejected")]})
    with pytest.raises(InvalidToken):
        asyncio.run(tm.send_one(bot, "111", "hi"))


@pytest.mark.parametrize("raw, seconds", [
    (5, 5),
    (timedelta(seconds=7), 7),
    (timedelta(seconds=2.2), 3),  # rounded up, never waits too little
])
def test_send_one_rate_limited_returns_wait_seconds(raw, seconds):
    bot = FakeBot({"111": [RetryAfter(raw)]})
    assert asyncio.run(tm.send_one(bot, "111", "hi")) == ("rate_limited", seconds)


# ---------------------------------------------------------------- send_with_flood_wait

def test_flood_wait_sleeps_then_succeeds(sleeps):
    bot = FakeBot({"111": [RetryAfter(4)]})
    result = asyncio.run(tm.send_with_flood_wait(bot, "111", "hi"))
    assert result == ("sent", None)
    assert sleeps == [4]
    assert len(bot.calls) == 2


def test_flood_wait_gives_up_after_max_retries(sleeps, monkeypatch):
    monkeypatch.setattr(tm, "MAX_FLOOD_RETRIES", 2)
    bot = FakeBot({"111": [RetryAfter(1)] * 10})
    status, detail = asyncio.run(tm.send_with_flood_wait(bot, "111", "hi"))
    assert (status, detail) == ("rate_limited", 1)
    assert len(bot.calls) == 3  # 1 attempt + 2 retries
    assert sleeps == [1, 1]


def test_flood_wait_does_not_sleep_past_the_cap(sleeps, monkeypatch):
    monkeypatch.setattr(tm, "MAX_FLOOD_WAIT", 60)
    bot = FakeBot({"111": [RetryAfter(3000)]})
    status, detail = asyncio.run(tm.send_with_flood_wait(bot, "111", "hi"))
    assert (status, detail) == ("rate_limited", 3000)
    assert sleeps == []
    assert len(bot.calls) == 1


def test_flood_wait_passes_other_statuses_straight_through(sleeps):
    bot = FakeBot({"111": [Forbidden("blocked")]})
    assert asyncio.run(tm.send_with_flood_wait(bot, "111", "hi"))[0] == "blocked"
    assert sleeps == []


# ---------------------------------------------------------------- record_outcome

def _record(status, detail="d", attempts=1):
    summary, queue = tm.new_summary(), tm.defaultdict(list)
    tm.record_outcome(summary, queue, "111", "text", status, detail, attempts)
    return summary, queue


def test_record_sent():
    summary, queue = _record("sent", None)
    assert summary["sent"] == 1 and not queue


def test_record_blocked_is_deduplicated():
    summary, queue = _record("blocked")
    tm.record_outcome(summary, queue, "111", "t2", "blocked", "d")
    assert summary["blocked"] == ["111"] and not queue


def test_record_bad_request_is_final():
    summary, queue = _record("bad_request", "Chat not found")
    assert summary["bad_request"] == [("111", "Chat not found")]
    assert not queue


@pytest.mark.parametrize("status", ["network", "rate_limited"])
def test_record_retryable_goes_to_queue(status):
    summary, queue = _record(status, "oops", attempts=2)
    assert queue[status] == [
        {"chat_id": "111", "text": "text", "attempts": 2, "detail": "oops"}
    ]
    assert summary["sent"] == 0 and summary["failed"] == []


def test_record_unknown_goes_to_failed():
    summary, queue = _record("unknown", "weird")
    assert summary["failed"] == [("111", "unknown", "weird")]
    assert not queue


# ---------------------------------------------------------------- retry_failed

def _queue_with(*items):
    queue = tm.defaultdict(list)
    for chat_id, category in items:
        queue[category].append(
            {"chat_id": chat_id, "text": "t", "attempts": 1, "detail": "first"}
        )
    return queue


def test_retry_failed_recovers_on_second_attempt(sleeps):
    bot = FakeBot()
    summary, queue = tm.new_summary(), _queue_with(("111", "network"))
    asyncio.run(tm.retry_failed(bot, queue, summary))
    assert summary["sent"] == 1 and summary["failed"] == []
    assert not queue
    assert sleeps == [tm.ROUND_DELAY]


def test_retry_failed_exhausts_rounds_then_reports(sleeps, monkeypatch):
    monkeypatch.setattr(tm, "MAX_RETRY_ROUNDS", 2)
    bot = FakeBot({"111": [NetworkError("down")] * 10})
    summary, queue = tm.new_summary(), _queue_with(("111", "network"))
    asyncio.run(tm.retry_failed(bot, queue, summary))
    assert len(bot.calls) == 2  # one send per round
    assert summary["sent"] == 0
    assert len(summary["failed"]) == 1
    chat_id, category, detail = summary["failed"][0]
    assert (chat_id, category) == ("111", "network")
    assert "down" in detail
    assert not queue


def test_retry_failed_final_errors_are_not_requeued(sleeps):
    bot = FakeBot({"111": [Forbidden("blocked")], "222": [BadRequest("bad html")]})
    summary = tm.new_summary()
    queue = _queue_with(("111", "network"), ("222", "network"))
    asyncio.run(tm.retry_failed(bot, queue, summary))
    assert summary["blocked"] == ["111"]
    assert summary["bad_request"] == [("222", "bad html")]
    assert len(bot.calls) == 2  # not tried again in a later round
    assert not queue


def test_retry_failed_empty_queue_does_nothing(sleeps):
    bot = FakeBot()
    summary = tm.new_summary()
    asyncio.run(tm.retry_failed(bot, tm.defaultdict(list), summary))
    assert bot.calls == [] and sleeps == []
    assert summary == tm.new_summary()


# ---------------------------------------------------------------- send_all

def test_send_all_missing_token_fails_before_creating_a_bot(monkeypatch):
    monkeypatch.setattr(tm, "load_dotenv", lambda: None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    def boom(token):
        raise AssertionError("Bot must not be created without a token")

    monkeypatch.setattr(tm, "Bot", boom)
    with pytest.raises(RuntimeError):
        tm.send_all({"111": make_rows(1)})


def test_send_all_empty_input_returns_empty_summary(env, monkeypatch, sleeps):
    bot = FakeBot()
    use_bot(monkeypatch, bot)
    assert tm.send_all({}) == tm.new_summary()
    assert bot.calls == []


def test_send_all_happy_path(env, monkeypatch, sleeps):
    bot = FakeBot()
    use_bot(monkeypatch, bot)
    summary = tm.send_all({"111": make_rows(2), "222": make_rows(3)})
    assert summary == {"sent": 2, "blocked": [], "bad_request": [], "failed": []}
    assert len(bot.calls_for("111")) == 1
    assert len(bot.calls_for("222")) == 1


def test_send_all_one_users_failure_does_not_stop_the_others(env, monkeypatch, sleeps):
    bot = FakeBot({"111": [BadRequest("Chat not found")]})
    use_bot(monkeypatch, bot)
    summary = tm.send_all({"111": make_rows(1), "222": make_rows(1)})
    assert summary["sent"] == 1
    assert summary["bad_request"] == [("111", "Chat not found")]


def test_send_all_blocked_user_skips_remaining_chunks(env, monkeypatch, sleeps):
    bot = FakeBot({"111": [Forbidden("blocked")]})
    use_bot(monkeypatch, bot)
    rows = make_rows(100)
    assert len(tm.build_message(rows)) > 1  # precondition: multi-chunk user
    summary = tm.send_all({"111": rows, "222": make_rows(1)})
    assert len(bot.calls_for("111")) == 1
    assert summary["blocked"] == ["111"]
    assert summary["sent"] == 1  # user 222 still delivered


def test_send_all_pauses_between_chunks_of_the_same_user(env, monkeypatch, sleeps):
    monkeypatch.setattr(tm, "CHUNK_PAUSE", 1.5)
    monkeypatch.setattr(tm, "BATCH_SIZE", 10_000)
    bot = FakeBot()
    use_bot(monkeypatch, bot)
    chunks = len(tm.build_message(make_rows(100)))
    tm.send_all({"111": make_rows(100)})
    assert sleeps.count(1.5) == chunks - 1


def test_send_all_pauses_after_every_batch(env, monkeypatch, sleeps):
    monkeypatch.setattr(tm, "BATCH_SIZE", 2)
    monkeypatch.setattr(tm, "BATCH_PAUSE", 7)
    bot = FakeBot()
    use_bot(monkeypatch, bot)
    users = {str(i): make_rows(1) for i in range(1, 6)}  # 5 sends
    tm.send_all(users)
    assert sleeps.count(7) == 2  # before the 3rd and the 5th send


def test_send_all_retries_a_network_failure_and_recovers(env, monkeypatch, sleeps):
    bot = FakeBot({"111": [NetworkError("blip")]})
    use_bot(monkeypatch, bot)
    summary = tm.send_all({"111": make_rows(1)})
    assert summary["sent"] == 1 and summary["failed"] == []
    assert len(bot.calls_for("111")) == 2
    assert tm.ROUND_DELAY in sleeps


def test_send_all_reports_when_retries_run_out(env, monkeypatch, sleeps):
    monkeypatch.setattr(tm, "MAX_RETRY_ROUNDS", 2)
    bot = FakeBot({"111": [TimedOut()] * 10})
    use_bot(monkeypatch, bot)
    summary = tm.send_all({"111": make_rows(1), "222": make_rows(1)})
    assert summary["sent"] == 1
    assert len(bot.calls_for("111")) == 3  # first try + 2 rounds
    assert [(c, cat) for c, cat, _ in summary["failed"]] == [("111", "network")]


def test_send_all_bad_token_fails_in_preflight_before_any_send(env, monkeypatch, sleeps):
    bot = FakeBot(get_me_error=InvalidToken("rejected"))
    use_bot(monkeypatch, bot)
    with pytest.raises(InvalidToken):
        tm.send_all({"111": make_rows(1)})
    assert bot.calls == []


def test_send_all_token_revoked_mid_run_propagates(env, monkeypatch, sleeps):
    bot = FakeBot({"222": [InvalidToken("revoked")]})
    use_bot(monkeypatch, bot)
    with pytest.raises(InvalidToken):
        tm.send_all({"111": make_rows(1), "222": make_rows(1)})


def test_send_all_ignores_non_token_preflight_errors(env, monkeypatch, sleeps):
    bot = FakeBot(get_me_error=NetworkError("blip"))
    use_bot(monkeypatch, bot)
    summary = tm.send_all({"111": make_rows(1)})
    assert summary["sent"] == 1
