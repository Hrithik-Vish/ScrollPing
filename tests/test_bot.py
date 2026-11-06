"""src/bot.py: helpers, process_add, and every handler, using fake Telegram objects.
No network and no database: bot.database is replaced with a small in-memory fake."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop

from src import bot
from src.ratelimiter import TokenBucketLimiter

RULES = bot.load_rules()
UUID1 = "11111111-1111-4111-8111-111111111111"
UUID2 = "22222222-2222-4222-8222-222222222222"
USER = {"id": "user-1", "username": "h", "telegram_user_id": 7, "status": "active"}


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ fakes

class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


class FakeCallbackQuery:
    def __init__(self, data):
        self.data = data
        self.answers = []
        self.edits = []
        self.edit_error = None

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None):
        if self.edit_error:
            raise self.edit_error
        self.edits.append((text, reply_markup))


def make_update(text=None, callback_data=None, chat_type="private", user_id=7):
    query = FakeCallbackQuery(callback_data) if callback_data is not None else None
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="hrithik", first_name="Hrithik"),
        effective_chat=SimpleNamespace(type=chat_type),
        effective_message=FakeMessage(text),
        callback_query=query,
    )


def make_context(args=None, user_data=None, limiter=None):
    return SimpleNamespace(
        args=args or [],
        user_data={} if user_data is None else user_data,
        error=None,
        application=SimpleNamespace(bot_data={"rules": RULES, "limiter": limiter or TokenBucketLimiter()}),
    )


class FakeDB:
    """Stands in for src.database; records every call."""

    def __init__(self):
        self.user = dict(USER)
        self.subscriptions = []
        self.add_result = "added"
        self.calls = []
        self.fail_with = None

    def _record(self, name, *args):
        self.calls.append((name, args))
        if self.fail_with:
            raise self.fail_with

    def get_user(self, telegram_id):
        self._record("get_user", telegram_id)
        return self.user

    def create_user(self, telegram_id, username):
        self._record("create_user", telegram_id, username)
        self.user = {"id": "new-user", "username": username, "telegram_user_id": telegram_id, "status": "active"}
        return self.user

    def set_user_status(self, user_id, status):
        self._record("set_user_status", user_id, status)
        return True

    def delete_user_account(self, user_id):
        self._record("delete_user_account", user_id)
        return True

    def subscribe_user_to_novel(self, user_id, site_key, slug, limit):
        self._record("subscribe", user_id, site_key, slug, limit)
        return self.add_result

    def list_subscriptions(self, user_id):
        self._record("list_subscriptions", user_id)
        return list(self.subscriptions)

    def remove_subscription(self, user_id, sub_id):
        self._record("remove_subscription", user_id, sub_id)
        return any(s["id"] == sub_id for s in self.subscriptions)

    def set_subscription_status(self, user_id, sub_id, status):
        self._record("set_subscription_status", user_id, sub_id, status)
        return any(s["id"] == sub_id for s in self.subscriptions)

    def names(self):
        return [name for name, args in self.calls]


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(bot, "database", fake)
    return fake


def sub(sub_id=UUID1, name="some-novel", chap=157, status="reading", site="https://hivetoons.org/"):
    return {"id": sub_id, "reading_status": status, "novel_name": name, "latest_chap": chap, "web_domain_name": site}


def keyboard_data(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


# ------------------------------------------------------------------ plain helpers

@pytest.mark.parametrize(
    "value, expected",
    [
        (-1, "waiting for first update"),
        (-1.0, "waiting for first update"),
        (None, "waiting for first update"),
        ("junk", "waiting for first update"),
        (0, "chapter 0"),
        (157, "chapter 157"),
        (157.0, "chapter 157"),
        (26.3, "chapter 26.3"),
        ("26.30", "chapter 26.3"),
        (Decimal("12.50"), "chapter 12.5"),
    ],
)
def test_chapter_text(value, expected):
    assert bot.chapter_text(value) == expected


def test_format_subscriptions_numbers_rows_and_shows_host_and_status():
    text = bot.format_subscriptions([
        sub(name="alpha", chap=-1, status="left", site="https://www.mgeko.cc/jumbo/manga/"),
        sub(UUID2, name="beta", chap=5.0),
    ])
    assert text.splitlines() == [
        "1. alpha (mgeko.cc) - waiting for first update - left",
        "2. beta (hivetoons.org) - chapter 5 - reading",
    ]


def test_host_of_handles_missing_site():
    assert bot.host_of(None) == "?"


def test_button_label_is_capped():
    assert len(bot.button_label(sub(name="x" * 500))) == bot.MAX_BUTTON_LABEL_LEN


def test_display_name_prefers_username_then_first_name_then_id():
    assert bot.display_name(SimpleNamespace(username="u", first_name="F", id=1)) == "u"
    assert bot.display_name(SimpleNamespace(username=None, first_name="F", id=1)) == "F"
    assert bot.display_name(SimpleNamespace(username=None, first_name=None, id=1)) == "1"
    assert len(bot.display_name(SimpleNamespace(username="u" * 200, first_name="F", id=1))) == 64


def test_sites_text_lists_only_addable_sites():
    text = bot.sites_text(RULES)
    assert "asurascans.com" in text and "mgeko.cc" in text and "mangadex" not in text


def test_parse_uuid():
    assert bot.parse_uuid(UUID1) == UUID1
    assert bot.parse_uuid(UUID1.upper()) == UUID1
    for bad in ("nope", "", None, "1234", UUID1 + "x"):
        assert bot.parse_uuid(bad) is None


def test_every_callback_data_fits_telegrams_64_byte_limit():
    rows = [sub(UUID1)]
    longest = [
        bot.picker_keyboard("rm", rows),
        bot.status_keyboard(UUID1),
    ]
    for markup in longest:
        for data in keyboard_data(markup):
            assert len(data.encode()) <= 64


def test_message_templates_format_without_missing_keys():
    for key in ("added", "already", "menu_for", "choose_status", "status_changed", "confirm_remove", "removed", "limit"):
        bot.MESSAGES[key].format(slug="s", status="reading", limit=20)


# ------------------------------------------------------------------ process_add

def test_process_add_success(db):
    reply, done = bot.process_add("user-1", "https://hivetoons.org/series/some-novel/chapter-3", RULES)
    assert done is True and "some-novel" in reply
    assert db.calls == [("subscribe", ("user-1", "https://hivetoons.org/", "some-novel", bot.MAX_SUBSCRIPTIONS))]


def test_process_add_already_following(db):
    db.add_result = "already_subscribed"
    reply, done = bot.process_add("user-1", "https://hivetoons.org/series/some-novel", RULES)
    assert done is True and "already following" in reply


def test_process_add_limit_reached(db):
    db.add_result = "limit_reached"
    reply, done = bot.process_add("user-1", "https://hivetoons.org/series/some-novel", RULES)
    assert done is True and str(bot.MAX_SUBSCRIPTIONS) in reply


@pytest.mark.parametrize(
    "text, expected_fragment",
    [
        ("", "link"),
        ("hello there", "does not look like a link"),
        ("https://example.com/series/x", "supported websites"),
        ("https://hivetoons.org/about", "not a novel or chapter page"),
        ("https://hivetoons.org/series/-bad-", "could not read"),
        ("x" * (bot.MAX_INPUT_LEN + 1), "too long"),
    ],
)
def test_process_add_invalid_input_keeps_waiting_and_never_touches_the_database(db, text, expected_fragment):
    reply, done = bot.process_add("user-1", text, RULES)
    assert done is False
    assert expected_fragment in reply
    assert db.calls == []


def test_process_add_unsupported_site_lists_the_supported_ones(db):
    reply, _ = bot.process_add("user-1", "https://example.com/series/x", RULES)
    assert "hivetoons.org" in reply


# ------------------------------------------------------------------ /start

def test_start_registers_a_new_user_and_shows_sites(db):
    db.user = None
    update, context = make_update(), make_context()

    run(bot.cmd_start(update, context))

    assert ("create_user", (7, "hrithik")) in db.calls
    text = update.effective_message.replies[0][0]
    assert "Welcome to ScrollPing" in text and "mgeko.cc" in text and "/add" in text


def test_start_for_an_existing_user_does_not_register_again(db):
    update = make_update()
    run(bot.cmd_start(update, make_context()))
    assert "create_user" not in db.names()
    assert update.effective_message.replies[0][0] == bot.MESSAGES["welcome_back"]


def test_start_resumes_a_paused_user(db):
    db.user = dict(USER, status="paused")
    update = make_update()
    run(bot.cmd_start(update, make_context()))
    assert ("set_user_status", ("user-1", "active")) in db.calls
    assert update.effective_message.replies[0][0] == bot.MESSAGES["resumed"]


def test_help_lists_commands_and_sites(db):
    update = make_update()
    run(bot.cmd_help(update, make_context()))
    text = update.effective_message.replies[0][0]
    assert "/add" in text and "/stop" in text and "hivetoons.org" in text


# ------------------------------------------------------------------ /add and plain text

def test_add_without_a_link_prompts_and_waits(db):
    update, context = make_update(), make_context()
    run(bot.cmd_add(update, context))
    assert context.user_data["awaiting_link"] is True
    assert "copy" in update.effective_message.replies[0][0].lower()


def test_add_with_a_link_adds_immediately(db):
    update, context = make_update(), make_context(args=["https://hivetoons.org/series/some-novel"])
    run(bot.cmd_add(update, context))
    assert "subscribe" in db.names()
    assert context.user_data["awaiting_link"] is False
    assert "now following" in update.effective_message.replies[0][0]


def test_add_with_a_bad_link_keeps_waiting(db):
    update, context = make_update(), make_context(args=["nonsense"])
    run(bot.cmd_add(update, context))
    assert context.user_data["awaiting_link"] is True
    assert "subscribe" not in db.names()


def test_add_requires_registration(db):
    db.user = None
    update = make_update()
    run(bot.cmd_add(update, make_context()))
    assert update.effective_message.replies[0][0] == bot.MESSAGES["need_start"]


def test_text_without_add_gets_a_hint_and_no_database_work(db):
    update = make_update(text="https://hivetoons.org/series/some-novel")
    run(bot.on_text(update, make_context()))
    assert update.effective_message.replies[0][0] == bot.MESSAGES["add_usage_hint"]
    assert db.calls == []


def test_text_after_add_is_treated_as_the_link_and_clears_the_wait(db):
    update = make_update(text="https://hivetoons.org/series/some-novel")
    context = make_context(user_data={"awaiting_link": True})
    run(bot.on_text(update, context))
    assert "subscribe" in db.names()
    assert context.user_data["awaiting_link"] is False


def test_bad_text_after_add_keeps_waiting_for_a_retry(db):
    update = make_update(text="oops")
    context = make_context(user_data={"awaiting_link": True})
    run(bot.on_text(update, context))
    assert context.user_data["awaiting_link"] is True


def test_cancel_clears_a_pending_add(db):
    context = make_context(user_data={"awaiting_link": True})
    update = make_update()
    run(bot.cmd_cancel(update, context))
    assert "awaiting_link" not in context.user_data
    assert update.effective_message.replies[0][0] == bot.MESSAGES["cancelled"]


def test_cancel_with_nothing_pending(db):
    update = make_update()
    run(bot.cmd_cancel(update, make_context()))
    assert update.effective_message.replies[0][0] == bot.MESSAGES["nothing_to_cancel"]


def test_non_text_and_unknown_commands_get_short_replies(db):
    update = make_update()
    run(bot.on_non_text(update, make_context()))
    run(bot.on_unknown_command(update, make_context()))
    assert [r[0] for r in update.effective_message.replies] == [bot.MESSAGES["text_only"], bot.MESSAGES["unknown_command"]]


# ------------------------------------------------------------------ /list /remove /status /stop

def test_list_shows_subscriptions_with_one_button_each(db):
    db.subscriptions = [sub(UUID1), sub(UUID2, name="other")]
    update = make_update()
    run(bot.cmd_list(update, make_context()))
    text, markup = update.effective_message.replies[0]
    assert "1. some-novel" in text and "2. other" in text
    assert keyboard_data(markup) == [f"m:{UUID1}", f"m:{UUID2}"]


def test_remove_and_status_pickers_use_their_own_prefixes(db):
    db.subscriptions = [sub(UUID1)]
    for command, prefix in ((bot.cmd_remove, "rm"), (bot.cmd_status, "st")):
        update = make_update()
        run(command(update, make_context()))
        assert keyboard_data(update.effective_message.replies[0][1]) == [f"{prefix}:{UUID1}"]


def test_empty_list_says_so(db):
    update = make_update()
    run(bot.cmd_list(update, make_context()))
    assert update.effective_message.replies == [(bot.MESSAGES["no_subscriptions"], None)]


def test_list_requires_registration(db):
    db.user = None
    update = make_update()
    run(bot.cmd_list(update, make_context()))
    assert update.effective_message.replies[0][0] == bot.MESSAGES["need_start"]


def test_stop_shows_the_three_choices(db):
    update = make_update()
    run(bot.cmd_stop(update, make_context()))
    assert keyboard_data(update.effective_message.replies[0][1]) == ["stop:pause", "stop:delete", "no"]


# ------------------------------------------------------------------ callbacks

def press(db, data, **kwargs):
    update = make_update(callback_data=data, **kwargs)
    run(bot.on_callback(update, make_context()))
    return update.callback_query


def test_callback_always_answers_the_query_first(db):
    query = press(db, "no")
    assert query.answers == [None]


def test_cancel_button(db):
    query = press(db, "no")
    assert query.edits[0][0] == bot.MESSAGES["unchanged"]


def test_menu_for_a_subscription(db):
    db.subscriptions = [sub(UUID1)]
    query = press(db, f"m:{UUID1}")
    text, markup = query.edits[0]
    assert "some-novel" in text and "reading" in text
    assert keyboard_data(markup) == [f"st:{UUID1}", f"rm:{UUID1}", "no"]


def test_status_picker_offers_all_four_statuses(db):
    db.subscriptions = [sub(UUID1)]
    query = press(db, f"st:{UUID1}")
    assert keyboard_data(query.edits[0][1]) == [f"ss:{UUID1}:{i}" for i in range(4)] + ["no"]


@pytest.mark.parametrize("index, status", list(enumerate(bot.STATUS_CHOICES)))
def test_choosing_a_status_updates_it(db, index, status):
    db.subscriptions = [sub(UUID1)]
    query = press(db, f"ss:{UUID1}:{index}")
    assert ("set_subscription_status", ("user-1", UUID1, status)) in db.calls
    assert status in query.edits[0][0]


@pytest.mark.parametrize("suffix", ["9", "-1x", "abc", ""])
def test_a_bad_status_index_changes_nothing(db, suffix):
    db.subscriptions = [sub(UUID1)]
    query = press(db, f"ss:{UUID1}:{suffix}")
    assert "set_subscription_status" not in db.names()
    assert query.edits[0][0] == bot.MESSAGES["not_found"]


def test_remove_asks_for_confirmation_first(db):
    db.subscriptions = [sub(UUID1)]
    query = press(db, f"rm:{UUID1}")
    assert "remove_subscription" not in db.names()
    assert keyboard_data(query.edits[0][1]) == [f"ry:{UUID1}", "no"]


def test_confirmed_remove_deletes_with_the_users_own_id(db):
    db.subscriptions = [sub(UUID1)]
    query = press(db, f"ry:{UUID1}")
    assert ("remove_subscription", ("user-1", UUID1)) in db.calls
    assert "Removed" in query.edits[0][0]


def test_someone_elses_subscription_id_is_not_found_and_nothing_is_written(db):
    db.subscriptions = [sub(UUID1)]                       # the user only owns UUID1
    for data in (f"ry:{UUID2}", f"ss:{UUID2}:0", f"rm:{UUID2}", f"m:{UUID2}", f"st:{UUID2}"):
        query = press(db, data)
        assert query.edits[0][0] == bot.MESSAGES["not_found"]
    assert "remove_subscription" not in db.names()
    assert "set_subscription_status" not in db.names()


@pytest.mark.parametrize("data", ["m:not-a-uuid", "ry:", "ry", "zz:" + UUID1, "", "m:" + "a" * 500])
def test_garbage_callback_data_is_handled_without_database_writes(db, data):
    db.subscriptions = [sub(UUID1)]
    query = press(db, data)
    assert query.edits[0][0] == bot.MESSAGES["not_found"]
    assert "remove_subscription" not in db.names()


def test_callbacks_from_unregistered_users_are_refused(db):
    db.user = None
    query = press(db, f"ry:{UUID1}")
    assert query.edits[0][0] == bot.MESSAGES["need_start"]


def test_stop_pause(db):
    query = press(db, "stop:pause")
    assert ("set_user_status", ("user-1", "paused")) in db.calls
    assert query.edits[0][0] == bot.MESSAGES["stop_paused"]


def test_stop_delete_needs_a_second_confirmation(db):
    query = press(db, "stop:delete")
    assert "delete_user_account" not in db.names()
    assert keyboard_data(query.edits[0][1]) == ["stop:deleteyes", "no"]


def test_stop_delete_confirmed_deletes_the_account(db):
    query = press(db, "stop:deleteyes")
    assert ("delete_user_account", ("user-1",)) in db.calls
    assert query.edits[0][0] == bot.MESSAGES["stop_deleted"]


def test_unknown_stop_action_does_nothing(db):
    query = press(db, "stop:explode")
    assert "delete_user_account" not in db.names() and "set_user_status" not in db.names()
    assert query.edits[0][0] == bot.MESSAGES["not_found"]


def test_message_not_modified_errors_are_ignored(db):
    query = FakeCallbackQuery("no")
    query.edit_error = BadRequest("Message is not modified")
    update = make_update()
    update.callback_query = query
    run(bot.on_callback(update, make_context()))          # must not raise


def test_other_edit_errors_still_raise(db):
    query = FakeCallbackQuery("no")
    query.edit_error = BadRequest("chat not found")
    update = make_update()
    update.callback_query = query
    with pytest.raises(BadRequest):
        run(bot.on_callback(update, make_context()))


# ------------------------------------------------------------------ rate gate

class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def gate(update, limiter):
    return run(bot.rate_gate(update, make_context(limiter=limiter)))


def test_gate_lets_normal_traffic_through():
    gate(make_update(), TokenBucketLimiter(capacity=2, clock=Clock()))     # no exception


def test_gate_drops_group_chats_silently():
    update = make_update(chat_type="group")
    with pytest.raises(ApplicationHandlerStop):
        gate(update, TokenBucketLimiter())
    assert update.effective_message.replies == []


def test_gate_drops_updates_without_a_user():
    update = make_update()
    update.effective_user = None
    with pytest.raises(ApplicationHandlerStop):
        gate(update, TokenBucketLimiter())


def test_gate_warns_once_then_drops_silently_when_flooded():
    limiter = TokenBucketLimiter(capacity=2, refill_per_second=0, clock=Clock())
    update = make_update()
    gate(update, limiter)
    gate(update, limiter)

    for _ in range(4):
        with pytest.raises(ApplicationHandlerStop):
            gate(update, limiter)

    assert [r[0] for r in update.effective_message.replies] == [bot.MESSAGES["slow_down"]]


def test_gate_answers_button_presses_with_the_warning_instead_of_a_message():
    limiter = TokenBucketLimiter(capacity=1, refill_per_second=0, clock=Clock())
    first = make_update(callback_data="no")
    gate(first, limiter)

    second = make_update(callback_data="no")
    with pytest.raises(ApplicationHandlerStop):
        gate(second, limiter)
    assert second.callback_query.answers == [bot.MESSAGES["slow_down"]]
    assert second.effective_message.replies == []


def test_one_flooding_user_does_not_block_another():
    limiter = TokenBucketLimiter(capacity=1, refill_per_second=0, clock=Clock())
    gate(make_update(user_id=1), limiter)
    with pytest.raises(ApplicationHandlerStop):
        gate(make_update(user_id=1), limiter)
    gate(make_update(user_id=2), limiter)                  # still fine


# ------------------------------------------------------------------ errors and wiring

def test_error_handler_replies_with_a_generic_message(monkeypatch):
    from telegram import Update
    update = make_update()
    monkeypatch.setattr(bot, "Update", type(update))      # make the isinstance check accept the fake
    context = make_context()
    context.error = RuntimeError("secret internals")

    run(bot.on_error(update, context))

    reply = update.effective_message.replies[0][0]
    assert reply == bot.MESSAGES["generic_error"] and "secret" not in reply


def test_error_handler_survives_updates_it_cannot_reply_to():
    context = make_context()
    context.error = RuntimeError("boom")
    run(bot.on_error(None, context))                       # must not raise


def test_a_database_failure_inside_a_handler_propagates_to_the_error_handler(db):
    db.fail_with = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        run(bot.cmd_list(make_update(), make_context()))


def test_application_wires_the_gate_first_and_all_commands():
    application = bot.build_application("123:ABC", RULES)
    assert -1 in application.handlers
    commands = set()
    for handler in application.handlers[0]:
        commands |= set(getattr(handler, "commands", []))
    assert commands == {"start", "help", "add", "list", "remove", "status", "stop", "cancel"}
    assert isinstance(application.bot_data["limiter"], TokenBucketLimiter)
    assert application.bot_data["rules"] is RULES


def test_the_published_command_menu_matches_real_commands():
    menu = {name for name, description in bot.BOT_COMMANDS}
    assert menu <= {"start", "help", "add", "list", "remove", "status", "stop", "cancel"}


# ------------------------------------------------------------------ logging must never leak the token

def test_configure_logging_silences_loggers_that_print_the_bot_token():
    import logging
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.NOTSET)

    bot.configure_logging()

    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


def test_an_httpx_info_line_with_the_token_is_not_emitted_after_configure_logging(caplog):
    import logging
    bot.configure_logging()
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info("HTTP Request: POST https://api.telegram.org/bot123:SECRET/getUpdates")
    assert "SECRET" not in caplog.text