"""Tests for telegramMessenger.py (Phase 5).

No real network, no real Telegram: a FakeBot replays scripted outcomes, a FakeDelivery records every
acknowledgement, and asyncio.sleep is replaced so nothing waits. Async code is driven with
asyncio.run, so no extra pytest plugin is needed.
"""

import asyncio
import inspect
from datetime import datetime, timedelta, timezone

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

NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
ASURA = "https://asurascans.com/"
HIVE = "https://hivetoons.org/"
RULES = {ASURA: {"display_name": "Asura Scans"}, HIVE: {"display_name": "Hive Toons"}}


# ---------------------------------------------------------------- helpers

@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Replace asyncio.sleep with a recorder (so nothing waits) and freeze the spacing clock."""
    recorded = []

    async def fake_sleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(tm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(tm, "_now", lambda: 0.0)       # every send is "0 s after the previous one"
    return recorded


def pauses(sleeps):
    """The sleeps that are not the 0.05 s send spacing."""
    return [s for s in sleeps if s != tm.SEND_SPACING]


class Events:
    """One ordered log shared by the fake bot and the fake delivery."""

    def __init__(self):
        self.log = []

    def of(self, kind):
        return [e for e in self.log if e[0] == kind]


class FakeBot:
    """Stands in for telegram.Bot.

    script maps chat_id -> list of outcomes consumed one per send_message call for that chat. An
    outcome that is an Exception is raised; anything else means success. When a chat's list runs
    out, sends succeed.
    """

    def __init__(self, script=None, get_me_error=None, events=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.get_me_error = get_me_error
        self.events = events
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
        if self.events is not None:
            self.events.log.append(("send", kwargs["chat_id"], kwargs["text"]))
        queue = self.script.get(kwargs["chat_id"])
        if queue:
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome

    def calls_for(self, chat_id):
        return [c for c in self.calls if c["chat_id"] == chat_id]


class FakeDelivery:
    def __init__(self, events=None):
        self.events = events
        self.acked, self.released, self.dead, self.blocked_calls = [], [], [], []

    def _log(self, *entry):
        if self.events is not None:
            self.events.log.append(entry)

    def ack_sent(self, ids):
        self.acked.append(list(ids))
        self._log("ack", list(ids))

    def release(self, ids, error_text):
        self.released.append((list(ids), error_text))
        self._log("release", list(ids), error_text)

    def mark_dead(self, ids, error_text):
        self.dead.append((list(ids), error_text))
        self._log("dead", list(ids), error_text)

    def blocked(self, chat_id, ids):
        self.blocked_calls.append((chat_id, list(ids)))
        self._log("blocked", chat_id, list(ids))

    @property
    def all_acked(self):
        return [i for ids in self.acked for i in ids]


@pytest.fixture
def env(monkeypatch):
    """A token is set and .env loading is neutralised."""
    monkeypatch.setattr(tm, "load_dotenv", lambda: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake-token")


def use_bot(monkeypatch, bot):
    monkeypatch.setattr(tm, "Bot", lambda token: bot)


def row(i, domain=ASURA, **overrides):
    base = {
        "notification_id": f"q{i}", "novel_name": f"novel-{i}", "display_title": f"Novel {i}",
        "web_domain_name": domain, "chapter": float(i), "chapter_url": f"https://example.com/series/{i}/chapter-{i}",
        "access_state": "free", "unlock_at": None,
    }
    base.update(overrides)
    return base


def many_rows(count, name_len=40, domain=ASURA):
    return [row(i, domain, display_title=f"Novel {i:04d} " + "x" * name_len) for i in range(1, count + 1)]


def run(batches, delivery, rules=RULES, now=NOW):
    return tm.send_all(batches, delivery, rules=rules, now=now)


# ---------------------------------------------------------------- token

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
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "  123:abc  ")
    assert tm.get_token() == "123:abc"


# ---------------------------------------------------------------- build_message: shape

def test_build_message_empty_returns_empty_list():
    assert tm.build_message([], RULES, NOW) == []
    assert tm.build_message(None, RULES, NOW) == []


def test_build_message_single_row_matches_the_documented_layout():
    [chunk] = tm.build_message([row(1, display_title="Killer Pietro", chapter=156.0,
                                    chapter_url="https://asurascans.com/comics/killer-pietro/chapter/156")], RULES, NOW)
    assert chunk["row_ids"] == ["q1"]
    assert chunk["text"] == (
        "<b>\U0001F4DA New chapters</b>\n"
        "\n"
        "<b>Asura Scans</b>\n"
        '\u2022 <a href="https://asurascans.com/comics/killer-pietro/chapter/156">Killer Pietro</a> \u2014 Ch. 156'
    )


def test_build_message_groups_by_site_with_a_blank_line_between_sites():
    rows = [row(1, ASURA, display_title="A"), row(2, HIVE, display_title="B")]
    [chunk] = tm.build_message(rows, RULES, NOW)
    lines = chunk["text"].split("\n")
    assert lines[0].startswith("<b>") and lines[1] == "" and lines[2] == "<b>Asura Scans</b>"
    assert lines[4] == "" and lines[5] == "<b>Hive Toons</b>"


def test_sites_are_sorted_by_display_name_and_novels_by_title_then_chapter():
    rows = [
        row(1, HIVE, display_title="zeta", chapter=1.0),
        row(2, ASURA, display_title="banana", chapter=9.0),
        row(3, ASURA, display_title="Apple", chapter=2.0),
        row(4, ASURA, display_title="apple", chapter=1.0),
        row(5, HIVE, display_title="Alpha", chapter=3.0),
    ]
    [chunk] = tm.build_message(rows, RULES, NOW)
    assert chunk["row_ids"] == ["q4", "q3", "q2", "q5", "q1"]     # Asura: apple(1), Apple(2), banana; Hive: Alpha, zeta
    assert chunk["text"].index("Asura Scans") < chunk["text"].index("Hive Toons")


def test_site_name_falls_back_to_the_host_then_to_unknown():
    [chunk] = tm.build_message([row(1, "https://other.example/")], {}, NOW)
    assert "<b>other.example</b>" in chunk["text"]
    [chunk] = tm.build_message([row(1, None)], RULES, NOW)
    assert "<b>Unknown site</b>" in chunk["text"]
    [chunk] = tm.build_message([row(1, ASURA)], {ASURA: {"display_name": "   "}}, NOW)
    assert "<b>asurascans.com</b>" in chunk["text"]


def test_build_message_does_not_mutate_its_input():
    rows = [row(2), row(1)]
    snapshot = [dict(r) for r in rows]
    tm.build_message(rows, RULES, NOW)
    assert rows == snapshot


def test_rows_without_a_notification_id_are_shown_but_have_no_id():
    [chunk] = tm.build_message([row(1, notification_id=None)], RULES, NOW)
    assert chunk["row_ids"] == [] and "Novel 1" in chunk["text"]


# ---------------------------------------------------------------- build_message: titles, escaping, links

def test_title_prefers_the_scraped_title_and_falls_back_to_a_pretty_slug():
    [chunk] = tm.build_message([row(1, display_title="Real Title", novel_name="real-title-x"),
                                row(2, display_title=None, novel_name="the-hero-s-ballad")], RULES, NOW)
    assert ">Real Title</a>" in chunk["text"]
    assert ">The Hero's Ballad</a>" in chunk["text"]


def test_build_message_escapes_title_site_name_and_url():
    evil = '<b>&"x'
    [chunk] = tm.build_message(
        [row(1, display_title=evil, chapter_url='https://e.com/a"onclick="x&y=1')],
        {ASURA: {"display_name": evil}}, NOW)
    text = chunk["text"]
    assert "&lt;b&gt;&amp;\"x</a>" in text                       # title: & < > escaped
    assert "<b>&lt;b&gt;&amp;\"x</b>" in text                    # site heading escaped too
    assert 'href="https://e.com/a&quot;onclick=&quot;x&amp;y=1"' in text   # the quote cannot leave the attribute
    assert "<b>&" not in text.replace("<b>&lt;", "")


def test_apostrophes_stay_plain_not_numeric_entities():
    [chunk] = tm.build_message([row(1, display_title="The Hero's Ballad")], RULES, NOW)
    assert ">The Hero's Ballad</a>" in chunk["text"] and "&#" not in chunk["text"]


def test_very_long_titles_are_cut_with_an_ellipsis():
    [chunk] = tm.build_message([row(1, display_title="x" * 500)], RULES, NOW)
    assert ">" + "x" * (tm.MAX_NAME_LEN - 1) + "\u2026</a>" in chunk["text"]
    assert "x" * tm.MAX_NAME_LEN + "<" not in chunk["text"]


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "ftp://files.example/x", "data:text/html,hi", "/relative/path", "mailto:a@b.c", "", None, "   "],
)
def test_only_http_and_https_urls_become_links(url):
    [chunk] = tm.build_message([row(1, chapter_url=url)], RULES, NOW)
    assert "<a " not in chunk["text"] and "Novel 1 \u2014 Ch. 1" in chunk["text"]


@pytest.mark.parametrize("url", ["http://example.com/c/1", "https://example.com/c/1", "HTTPS://EXAMPLE.COM/c/1"])
def test_http_and_https_urls_are_linked(url):
    [chunk] = tm.build_message([row(1, chapter_url=url)], RULES, NOW)
    assert f'<a href="{url}">' in chunk["text"]


def test_an_absurdly_long_url_is_not_linked():
    [chunk] = tm.build_message([row(1, chapter_url="https://e.com/" + "a" * (tm.MAX_URL_LEN + 1))], RULES, NOW)
    assert "<a " not in chunk["text"]


@pytest.mark.parametrize(
    "value, expected",
    [(631.0, "631"), (631, "631"), (12.5, "12.5"), ("7", "7"), (None, "?"), ("N/A", "N/A"), (0, "0")],
)
def test_chapter_formatting(value, expected):
    [chunk] = tm.build_message([row(1, chapter=value)], RULES, NOW)
    assert chunk["text"].endswith(f"\u2014 Ch. {expected}")


def test_the_old_latest_chap_key_is_still_understood():
    r = row(1)
    del r["chapter"]
    r["latest_chap"] = 12.5
    [chunk] = tm.build_message([r], RULES, NOW)
    assert chunk["text"].endswith("Ch. 12.5")


# ---------------------------------------------------------------- build_message: badges

def test_locked_with_a_future_unlock_shows_the_remaining_time():
    r = row(1, access_state="locked", unlock_at=(NOW + timedelta(hours=5)).isoformat())
    assert tm.build_message([r], RULES, NOW)[0]["text"].endswith("Ch. 1 \U0001F512 free in ~5h")


def test_locked_with_no_unlock_time_says_early_access():
    r = row(1, access_state="locked", unlock_at=None)
    assert tm.build_message([r], RULES, NOW)[0]["text"].endswith("Ch. 1 \U0001F512 early access")


def test_the_badge_follows_the_time_of_sending():
    r = row(1, access_state="locked", unlock_at=(NOW + timedelta(hours=5)).isoformat())
    assert "free in ~5h" in tm.build_message([r], RULES, NOW)[0]["text"]
    assert "free in ~<1h" in tm.build_message([r], RULES, NOW + timedelta(hours=4, minutes=30))[0]["text"]
    assert "\U0001F512" not in tm.build_message([r], RULES, NOW + timedelta(hours=6))[0]["text"]   # already free


@pytest.mark.parametrize("state", ["free", "unknown", None])
def test_free_and_unknown_chapters_have_no_badge(state):
    r = row(1, access_state=state, unlock_at=(NOW + timedelta(hours=5)).isoformat())
    assert "\U0001F512" not in tm.build_message([r], RULES, NOW)[0]["text"]


# ---------------------------------------------------------------- build_message: chunking

def test_chunks_respect_the_limit_and_keep_tags_intact():
    chunks = tm.build_message(many_rows(120), RULES, NOW)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk["text"]) <= tm.MAX_MESSAGE_LEN
        assert chunk["text"].count("<a ") == chunk["text"].count("</a>")
        assert chunk["text"].count("<b>") == chunk["text"].count("</b>")


def test_chunks_are_numbered_only_when_there_is_more_than_one():
    assert tm.build_message(many_rows(2), RULES, NOW)[0]["text"].startswith("<b>\U0001F4DA New chapters</b>")
    chunks = tm.build_message(many_rows(120), RULES, NOW)
    for i, chunk in enumerate(chunks, start=1):
        assert chunk["text"].startswith(f"<b>\U0001F4DA New chapters ({i}/{len(chunks)})</b>")


def test_every_row_is_in_exactly_one_chunk_in_display_order():
    chunks = tm.build_message(many_rows(120), RULES, NOW)
    ids = [i for chunk in chunks for i in chunk["row_ids"]]
    assert ids == [f"q{i}" for i in range(1, 121)]


def test_the_site_heading_is_repeated_when_a_site_continues_in_the_next_chunk():
    chunks = tm.build_message(many_rows(120), RULES, NOW)
    assert all(chunk["text"].count("<b>Asura Scans</b>") == 1 for chunk in chunks)
    assert all("<b>Asura Scans</b>" in chunk["text"] for chunk in chunks)


def test_a_new_site_starting_a_chunk_gets_its_heading_once():
    rows = many_rows(60, domain=ASURA) + [row(1000 + i, HIVE, display_title=f"Hive {i:03d}") for i in range(60)]
    chunks = tm.build_message(rows, RULES, NOW)
    for chunk in chunks:
        assert chunk["text"].count("<b>Hive Toons</b>") <= 1
        assert len(chunk["text"]) <= tm.MAX_MESSAGE_LEN
    assert sum(c["text"].count("<b>Hive Toons</b>") for c in chunks) >= 1


def test_a_tiny_limit_still_puts_at_least_one_line_in_every_chunk(monkeypatch):
    monkeypatch.setattr(tm, "MAX_MESSAGE_LEN", 80)
    chunks = tm.build_message(many_rows(5, name_len=60), RULES, NOW)
    assert len(chunks) == 5 and all(len(c["row_ids"]) == 1 for c in chunks)


# ---------------------------------------------------------------- send_one

def test_send_one_success_uses_html_and_disables_previews():
    bot = FakeBot()
    assert asyncio.run(tm.send_one(bot, 1, "hello")) == ("sent", None)
    call = bot.calls[0]
    assert call["chat_id"] == 1 and call["text"] == "hello"
    assert call["parse_mode"] == "HTML" and call["disable_notification"] is False
    assert call["link_preview_options"].is_disabled is True


def test_send_one_can_send_plain_text_and_silent():
    bot = FakeBot()
    asyncio.run(tm.send_one(bot, 1, "hello", parse_mode=None, disable_notification=True))
    call = bot.calls[0]
    assert "parse_mode" not in call and call["disable_notification"] is True


def test_bad_request_is_a_network_error_subclass():
    """Why the except-clause order in send_one matters."""
    assert issubclass(BadRequest, NetworkError) and issubclass(TimedOut, NetworkError)


@pytest.mark.parametrize(
    "error, expected_status",
    [
        (Forbidden("bot was blocked by the user"), "blocked"),
        (BadRequest("chat not found"), "bad_request"),
        (TimedOut("timed out"), "network"),
        (NetworkError("connection reset"), "network"),
        (TelegramError("something else"), "unknown"),
    ],
)
def test_send_one_classifies_errors(error, expected_status):
    status, detail = asyncio.run(tm.send_one(FakeBot({1: [error]}), 1, "x"))
    assert status == expected_status and detail == str(error)


def test_send_one_reraises_invalid_token():
    with pytest.raises(InvalidToken):
        asyncio.run(tm.send_one(FakeBot({1: [InvalidToken("bad")]}), 1, "x"))


@pytest.mark.parametrize("raw, seconds", [(3, 3), (2.2, 3), (timedelta(seconds=7), 7), (0, 1)])
def test_send_one_rate_limited_returns_wait_seconds(raw, seconds):
    assert asyncio.run(tm.send_one(FakeBot({1: [RetryAfter(raw)]}), 1, "x")) == ("rate_limited", seconds)


# ---------------------------------------------------------------- send_with_flood_wait

def test_flood_wait_sleeps_then_succeeds(sleeps):
    bot = FakeBot({1: [RetryAfter(4)]})
    assert asyncio.run(tm.send_with_flood_wait(bot, 1, "x")) == ("sent", None)
    assert pauses(sleeps) == [4] and len(bot.calls) == 2


def test_flood_wait_gives_up_after_max_retries(sleeps, monkeypatch):
    monkeypatch.setattr(tm, "MAX_FLOOD_RETRIES", 2)
    bot = FakeBot({1: [RetryAfter(1)] * 5})
    assert asyncio.run(tm.send_with_flood_wait(bot, 1, "x")) == ("rate_limited", 1)
    assert len(bot.calls) == 3 and pauses(sleeps) == [1, 1]


def test_flood_wait_does_not_sleep_past_the_cap(sleeps, monkeypatch):
    monkeypatch.setattr(tm, "MAX_FLOOD_WAIT", 10)
    bot = FakeBot({1: [RetryAfter(500)]})
    assert asyncio.run(tm.send_with_flood_wait(bot, 1, "x")) == ("rate_limited", 500)
    assert len(bot.calls) == 1 and pauses(sleeps) == []


@pytest.mark.parametrize("error, status", [(Forbidden("blocked"), "blocked"), (BadRequest("bad"), "bad_request"), (NetworkError("net"), "network")])
def test_flood_wait_passes_other_statuses_straight_through(error, status):
    bot = FakeBot({1: [error]})
    assert asyncio.run(tm.send_with_flood_wait(bot, 1, "x"))[0] == status
    assert len(bot.calls) == 1


def test_flood_wait_forwards_the_formatting_options():
    bot = FakeBot()
    asyncio.run(tm.send_with_flood_wait(bot, 1, "x", parse_mode=None, disable_notification=True))
    assert "parse_mode" not in bot.calls[0] and bot.calls[0]["disable_notification"] is True


# ---------------------------------------------------------------- send_all: setup and the happy path

def test_send_all_missing_token_fails_before_creating_a_bot(monkeypatch):
    monkeypatch.setattr(tm, "load_dotenv", lambda: None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(tm, "Bot", lambda token: pytest.fail("a Bot must not be created without a token"))
    with pytest.raises(RuntimeError):
        run({111: [row(1)]}, FakeDelivery())


def test_send_all_empty_input_returns_a_zero_summary_without_a_bot(env, monkeypatch):
    monkeypatch.setattr(tm, "Bot", lambda token: pytest.fail("no bot needed for nothing to send"))
    assert run({}, FakeDelivery()) == {"sent": 0, "blocked": 0, "bad_request": 0, "failed": 0, "retried": 0}


def test_send_all_happy_path(env, monkeypatch):
    bot, delivery = FakeBot(), FakeDelivery()
    use_bot(monkeypatch, bot)
    summary = run({111: [row(1), row(2)], 222: [row(3)]}, delivery)
    assert summary == {"sent": 3, "blocked": 0, "bad_request": 0, "failed": 0, "retried": 0}
    assert len(bot.calls) == 2 and sorted(delivery.all_acked) == ["q1", "q2", "q3"]
    assert delivery.released == [] and delivery.dead == [] and delivery.blocked_calls == []


def test_the_summary_is_counts_only(env, monkeypatch):
    use_bot(monkeypatch, FakeBot({111: [BadRequest("chat not found: secret detail")] * 2}))
    summary = run({111: [row(1)]}, FakeDelivery())
    assert all(isinstance(v, int) for v in summary.values())


def test_each_chunk_is_acknowledged_right_after_it_is_sent_not_at_the_end(env, monkeypatch):
    events = Events()
    use_bot(monkeypatch, FakeBot(events=events))
    delivery = FakeDelivery(events)
    run({111: many_rows(120)}, delivery)
    kinds = [e[0] for e in events.log]
    assert kinds == ["send", "ack"] * (len(kinds) // 2)              # send, ack, send, ack, ...
    assert len(delivery.acked) == len(events.of("send"))


def test_the_rules_give_the_site_names_in_the_sent_text(env, monkeypatch):
    bot = FakeBot()
    use_bot(monkeypatch, bot)
    run({111: [row(1)]}, FakeDelivery())
    assert "<b>Asura Scans</b>" in bot.calls[0]["text"]
    bot2 = FakeBot()
    use_bot(monkeypatch, bot2)
    tm.send_all({111: [row(1)]}, FakeDelivery())                      # no rules: the host is used
    assert "<b>asurascans.com</b>" in bot2.calls[0]["text"]


def test_the_badge_uses_the_now_passed_in(env, monkeypatch):
    bot = FakeBot()
    use_bot(monkeypatch, bot)
    r = row(1, access_state="locked", unlock_at=(NOW + timedelta(hours=5)).isoformat())
    run({111: [r]}, FakeDelivery())
    assert "free in ~5h" in bot.calls[0]["text"]


def test_chunk_one_notifies_and_later_chunks_are_silent(env, monkeypatch):
    bot = FakeBot()
    use_bot(monkeypatch, bot)
    run({111: many_rows(120)}, FakeDelivery())
    flags = [c["disable_notification"] for c in bot.calls_for(111)]
    assert len(flags) > 1 and flags[0] is False and all(flags[1:])


def test_links_never_unfurl_into_previews(env, monkeypatch):
    bot = FakeBot()
    use_bot(monkeypatch, bot)
    run({111: [row(1)]}, FakeDelivery())
    assert all(c["link_preview_options"].is_disabled for c in bot.calls)


def test_send_all_pauses_between_chunks_of_the_same_user(env, monkeypatch, sleeps):
    use_bot(monkeypatch, FakeBot())
    chunks = len(tm.build_message(many_rows(120), RULES, NOW))
    run({111: many_rows(120)}, FakeDelivery())
    assert pauses(sleeps).count(tm.CHUNK_PAUSE) == chunks - 1


def test_send_all_spaces_every_send_by_the_minimum_gap(env, monkeypatch, sleeps):
    use_bot(monkeypatch, FakeBot())
    run({1: [row(1)], 2: [row(2)], 3: [row(3)], 4: [row(4)]}, FakeDelivery())
    assert sleeps.count(tm.SEND_SPACING) == 3                          # between 4 sends, none before the first


def test_the_old_batch_pause_rule_is_gone():
    assert not hasattr(tm, "BATCH_SIZE") and not hasattr(tm, "BATCH_PAUSE")
    assert tm.SEND_SPACING == 0.05 and tm.CHUNK_PAUSE == 1


def test_real_spacing_clock_waits_only_the_remaining_gap(monkeypatch, sleeps):
    ticks = iter([10.0, 10.02, 10.02, 10.5])
    monkeypatch.setattr(tm, "_now", lambda: next(ticks))
    run_state = tm._Run(FakeBot(), FakeDelivery(), {}, NOW)
    asyncio.run(run_state.pace())                                      # first send: no wait
    asyncio.run(run_state.pace())                                      # 0.02 s later: wait the remaining 0.03
    assert sleeps == [pytest.approx(0.03)]


def test_one_users_failure_does_not_stop_the_others(env, monkeypatch):
    bot, delivery = FakeBot({111: [Forbidden("blocked")]}), FakeDelivery()
    use_bot(monkeypatch, bot)
    summary = run({111: [row(1)], 222: [row(2)]}, delivery)
    assert summary["blocked"] == 1 and summary["sent"] == 1
    assert delivery.all_acked == ["q2"]


# ---------------------------------------------------------------- send_all: blocked users

def test_a_blocked_user_gets_blocked_called_with_all_their_unsent_ids(env, monkeypatch):
    rows = many_rows(120)
    chunks = tm.build_message(rows, RULES, NOW)
    assert len(chunks) >= 3
    bot, delivery = FakeBot({111: [None, Forbidden("blocked")]}), FakeDelivery()   # chunk 1 ok, chunk 2 blocked
    use_bot(monkeypatch, bot)

    summary = run({111: rows}, delivery)

    assert len(bot.calls_for(111)) == 2                                # chunk 3+ never attempted
    assert delivery.acked == [chunks[0]["row_ids"]]                    # chunk 1 stays acknowledged
    [(chat_id, ids)] = delivery.blocked_calls
    expected = sorted([i for c in chunks[1:] for i in c["row_ids"]], key=str)
    assert chat_id == 111 and ids == expected                          # chunk 2 (the blocked one) and everything after
    assert summary["blocked"] == 1 and summary["sent"] == len(chunks[0]["row_ids"])


def test_blocked_users_do_not_stop_other_users(env, monkeypatch):
    bot = FakeBot({111: [Forbidden("blocked")]})
    use_bot(monkeypatch, bot)
    delivery = FakeDelivery()
    run({111: [row(1)], 222: [row(2)]}, delivery)
    assert bot.calls_for(222) and delivery.blocked_calls == [(111, ["q1"])]


def test_a_blocked_users_waiting_retries_are_dropped_and_their_ids_included(env, monkeypatch):
    rows = many_rows(120)
    chunks = tm.build_message(rows, RULES, NOW)
    # chunk 1: network error (queued for retry); chunk 2: blocked
    bot, delivery = FakeBot({111: [NetworkError("down"), Forbidden("blocked")]}), FakeDelivery()
    use_bot(monkeypatch, bot)

    summary = run({111: rows}, delivery)

    [(chat_id, ids)] = delivery.blocked_calls
    assert set(chunks[0]["row_ids"]) <= set(ids) and set(chunks[1]["row_ids"]) <= set(ids)
    assert delivery.released == [] and delivery.acked == []
    assert len(bot.calls_for(111)) == 2                                # the queued retry was never sent
    assert summary["blocked"] == 1 and summary["failed"] == 0


# ---------------------------------------------------------------- send_all: transient failures and retries

def test_a_network_failure_is_retried_and_counted_as_retried(env, monkeypatch, sleeps):
    bot, delivery = FakeBot({111: [NetworkError("blip")]}), FakeDelivery()
    use_bot(monkeypatch, bot)
    summary = run({111: [row(1), row(2)]}, delivery)
    assert summary == {"sent": 2, "blocked": 0, "bad_request": 0, "failed": 0, "retried": 2}
    assert delivery.released == [] and delivery.all_acked == ["q1", "q2"]
    assert tm.ROUND_DELAY in sleeps and len(bot.calls) == 2


def test_a_first_try_success_is_not_counted_as_retried(env, monkeypatch):
    use_bot(monkeypatch, FakeBot())
    assert run({111: [row(1)]}, FakeDelivery())["retried"] == 0


def test_rows_are_released_when_the_retries_run_out(env, monkeypatch, sleeps):
    bot, delivery = FakeBot({111: [NetworkError("down")] * 10}), FakeDelivery()
    use_bot(monkeypatch, bot)
    summary = run({111: [row(1), row(2)]}, delivery)
    assert delivery.released == [(["q1", "q2"], "network")]
    assert delivery.all_acked == [] and delivery.dead == []
    assert summary["failed"] == 2 and summary["sent"] == 0
    assert len(bot.calls) == 1 + tm.MAX_RETRY_ROUNDS
    assert pauses(sleeps).count(tm.ROUND_DELAY) == tm.MAX_RETRY_ROUNDS


def test_a_rate_limit_longer_than_the_cap_is_deferred_to_the_retry_rounds(env, monkeypatch):
    bot, delivery = FakeBot({111: [RetryAfter(tm.MAX_FLOOD_WAIT + 100)]}), FakeDelivery()
    use_bot(monkeypatch, bot)
    summary = run({111: [row(1)]}, delivery)
    assert summary["sent"] == 1 and summary["retried"] == 1 and delivery.released == []


def test_rate_limited_forever_is_released_with_that_category(env, monkeypatch):
    use_bot(monkeypatch, FakeBot({111: [RetryAfter(tm.MAX_FLOOD_WAIT + 100)] * 10}))
    delivery = FakeDelivery()
    run({111: [row(1)]}, delivery)
    assert delivery.released == [(["q1"], "rate_limited")]


def test_an_unknown_telegram_error_is_retried_and_then_released_as_unknown(env, monkeypatch):
    bot, delivery = FakeBot({111: [TelegramError("weird")] * 10}), FakeDelivery()
    use_bot(monkeypatch, bot)
    run({111: [row(1)]}, delivery)
    assert len(bot.calls) == 1 + tm.MAX_RETRY_ROUNDS
    assert delivery.released == [(["q1"], "unknown")]


def test_an_unknown_error_that_clears_is_delivered(env, monkeypatch):
    delivery = FakeDelivery()
    use_bot(monkeypatch, FakeBot({111: [TelegramError("weird")]}))
    assert run({111: [row(1)]}, delivery)["sent"] == 1 and delivery.released == []


def test_one_users_transient_failure_does_not_affect_another(env, monkeypatch):
    use_bot(monkeypatch, FakeBot({111: [NetworkError("down")] * 10}))
    delivery = FakeDelivery()
    summary = run({111: [row(1)], 222: [row(2)]}, delivery)
    assert delivery.all_acked == ["q2"] and delivery.released == [(["q1"], "network")]
    assert summary["sent"] == 1 and summary["failed"] == 1


# ---------------------------------------------------------------- send_all: bad requests (the three-step fallback)

def test_a_refused_chunk_is_resent_one_message_per_row_and_each_row_acknowledged_separately(env, monkeypatch):
    bot, delivery = FakeBot({111: [BadRequest("can't parse entities")]}), FakeDelivery()
    use_bot(monkeypatch, bot)
    summary = run({111: [row(1), row(2), row(3)]}, delivery)
    assert summary == {"sent": 3, "blocked": 0, "bad_request": 0, "failed": 0, "retried": 0}
    assert delivery.acked == [["q1"], ["q2"], ["q3"]]
    assert len(bot.calls) == 1 + 3
    assert all(c["parse_mode"] == "HTML" for c in bot.calls)           # still escaped HTML at step (a)
    assert all(c["text"].count("\u2022") == 1 for c in bot.calls[1:])  # exactly one novel per message


def test_a_row_still_refused_as_html_is_sent_as_plain_text_with_the_url_on_its_own_line(env, monkeypatch):
    # chunk refused; row 1 ok; row 2 refused as HTML, ok as plain text
    bot, delivery = FakeBot({111: [BadRequest("x"), None, BadRequest("x"), None]}), FakeDelivery()
    use_bot(monkeypatch, bot)
    summary = run({111: [row(1), row(2, display_title="Tom & <Jerry>")]}, delivery)
    plain = bot.calls[3]
    assert "parse_mode" not in plain
    assert plain["text"] == "Tom & <Jerry> \u2014 Ch. 2\nhttps://example.com/series/2/chapter-2"
    assert "<a" not in plain["text"] and "<b>" not in plain["text"]
    assert delivery.acked == [["q1"], ["q2"]] and delivery.dead == []
    assert summary["sent"] == 2 and summary["bad_request"] == 0


def test_a_row_refused_even_as_plain_text_is_marked_dead(env, monkeypatch):
    # chunk, row1 html, row1 plain: all refused; row 2 html ok
    bot, delivery = FakeBot({111: [BadRequest("x"), BadRequest("x"), BadRequest("x"), None]}), FakeDelivery()
    use_bot(monkeypatch, bot)
    summary = run({111: [row(1), row(2)]}, delivery)
    assert delivery.dead == [(["q1"], "bad_request")]
    assert delivery.acked == [["q2"]]
    assert summary["sent"] == 1 and summary["bad_request"] == 1


def test_a_single_row_chunk_goes_straight_to_plain_text(env, monkeypatch):
    bot, delivery = FakeBot({111: [BadRequest("x")]}), FakeDelivery()
    use_bot(monkeypatch, bot)
    summary = run({111: [row(1)]}, delivery)
    assert len(bot.calls) == 2 and "parse_mode" not in bot.calls[1]
    assert delivery.acked == [["q1"]] and summary["sent"] == 1


def test_a_single_row_refused_everywhere_is_dead_after_exactly_two_attempts(env, monkeypatch):
    bot, delivery = FakeBot({111: [BadRequest("x")] * 5}), FakeDelivery()
    use_bot(monkeypatch, bot)
    summary = run({111: [row(1)]}, delivery)
    assert len(bot.calls) == 2 and delivery.dead == [(["q1"], "bad_request")] and summary["bad_request"] == 1
    assert delivery.released == [] and delivery.all_acked == []


def test_bad_request_detail_is_never_stored_or_returned(env, monkeypatch):
    use_bot(monkeypatch, FakeBot({111: [BadRequest("chat not found, token 123:SECRET")] * 5}))
    delivery = FakeDelivery()
    summary = run({111: [row(1)]}, delivery)
    assert delivery.dead == [(["q1"], "bad_request")]                  # the fixed label, not Telegram's text
    assert "SECRET" not in repr(summary) + repr(delivery.dead)


def test_other_chunks_continue_after_a_chunk_needs_the_fallback(env, monkeypatch):
    rows = many_rows(120)
    chunks = tm.build_message(rows, RULES, NOW)
    bot, delivery = FakeBot({111: [BadRequest("x")]}), FakeDelivery()
    use_bot(monkeypatch, bot)
    summary = run({111: rows}, delivery)
    assert summary["sent"] == 120 and sorted(delivery.all_acked) == sorted(i for c in chunks for i in c["row_ids"])


def test_blocked_during_the_fallback_stops_the_remaining_rows(env, monkeypatch):
    # chunk refused; row 1 blocked -> rows 2 and 3 are not tried
    bot, delivery = FakeBot({111: [BadRequest("x"), Forbidden("blocked")]}), FakeDelivery()
    use_bot(monkeypatch, bot)
    summary = run({111: [row(1), row(2), row(3)]}, delivery)
    assert len(bot.calls) == 2
    [(chat_id, ids)] = delivery.blocked_calls
    assert chat_id == 111 and ids == ["q1", "q2", "q3"] and summary["blocked"] == 1


def test_a_network_error_in_the_fallback_is_retried_with_the_same_format(env, monkeypatch):
    # chunk refused; row 1 html -> network error (queued); row 2 ok; the retry of row 1 succeeds
    bot, delivery = FakeBot({111: [BadRequest("x"), NetworkError("down"), None, None]}), FakeDelivery()
    use_bot(monkeypatch, bot)
    summary = run({111: [row(1), row(2)]}, delivery)
    assert sorted(delivery.all_acked) == ["q1", "q2"]
    assert summary["sent"] == 2 and summary["retried"] == 1
    assert bot.calls[-1]["parse_mode"] == "HTML" and "Novel 1" in bot.calls[-1]["text"]


# ---------------------------------------------------------------- send_all: token problems

def test_a_bad_token_fails_in_preflight_before_any_send_or_acknowledgement(env, monkeypatch):
    bot, delivery = FakeBot(get_me_error=InvalidToken("nope")), FakeDelivery()
    use_bot(monkeypatch, bot)
    with pytest.raises(InvalidToken):
        run({111: [row(1)]}, delivery)
    assert bot.calls == [] and delivery.acked == [] and delivery.released == []


def test_a_token_revoked_mid_run_propagates_and_leaves_rows_for_recovery(env, monkeypatch):
    bot, delivery = FakeBot({222: [InvalidToken("revoked")]}), FakeDelivery()
    use_bot(monkeypatch, bot)
    with pytest.raises(InvalidToken):
        run({111: [row(1)], 222: [row(2)], 333: [row(3)]}, delivery)
    assert delivery.all_acked == ["q1"]                                # delivered before the failure
    assert delivery.released == [] and delivery.dead == []             # q2/q3 stay 'sending'; maintain recovers them
    assert bot.calls_for(333) == []


def test_non_token_preflight_errors_are_ignored(env, monkeypatch, capsys):
    bot = FakeBot(get_me_error=NetworkError("blip"))
    use_bot(monkeypatch, bot)
    assert run({111: [row(1)]}, FakeDelivery())["sent"] == 1
    assert "preflight" in capsys.readouterr().out


# ---------------------------------------------------------------- module rules

def test_the_messenger_knows_nothing_about_the_database_or_main():
    source = inspect.getsource(tm)
    assert "import database" not in source and "from src import database" not in source
    assert "import main" not in source and "supabase" not in source.lower()


def test_nothing_runs_at_import_time_and_no_token_is_read_at_module_level():
    assert not hasattr(tm, "TOKEN") and not hasattr(tm, "BOT_TOKEN")


# ---------------------------------------------------------------- send_admin (alerts and the digest)

ADMIN_TEXT = "\U0001F6A8 <b>ScrollPing alert</b>\n<b>Asura &amp; Co</b>\nError: <code>a &lt; b</code>"


def test_strip_tags_gives_readable_plain_text():
    assert tm._strip_tags(ADMIN_TEXT) == "\U0001F6A8 ScrollPing alert\nAsura & Co\nError: a < b"


def test_send_admin_needs_a_token(monkeypatch):
    monkeypatch.setattr(tm, "load_dotenv", lambda: None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(tm, "Bot", lambda token: pytest.fail("no bot without a token"))
    with pytest.raises(RuntimeError):
        tm.send_admin([1], ADMIN_TEXT)


def test_send_admin_with_nobody_to_tell_does_not_create_a_bot(env, monkeypatch):
    monkeypatch.setattr(tm, "Bot", lambda token: pytest.fail("no bot needed"))
    assert tm.send_admin([], ADMIN_TEXT) == {"delivered": 0, "failed": 0}
    assert tm.send_admin(None, ADMIN_TEXT) == {"delivered": 0, "failed": 0}


def test_send_admin_delivers_html_to_every_admin_once(env, monkeypatch):
    bot = FakeBot()
    use_bot(monkeypatch, bot)
    assert tm.send_admin([11, 22, 11], ADMIN_TEXT) == {"delivered": 2, "failed": 0}
    assert [c["chat_id"] for c in bot.calls] == [11, 22]
    assert all(c["text"] == ADMIN_TEXT and c["parse_mode"] == "HTML" for c in bot.calls)
    assert all(c["link_preview_options"].is_disabled and c["disable_notification"] is False for c in bot.calls)


def test_send_admin_spaces_its_sends(env, monkeypatch, sleeps):
    use_bot(monkeypatch, FakeBot())
    tm.send_admin([1, 2, 3], ADMIN_TEXT)
    assert sleeps.count(tm.SEND_SPACING) == 2


def test_an_admin_who_blocked_the_bot_does_not_stop_the_others(env, monkeypatch):
    bot = FakeBot({11: [Forbidden("blocked")]})
    use_bot(monkeypatch, bot)
    assert tm.send_admin([11, 22], ADMIN_TEXT) == {"delivered": 1, "failed": 1}
    assert len(bot.calls_for(11)) == 1                   # a block is final: no retries


def test_refused_html_is_resent_once_as_plain_text(env, monkeypatch):
    bot = FakeBot({11: [BadRequest("can't parse entities")]})
    use_bot(monkeypatch, bot)
    assert tm.send_admin([11], ADMIN_TEXT) == {"delivered": 1, "failed": 0}
    first, second = bot.calls
    assert first["parse_mode"] == "HTML" and "parse_mode" not in second
    assert "<b>" not in second["text"] and "Asura & Co" in second["text"]


def test_refused_as_plain_text_too_is_a_failure(env, monkeypatch):
    bot = FakeBot({11: [BadRequest("x"), BadRequest("x")]})
    use_bot(monkeypatch, bot)
    assert tm.send_admin([11], ADMIN_TEXT) == {"delivered": 0, "failed": 1}
    assert len(bot.calls) == 2


def test_transient_failures_are_retried_in_rounds(env, monkeypatch, sleeps):
    bot = FakeBot({11: [NetworkError("down")], 22: [TelegramError("odd")]})
    use_bot(monkeypatch, bot)
    assert tm.send_admin([11, 22], ADMIN_TEXT) == {"delivered": 2, "failed": 0}
    assert tm.ROUND_DELAY in sleeps and len(bot.calls) == 4


def test_a_permanently_failing_admin_is_given_up_after_the_retry_rounds(env, monkeypatch):
    bot = FakeBot({11: [NetworkError("down")] * 10})
    use_bot(monkeypatch, bot)
    assert tm.send_admin([11, 22], ADMIN_TEXT) == {"delivered": 1, "failed": 1}
    assert len(bot.calls_for(11)) == 1 + tm.MAX_RETRY_ROUNDS


def test_a_long_rate_limit_is_deferred_to_a_retry_round(env, monkeypatch):
    bot = FakeBot({11: [RetryAfter(tm.MAX_FLOOD_WAIT + 100)]})
    use_bot(monkeypatch, bot)
    assert tm.send_admin([11], ADMIN_TEXT) == {"delivered": 1, "failed": 0}


def test_send_admin_lets_a_rejected_token_escape(env, monkeypatch):
    use_bot(monkeypatch, FakeBot(get_me_error=InvalidToken("nope")))
    with pytest.raises(InvalidToken):
        tm.send_admin([11], ADMIN_TEXT)
