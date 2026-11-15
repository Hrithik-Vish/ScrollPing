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
WEB_ASURA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WEB_HIVE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
WEB_OTHER = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
USER = {"id": "user-1", "username": "h", "telegram_user_id": 7, "status": "active"}


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ fakes

class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.text_html = text
        self.replies = []
        self.deleted = False
        self.delete_error = None

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))

    async def delete(self):
        if self.delete_error:
            raise self.delete_error
        self.deleted = True


class FakeCallbackQuery:
    def __init__(self, data):
        self.data = data
        self.answers = []
        self.edits = []
        self.edit_error = None
        self.message = FakeMessage()

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
        self.summary = {"id": "novel-1", "web_id": WEB_HIVE, "novel_name": "some-novel", "display_title": None}
        self.summary_error = None
        self.tiers = {}                                   # (user_id, web_id) -> tier
        self.website_domains = {WEB_ASURA: "https://asurascans.com/", WEB_HIVE: "https://hivetoons.org/"}
        self.timing_changes = True

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

    def get_novel_summary(self, site_key, slug):
        self._record("get_novel_summary", site_key, slug)
        if self.summary_error:
            raise self.summary_error
        return self.summary

    def get_website_domain(self, web_id):
        self._record("get_website_domain", web_id)
        return self.website_domains.get(web_id)

    def get_or_create_website(self, domain):
        self._record("get_or_create_website", domain)
        return next((w for w, d in self.website_domains.items() if d == domain), WEB_OTHER)

    def get_user_tier(self, user_id, web_id):
        self._record("get_user_tier", user_id, web_id)
        return self.tiers.get((user_id, web_id))

    def list_user_tiers(self, user_id):
        self._record("list_user_tiers", user_id)
        return {web: tier for (user, web), tier in self.tiers.items() if user == user_id}

    def set_user_tier(self, user_id, web_id, tier):
        self._record("set_user_tier", user_id, web_id, tier)
        self.tiers[(user_id, web_id)] = tier
        return True

    def set_alert_timing(self, user_id, sub_id, timing):
        self._record("set_alert_timing", user_id, sub_id, timing)
        found = [s for s in self.subscriptions if s["id"] == sub_id]
        for s_ in found:
            s_["alert_timing"] = timing
        return bool(found) and self.timing_changes

    def names(self):
        return [name for name, args in self.calls]


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(bot, "database", fake)
    return fake


WEB_OF_SITE = {"https://asurascans.com/": WEB_ASURA, "https://hivetoons.org/": WEB_HIVE}


def sub(sub_id=UUID1, name="some-novel", chap=157, status="reading", site="https://hivetoons.org/",
        display_title=None, alert_timing=None, web_id=None):
    return {"id": sub_id, "reading_status": status, "novel_name": name, "latest_chap": chap, "web_domain_name": site,
            "display_title": display_title, "alert_timing": alert_timing, "web_id": web_id or WEB_OF_SITE.get(site, WEB_OTHER)}


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


def test_host_of_handles_missing_site():
    assert bot.host_of(None) == "?"


def test_button_label_is_capped():
    assert len(bot.button_label(sub(name="x" * 500))) == bot.MAX_BUTTON_LABEL_LEN


def test_button_label_shows_the_title_not_the_slug():
    assert bot.button_label(sub(name="killer-pietro", display_title="Killer Pietro")) == "Killer Pietro"
    assert bot.button_label(sub(name="killer-pietro")) == "Killer Pietro"


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
    for key in ("added", "already", "novel_menu", "choose_status", "status_changed", "confirm_remove", "removed", "limit"):
        bot.MESSAGES[key].format(title="t", site="s", slug="s", status="reading", limit=20, chapter="c")


# ------------------------------------------------------------------ process_add

def test_process_add_success(db):
    outcome = bot.process_add("user-1", "https://hivetoons.org/series/some-novel/chapter-3", RULES)
    assert outcome.done is True and "Some Novel" in outcome.text and "Hive Toons" in outcome.text
    assert db.calls[0] == ("subscribe", ("user-1", "https://hivetoons.org/", "some-novel", bot.MAX_SUBSCRIPTIONS))
    assert outcome.tier_web_id is None                      # hivetoons has no early access: no tier question


def test_process_add_already_following(db):
    db.add_result = "already_subscribed"
    outcome = bot.process_add("user-1", "https://hivetoons.org/series/some-novel", RULES)
    assert outcome.done is True and "already following" in outcome.text and outcome.tier_web_id is None


def test_process_add_limit_reached(db):
    db.add_result = "limit_reached"
    outcome = bot.process_add("user-1", "https://hivetoons.org/series/some-novel", RULES)
    assert outcome.done is True and str(bot.MAX_SUBSCRIPTIONS) in outcome.text
    assert "get_novel_summary" not in db.names()            # nothing was followed, nothing to look up


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
    outcome = bot.process_add("user-1", text, RULES)
    assert outcome.done is False
    assert expected_fragment in outcome.text
    assert db.calls == []


def test_process_add_unsupported_site_lists_the_supported_ones(db):
    outcome = bot.process_add("user-1", "https://example.com/series/x", RULES)
    assert "hivetoons.org" in outcome.text


# ------------------------------------------------------------------ /start

def test_start_registers_a_new_user_and_shows_sites(db):
    db.user = None
    update, context = make_update(), make_context()

    run(bot.cmd_start(update, context))

    assert ("create_user", (7, "hrithik")) in db.calls
    text = update.effective_message.replies[0][0]
    assert "Welcome to ScrollPing" in text and "mgeko" in text and "Asura Scans" in text and "/add" in text


def test_start_for_an_existing_user_does_not_register_again(db):
    update = make_update()
    run(bot.cmd_start(update, make_context()))
    assert "create_user" not in db.names()
    assert update.effective_message.replies[0][0] == bot.fmt("welcome_back", count="0 novels")


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

def test_list_opens_the_site_hub_with_one_button_per_site(db):
    db.subscriptions = [sub(UUID1), sub(UUID2, name="other")]
    update = make_update()
    run(bot.cmd_list(update, make_context()))
    text, markup = update.effective_message.replies[0]
    assert "2 novels across 1 site" in text
    assert keyboard_data(markup) == [f"site:{WEB_HIVE}", "close"]


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
    assert "Some Novel" in text and "reading" in text
    assert keyboard_data(markup) == [f"st:{UUID1}", f"rm:{UUID1}", f"site:{WEB_HIVE}"]


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
    assert commands == {"start", "help", "add", "list", "settings", "remove", "status", "stop", "cancel"}
    assert isinstance(application.bot_data["limiter"], TokenBucketLimiter)
    assert application.bot_data["rules"] is RULES


def test_the_published_command_menu_matches_real_commands():
    menu = {name for name, description in bot.BOT_COMMANDS}
    assert menu <= {"start", "help", "add", "list", "settings", "remove", "status", "stop", "cancel"}
    assert "settings" in menu


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


# ------------------------------------------------------------------ hosting: health endpoint wiring

def test_post_init_publishes_the_menu_and_marks_the_bot_running():
    from src.health import STATUS
    STATUS.set_running(False)
    calls = []

    class FakeBot:
        async def set_my_commands(self, commands):
            calls.append(commands)

    run(bot.post_init(SimpleNamespace(bot=FakeBot())))

    assert STATUS.running is True
    assert [c.command for c in calls[0]] == [name for name, description in bot.BOT_COMMANDS]


def test_post_init_survives_a_failing_command_menu_and_still_marks_running():
    from src.health import STATUS
    STATUS.set_running(False)

    class BrokenBot:
        async def set_my_commands(self, commands):
            raise RuntimeError("telegram hiccup")

    run(bot.post_init(SimpleNamespace(bot=BrokenBot())))        # must not raise
    assert STATUS.running is True


def test_post_shutdown_marks_the_bot_not_running():
    from src.health import STATUS
    STATUS.set_running(True)
    run(bot.post_shutdown(SimpleNamespace()))
    assert STATUS.running is False


def test_application_registers_the_lifecycle_hooks():
    application = bot.build_application("123:ABC", RULES)
    assert application.post_init is bot.post_init
    assert application.post_shutdown is bot.post_shutdown


@pytest.mark.parametrize("raw, expected", [(None, None), ("", None), ("  ", None), ("7860", 7860), (" 8080 ", 8080)])
def test_health_port_from_env(monkeypatch, raw, expected):
    monkeypatch.delenv("PORT", raising=False)
    if raw is None:
        monkeypatch.delenv("HEALTH_PORT", raising=False)
    else:
        monkeypatch.setenv("HEALTH_PORT", raw)
    assert bot.health_port_from_env() == expected


def test_health_port_falls_back_to_the_port_variable_hosts_like_render_set(monkeypatch):
    monkeypatch.delenv("HEALTH_PORT", raising=False)
    monkeypatch.setenv("PORT", "10000")
    assert bot.health_port_from_env() == 10000


def test_health_port_wins_over_port(monkeypatch):
    monkeypatch.setenv("HEALTH_PORT", "7860")
    monkeypatch.setenv("PORT", "10000")
    assert bot.health_port_from_env() == 7860


def test_a_blank_health_port_still_falls_back_to_port(monkeypatch):
    monkeypatch.setenv("HEALTH_PORT", "  ")
    monkeypatch.setenv("PORT", "10000")
    assert bot.health_port_from_env() == 10000


def test_a_bad_port_variable_is_reported_by_name(monkeypatch):
    monkeypatch.delenv("HEALTH_PORT", raising=False)
    monkeypatch.setenv("PORT", "abc")
    with pytest.raises(ValueError, match="PORT must be a number"):
        bot.health_port_from_env()


@pytest.mark.parametrize("raw", ["abc", "78.5", "0", "-1", "70000"])
def test_health_port_from_env_rejects_bad_values_with_a_clear_message(monkeypatch, raw):
    monkeypatch.setenv("HEALTH_PORT", raw)
    with pytest.raises(ValueError, match="HEALTH_PORT"):
        bot.health_port_from_env()


class FakeApplication:
    def __init__(self):
        self.polled = None

    def run_polling(self, **kwargs):
        self.polled = kwargs


def patch_main(monkeypatch):
    app = FakeApplication()
    started = []
    monkeypatch.setattr(bot, "configure_logging", lambda: None)
    monkeypatch.setattr(bot, "get_token", lambda: "123:ABC")
    monkeypatch.setattr(bot, "load_rules", lambda: RULES)
    monkeypatch.setattr(bot, "build_application", lambda token, rules: app)
    monkeypatch.setattr(bot, "start_health_server", lambda status, port: started.append(port))
    return app, started


def test_main_opens_no_port_when_no_port_variable_is_set(monkeypatch):
    monkeypatch.delenv("HEALTH_PORT", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    app, started = patch_main(monkeypatch)
    bot.main()
    assert started == [] and app.polled["allowed_updates"] == ["message", "callback_query"]


def test_main_starts_the_health_server_before_polling(monkeypatch):
    monkeypatch.setenv("HEALTH_PORT", "7860")
    app, started = patch_main(monkeypatch)
    bot.main()
    assert started == [7860] and app.polled is not None


def test_main_listens_on_the_port_render_provides(monkeypatch):
    monkeypatch.delenv("HEALTH_PORT", raising=False)
    monkeypatch.setenv("PORT", "10000")
    app, started = patch_main(monkeypatch)
    bot.main()
    assert started == [10000]


def test_main_refuses_to_start_with_a_bad_health_port(monkeypatch):
    monkeypatch.setenv("HEALTH_PORT", "nope")
    app, started = patch_main(monkeypatch)
    with pytest.raises(ValueError):
        bot.main()
    assert app.polled is None


# ------------------------------------------------------------------ the 50-novel cap and long /list output

def test_the_cap_is_fifty_and_reaches_the_database_call(db):
    assert bot.MAX_SUBSCRIPTIONS == 50
    bot.process_add("user-1", "https://hivetoons.org/series/some-novel", RULES)
    assert db.calls[0][1][-1] == 50


def test_limit_message_names_the_current_cap(db):
    db.add_result = "limit_reached"
    outcome = bot.process_add("user-1", "https://hivetoons.org/series/some-novel", RULES)
    assert "50" in outcome.text and outcome.done is True


def test_split_text_keeps_short_text_whole():
    assert bot.split_text("a\nb\nc") == ["a\nb\nc"]


def test_split_text_breaks_between_lines_and_loses_nothing():
    lines = [f"{i}. " + "x" * 90 for i in range(100)]
    chunks = bot.split_text("\n".join(lines), limit=1000)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)
    assert "\n".join(chunks).split("\n") == lines          # every line, in order, unbroken


def test_split_text_cuts_a_single_oversized_line():
    chunks = bot.split_text("start\n" + "y" * 250 + "\nend", limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks).replace("\n", "") == "start" + "y" * 250 + "end"


def test_a_site_with_fifty_long_names_fits_telegrams_limit_and_keeps_every_button(db):
    db.subscriptions = [
        sub(f"{i:08d}-1111-4111-8111-111111111111", name=("novel-" + str(i) + "-") + "a" * 130, chap=i)
        for i in range(50)
    ]
    query = press(db, f"site:{WEB_HIVE}")

    assert len(query.edits) == 1 and query.message.replies           # the text really is too long for one message
    texts = [query.edits[0][0]] + [text for text, markup in query.message.replies]
    assert all(len(text) <= 4096 for text in texts)
    assert query.edits[0][1] is None                                  # the first chunk replaces the tapped message, no buttons
    assert all(markup is None for text, markup in query.message.replies[:-1])      # buttons only on the last message
    buttons = keyboard_data(query.message.replies[-1][1])
    assert len(buttons) == 51 and buttons[-1] == "hub"                # 50 novels + Back
    joined = "\n".join(texts)
    assert "Novel 0 " in joined and "Novel 49 " in joined


def test_remove_and_status_pickers_still_send_one_message(db):
    db.subscriptions = [sub(f"{i:08d}-1111-4111-8111-111111111111", name=f"n{i}") for i in range(50)]
    update = make_update()
    run(bot.cmd_remove(update, make_context()))
    assert len(update.effective_message.replies) == 1

# =====================================================================================================
# Phase 6: HTML escaping, the /list hub, site and novel pages, alert timing, /settings, the tier question
# =====================================================================================================

import string

from telegram.constants import ParseMode

HOSTILE = '<i>x</i> & "q"'
ASURA_URL = "https://asurascans.com/"
HIVE_URL = "https://hivetoons.org/"
KUN_URL = "https://www.kunmanga.online/"
ASURA_LINK = "https://asurascans.com/comics/killer-pietro-f212c582/chapter/156"


def all_buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def press_with_text(db, data, message_text):
    update = make_update(callback_data=data)
    update.callback_query.message.text_html = message_text
    run(bot.on_callback(update, make_context()))
    return update.callback_query


# ------------------------------------------------------------------ escaping

def template_fields(template):
    return [name for _, name, _, _ in string.Formatter().parse(template) if name]


@pytest.mark.parametrize("key", [k for k, v in bot.MESSAGES.items() if template_fields(v)])
def test_fmt_escapes_every_value_of_every_template(key):
    values = {name: HOSTILE for name in template_fields(bot.MESSAGES[key])}
    out = bot.fmt(key, **values)
    assert "<i>" not in out and "</i>" not in out and "& " not in out.replace("&amp; ", "")
    assert "&lt;i&gt;x&lt;/i&gt; &amp;" in out


def test_esc_only_touches_the_three_html_characters():
    assert bot.esc('<b>Tom & "Jerry\'s"</b>') == "&lt;b&gt;Tom &amp; \"Jerry's\"&lt;/b&gt;"
    assert bot.esc(12) == "12" and bot.esc(None) == "None"


def test_the_application_sends_everything_in_html_mode():
    application = bot.build_application("123:ABC", RULES)
    assert application.bot.defaults.parse_mode == ParseMode.HTML


def test_hostile_titles_never_become_markup_in_any_view():
    row = sub(display_title=HOSTILE, alert_timing="early", site=ASURA_URL)
    group = bot.group_by_site([row], RULES)[0]
    views = [
        bot.site_view(group)[0],
        bot.novel_view(row, RULES)[0],
        bot.timing_view(row, RULES, "standard")[0],
        bot.fmt("choose_status", title=bot.title_of(row)),
        bot.fmt("confirm_remove", title=bot.title_of(row)),
        bot.fmt("removed", title=bot.title_of(row)),
        bot.fmt("status_changed", title=bot.title_of(row), status="reading"),
    ]
    for text in views:
        assert "<i>" not in text and "&lt;i&gt;" in text


def test_a_hostile_site_name_is_escaped_in_the_hub_and_site_page():
    rules = {ASURA_URL: dict(RULES[ASURA_URL], display_name=HOSTILE)}
    row = sub(site=ASURA_URL)
    assert "<i>" not in bot.hub_view([row], rules)[0]
    assert "<i>" not in bot.site_view(bot.group_by_site([row], rules)[0])[0]
    assert "<i>" not in bot.sites_inline({ASURA_URL: dict(rules[ASURA_URL], novel_link_patterns=["x"], base_url="https://a.b")})


def test_hostile_values_are_escaped_in_the_add_confirmation(db):
    db.summary = {"id": "n", "web_id": WEB_HIVE, "novel_name": "x", "display_title": HOSTILE}
    outcome = bot.process_add("user-1", "https://hivetoons.org/series/x", RULES)
    assert "<i>" not in outcome.text and "&lt;i&gt;" in outcome.text


# ------------------------------------------------------------------ small helpers

@pytest.mark.parametrize(
    "value, expected",
    [(-1, "waiting for first update"), (None, "waiting for first update"), ("junk", "waiting for first update"),
     (0, "Ch. 0"), (157, "Ch. 157"), (157.0, "Ch. 157"), (26.3, "Ch. 26.3"), (Decimal("12.50"), "Ch. 12.5"),
     (1000000, "Ch. 1000000")],
)
def test_chapter_label(value, expected):
    assert bot.chapter_label(value) == expected


def test_plural():
    assert bot.plural(1, "novel") == "1 novel" and bot.plural(0, "novel") == "0 novels" and bot.plural(2, "site") == "2 sites"


def test_site_name_prefers_the_display_name_then_the_host():
    assert bot.site_name(RULES, ASURA_URL) == "Asura Scans"
    assert bot.site_name({}, "https://www.example.org/x") == "example.org"
    assert bot.site_name(RULES, None) == "?"


def test_only_asura_is_timing_capable():
    assert [d for d in RULES if bot.timing_capable(RULES, d)] == [ASURA_URL]
    assert not bot.timing_capable(RULES, "https://unknown.example/")


def test_tier_labels_come_from_the_rules_with_a_fallback():
    assert bot.tier_label(RULES, ASURA_URL, "standard") == "Free reader"
    assert bot.tier_label(RULES, ASURA_URL, "member") == "Asura+ member"
    assert bot.tier_label(RULES, HIVE_URL, "member") == "Member"


def test_sites_inline_lists_display_names():
    inline = bot.sites_inline(RULES)
    assert "Asura Scans" in inline and "Hive Toons" in inline and "mgeko" in inline and "MangaDex" not in inline


def test_without_question_strips_only_the_question():
    confirmation = "Done! You are now following <b>X</b> (Asura Scans)."
    assert bot.without_question(confirmation + "\n\n" + bot.MESSAGES["tier_question"]) == confirmation
    assert bot.without_question(confirmation) == confirmation
    assert bot.without_question(None) == ""


# ------------------------------------------------------------------ the /list hub

def test_hub_counts_novels_and_sites_and_lists_sites_alphabetically():
    rows = [sub(UUID1, site=HIVE_URL), sub(UUID2, name="b", site=ASURA_URL), sub("33333333-3333-4333-8333-333333333333", name="c", site=ASURA_URL)]
    text, markup = bot.hub_view(rows, RULES)
    assert "3 novels across 2 sites" in text
    labels = [b.text for b in all_buttons(markup)]
    assert labels == ["Asura Scans (2)", "Hive Toons (1)", "Close"]
    assert [b.callback_data for b in all_buttons(markup)] == [f"site:{WEB_ASURA}", f"site:{WEB_HIVE}", "close"]


def test_hub_singular_wording():
    text, _ = bot.hub_view([sub()], RULES)
    assert "1 novel across 1 site" in text


def test_group_by_site_sorts_novels_by_title_then_id():
    rows = [sub(UUID2, name="b-novel"), sub(UUID1, name="a-novel", display_title="Zed Title"), sub("33333333-3333-4333-8333-333333333333", name="c-novel")]
    [group] = bot.group_by_site(rows, RULES)
    assert [bot.title_of(r) for r in group["rows"]] == ["B Novel", "C Novel", "Zed Title"]


# ------------------------------------------------------------------ the site page

def test_site_page_lists_title_chapter_and_a_status_tag_only_when_not_reading():
    rows = [sub(UUID1, name="alpha", chap=-1, status="left"), sub(UUID2, name="beta", chap=5.0, display_title="Beta Prime")]
    text, markup = bot.site_view(bot.group_by_site(rows, RULES)[0])
    assert "<b>Hive Toons</b>" in text and "2 novels" in text
    assert "\u2022 Alpha \u2014 waiting for first update (left)" in text
    assert "\u2022 Beta Prime \u2014 Ch. 5\n" in text + "\n" and "(reading)" not in text
    assert [b.callback_data for b in all_buttons(markup)] == [f"nov:{UUID1}", f"nov:{UUID2}", "hub"]
    assert [b.text for b in all_buttons(markup)] == ["Alpha", "Beta Prime", "\u00ab Back"]


def test_tapping_a_site_shows_only_that_sites_novels(db):
    db.subscriptions = [sub(UUID1, name="hive-one"), sub(UUID2, name="asura-one", site=ASURA_URL)]
    query = press(db, f"site:{WEB_ASURA}")
    text, markup = query.edits[0]
    assert "Asura One" in text and "Hive One" not in text
    assert keyboard_data(markup) == [f"nov:{UUID2}", "hub"]


def test_a_site_the_user_does_not_follow_anything_on_is_not_found(db):
    db.subscriptions = [sub(UUID1)]
    assert press(db, f"site:{WEB_ASURA}").edits[0][0] == bot.MESSAGES["not_found"]


@pytest.mark.parametrize("data", ["site", "site:", "site:nope", f"site:{WEB_HIVE}x"])
def test_malformed_site_callbacks_are_not_found(db, data):
    db.subscriptions = [sub(UUID1)]
    assert press(db, data).edits[0][0] == bot.MESSAGES["not_found"]


def test_hub_button_returns_to_the_hub(db):
    db.subscriptions = [sub(UUID1), sub(UUID2, site=ASURA_URL)]
    query = press(db, "hub")
    assert "2 novels across 2 sites" in query.edits[0][0]
    assert keyboard_data(query.edits[0][1]) == [f"site:{WEB_ASURA}", f"site:{WEB_HIVE}", "close"]


def test_hub_button_with_nothing_followed_says_so(db):
    assert press(db, "hub").edits[0][0] == bot.MESSAGES["no_subscriptions"]


def test_close_deletes_the_message_and_falls_back_to_an_edit(db):
    update = make_update(callback_data="close")
    run(bot.on_callback(update, make_context()))
    assert update.callback_query.message.deleted is True and update.callback_query.edits == []

    update = make_update(callback_data="close")
    update.callback_query.message.delete_error = BadRequest("Message can't be deleted")
    run(bot.on_callback(update, make_context()))
    assert update.callback_query.edits[0][0] == bot.MESSAGES["closed"]


# ------------------------------------------------------------------ the novel page

def test_novel_page_for_an_early_access_site_offers_alert_timing(db):
    db.subscriptions = [sub(UUID1, name="killer-pietro", site=ASURA_URL, chap=156)]
    text, markup = press(db, f"nov:{UUID1}").edits[0]
    assert "<b>Killer Pietro</b>" in text and "Asura Scans" in text and "Ch. 156" in text and "Status: reading" in text
    assert keyboard_data(markup) == [f"st:{UUID1}", f"tim:{UUID1}", f"rm:{UUID1}", f"site:{WEB_ASURA}"]


def test_novel_page_for_a_normal_site_has_no_alert_timing(db):
    db.subscriptions = [sub(UUID1)]
    assert not any(d.startswith("tim:") for d in keyboard_data(press(db, f"nov:{UUID1}").edits[0][1]))


def test_novel_page_for_a_novel_not_scraped_yet_explains_the_wait(db):
    db.subscriptions = [sub(UUID1, chap=-1)]
    text = press(db, f"nov:{UUID1}").edits[0][0]
    assert "waiting for first update" in text and bot.MESSAGES["waiting_note"] in text


def test_the_old_m_buttons_in_earlier_messages_still_work(db):
    db.subscriptions = [sub(UUID1)]
    assert press(db, f"m:{UUID1}").edits[0] == press(db, f"nov:{UUID1}").edits[0]


def test_another_users_subscription_never_opens(db):
    db.subscriptions = [sub(UUID1)]
    for action in ("nov", "tim", "tset", "st", "rm"):
        suffix = ":d" if action == "tset" else ""
        assert press(db, f"{action}:{UUID2}{suffix}").edits[0][0] == bot.MESSAGES["not_found"]
    assert "set_alert_timing" not in db.names()


# ------------------------------------------------------------------ alert timing

def test_timing_page_shows_the_current_setting_with_the_tier_label(db):
    db.subscriptions = [sub(UUID1, site=ASURA_URL)]
    db.tiers[("user-1", WEB_ASURA)] = "member"
    text, markup = press(db, f"tim:{UUID1}").edits[0]
    assert "Follow my site setting (Asura+ member)" in text
    assert keyboard_data(markup) == [f"tset:{UUID1}:d", f"tset:{UUID1}:e", f"tset:{UUID1}:w", f"nov:{UUID1}"]
    assert [b.text for b in all_buttons(markup)][0].startswith("\u2713 ")            # the current choice is ticked


def test_timing_page_defaults_to_the_free_reader_label_when_no_tier_was_chosen(db):
    db.subscriptions = [sub(UUID1, site=ASURA_URL)]
    assert "Follow my site setting (Free reader)" in press(db, f"tim:{UUID1}").edits[0][0]


@pytest.mark.parametrize("current, label", [("early", "Always early"), ("when_free", "Only when free")])
def test_timing_page_names_an_explicit_choice(db, current, label):
    db.subscriptions = [sub(UUID1, site=ASURA_URL, alert_timing=current)]
    text, markup = press(db, f"tim:{UUID1}").edits[0]
    assert f"Now: {label}" in text
    ticked = [b.text for b in all_buttons(markup) if b.text.startswith("\u2713")]
    assert ticked == [f"\u2713 {label}"]


def test_timing_page_is_not_available_on_a_site_without_early_access(db):
    db.subscriptions = [sub(UUID1)]
    assert press(db, f"tim:{UUID1}").edits[0][0] == bot.MESSAGES["not_found"]


@pytest.mark.parametrize("code, stored", [("d", None), ("e", "early"), ("w", "when_free")])
def test_choosing_a_timing_saves_it_and_redraws_the_page(db, code, stored):
    db.subscriptions = [sub(UUID1, site=ASURA_URL, alert_timing="early" if stored is None else None)]
    query = press(db, f"tset:{UUID1}:{code}")
    assert ("set_alert_timing", ("user-1", UUID1, stored)) in db.calls
    text, markup = query.edits[0]
    assert bot.MESSAGES["timing_saved"] in text
    assert sum(1 for b in all_buttons(markup) if b.text.startswith("\u2713")) == 1


@pytest.mark.parametrize("data", [f"tset:{UUID1}", f"tset:{UUID1}:x", f"tset:{UUID1}:", f"tset:{UUID1}:early"])
def test_unknown_timing_codes_change_nothing(db, data):
    db.subscriptions = [sub(UUID1, site=ASURA_URL)]
    assert press(db, data).edits[0][0] == bot.MESSAGES["not_found"]
    assert "set_alert_timing" not in db.names()


def test_timing_cannot_be_set_on_a_site_without_early_access(db):
    db.subscriptions = [sub(UUID1)]
    assert press(db, f"tset:{UUID1}:e").edits[0][0] == bot.MESSAGES["not_found"]
    assert "set_alert_timing" not in db.names()


def test_a_timing_write_that_matched_no_row_is_not_found(db):
    db.subscriptions = [sub(UUID1, site=ASURA_URL)]
    db.timing_changes = False
    assert press(db, f"tset:{UUID1}:e").edits[0][0] == bot.MESSAGES["not_found"]


def test_pressing_the_same_timing_twice_does_not_crash(db):
    db.subscriptions = [sub(UUID1, site=ASURA_URL)]
    update = make_update(callback_data=f"tset:{UUID1}:e")
    update.callback_query.edit_error = BadRequest("Message is not modified")
    run(bot.on_callback(update, make_context()))               # must not raise


# ------------------------------------------------------------------ /settings and the toggle

def test_settings_lists_only_the_early_access_sites_with_the_current_account_type(db):
    db.tiers[("user-1", WEB_ASURA)] = "member"
    update = make_update()
    run(bot.cmd_settings(update, make_context()))
    text, markup = update.effective_message.replies[0]
    assert text == bot.MESSAGES["settings_header"]
    assert [b.text for b in all_buttons(markup)] == ["Asura Scans: Asura+ member (tap to switch)", "Close"]
    assert keyboard_data(markup) == [f"tog:{WEB_ASURA}", "close"]


def test_settings_defaults_to_the_free_reader_account(db):
    update = make_update()
    run(bot.cmd_settings(update, make_context()))
    assert "Asura Scans: Free reader" in all_buttons(update.effective_message.replies[0][1])[0].text


def test_settings_needs_a_registered_user(db):
    db.user = None
    update = make_update()
    run(bot.cmd_settings(update, make_context()))
    assert update.effective_message.replies[0][0] == bot.MESSAGES["need_start"]


def test_settings_with_no_early_access_sites_says_there_is_nothing_to_set_up(db):
    update = make_update()
    context = make_context()
    context.application.bot_data["rules"] = {HIVE_URL: RULES[HIVE_URL]}
    run(bot.cmd_settings(update, context))
    assert update.effective_message.replies[0] == (bot.MESSAGES["settings_none"], None)


def test_the_toggle_flips_the_account_type_and_redraws(db):
    query = press(db, f"tog:{WEB_ASURA}")
    assert db.tiers[("user-1", WEB_ASURA)] == "member"
    assert "Asura+ member" in all_buttons(query.edits[0][1])[0].text
    query = press(db, f"tog:{WEB_ASURA}")
    assert db.tiers[("user-1", WEB_ASURA)] == "standard"
    assert "Free reader" in all_buttons(query.edits[0][1])[0].text


def test_the_toggle_rejects_an_unknown_site_and_a_site_without_early_access(db):
    for web_id in (WEB_OTHER, WEB_HIVE):
        assert press(db, f"tog:{web_id}").edits[0][0] == bot.MESSAGES["not_found"]
    assert "set_user_tier" not in db.names()


@pytest.mark.parametrize("data", ["tog", "tog:", "tog:nope"])
def test_malformed_toggles_are_not_found(db, data):
    assert press(db, data).edits[0][0] == bot.MESSAGES["not_found"]
    assert "set_user_tier" not in db.names()


# ------------------------------------------------------------------ the tier question after /add

def test_following_on_an_early_access_site_asks_for_the_account_type_once(db):
    db.summary = {"id": "n1", "web_id": WEB_ASURA, "novel_name": "killer-pietro", "display_title": "Killer Pietro"}
    outcome = bot.process_add("user-1", ASURA_LINK, RULES)
    assert outcome.done is True and outcome.tier_web_id == WEB_ASURA
    assert outcome.tier_labels == {"standard": "Free reader", "member": "Asura+ member"}
    assert outcome.text.startswith("Done! You are now following <b>Killer Pietro</b> (Asura Scans)")
    assert outcome.text.endswith(bot.MESSAGES["tier_question"])
    assert ("get_user_tier", ("user-1", WEB_ASURA)) in db.calls


def test_no_question_when_the_account_type_is_already_known(db):
    db.summary = {"id": "n1", "web_id": WEB_ASURA, "novel_name": "killer-pietro", "display_title": None}
    db.tiers[("user-1", WEB_ASURA)] = "standard"
    outcome = bot.process_add("user-1", ASURA_LINK, RULES)
    assert outcome.tier_web_id is None and bot.MESSAGES["tier_question"] not in outcome.text


def test_no_question_on_a_site_without_early_access(db):
    bot.process_add("user-1", "https://hivetoons.org/series/x", RULES)
    assert "get_user_tier" not in db.names()


def test_no_question_when_the_novel_was_already_followed(db):
    db.add_result = "already_subscribed"
    db.summary = {"id": "n1", "web_id": WEB_ASURA, "novel_name": "killer-pietro", "display_title": None}
    outcome = bot.process_add("user-1", ASURA_LINK, RULES)
    assert outcome.tier_web_id is None and "already following" in outcome.text


def test_a_failure_after_the_follow_never_turns_it_into_an_error(db):
    db.summary_error = RuntimeError("db hiccup")
    outcome = bot.process_add("user-1", ASURA_LINK, RULES)
    assert outcome.done is True and "Killer Pietro" in outcome.text and outcome.tier_web_id is None


def test_a_failed_tier_lookup_also_never_turns_the_follow_into_an_error(db, monkeypatch):
    db.summary = {"id": "n1", "web_id": WEB_ASURA, "novel_name": "killer-pietro", "display_title": None}
    monkeypatch.setattr(db, "get_user_tier", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    outcome = bot.process_add("user-1", ASURA_LINK, RULES)
    assert outcome.done is True and "following" in outcome.text and outcome.tier_web_id is None


def test_the_add_handler_attaches_the_two_buttons(db):
    db.summary = {"id": "n1", "web_id": WEB_ASURA, "novel_name": "killer-pietro", "display_title": None}
    update, context = make_update(), make_context(args=[ASURA_LINK])
    run(bot.cmd_add(update, context))
    text, markup = update.effective_message.replies[0]
    assert bot.MESSAGES["tier_question"] in text
    assert keyboard_data(markup) == [f"tier:{WEB_ASURA}:s", f"tier:{WEB_ASURA}:m"]
    assert [b.text for b in all_buttons(markup)] == ["Free reader", "Asura+ member"]
    assert context.user_data["awaiting_link"] is False


def test_the_add_handler_sends_no_buttons_for_a_normal_site(db):
    update = make_update()
    run(bot.cmd_add(update, make_context(args=["https://hivetoons.org/series/x"])))
    assert update.effective_message.replies[0][1] is None


@pytest.mark.parametrize("code, tier", [("s", "standard"), ("m", "member")])
def test_answering_the_question_saves_the_tier_and_keeps_the_confirmation(db, code, tier):
    confirmation = "Done! You are now following <b>Killer Pietro</b> (Asura Scans). I will message you when a new chapter appears."
    query = press_with_text(db, f"tier:{WEB_ASURA}:{code}", confirmation + "\n\n" + bot.MESSAGES["tier_question"])
    assert db.tiers[("user-1", WEB_ASURA)] == tier
    text = query.edits[0][0]
    assert text.startswith(confirmation) and bot.MESSAGES["tier_question"] not in text
    assert bot.fmt("tier_saved", label=bot.tier_label(RULES, ASURA_URL, tier)) in text
    assert query.edits[0][1] is None                                      # the buttons go away


def test_answering_when_the_message_text_is_unavailable_still_confirms(db):
    query = press_with_text(db, f"tier:{WEB_ASURA}:m", None)
    assert query.edits[0][0] == bot.fmt("tier_saved", label="Asura+ member")


@pytest.mark.parametrize("data", [f"tier:{WEB_ASURA}", f"tier:{WEB_ASURA}:x", f"tier:{WEB_ASURA}:", f"tier:{WEB_ASURA}:member", "tier", "tier:nope:s"])
def test_malformed_tier_answers_change_nothing(db, data):
    assert press(db, data).edits[0][0] == bot.MESSAGES["not_found"]
    assert "set_user_tier" not in db.names()


def test_a_tier_answer_for_a_site_that_does_not_exist_or_has_no_early_access_changes_nothing(db):
    for web_id in (WEB_OTHER, WEB_HIVE):
        assert press(db, f"tier:{web_id}:m").edits[0][0] == bot.MESSAGES["not_found"]
    assert "set_user_tier" not in db.names()


def test_every_new_action_needs_a_registered_user(db):
    db.user = None
    for data in ("hub", f"site:{WEB_HIVE}", f"tier:{WEB_ASURA}:s", f"tog:{WEB_ASURA}", f"nov:{UUID1}", f"tim:{UUID1}", f"tset:{UUID1}:e"):
        assert press(db, data).edits[0][0] == bot.MESSAGES["need_start"]
    assert "set_user_tier" not in db.names() and "set_alert_timing" not in db.names()


# ------------------------------------------------------------------ /start wording and the flat pickers

def test_start_for_a_returning_user_shows_how_many_novels_they_follow(db):
    db.subscriptions = [sub(UUID1)]
    update = make_update()
    run(bot.cmd_start(update, make_context()))
    assert update.effective_message.replies[0][0] == "Welcome back! You're following 1 novel. /list to see them, /add to follow another."


def test_start_welcome_for_a_new_user_follows_the_documented_text(db):
    db.user = None
    update = make_update()
    run(bot.cmd_start(update, make_context()))
    text = update.effective_message.replies[0][0]
    assert text.startswith("\U0001F44B <b>Welcome to ScrollPing!</b>\nI'll message you when novels you follow get a new chapter.")
    assert "To begin, send /add and paste a novel link." in text and "Supported sites: Asura Scans, Hive Toons" in text


def test_start_resumes_with_the_alerts_wording(db):
    db.user = dict(USER, status="paused")
    update = make_update()
    run(bot.cmd_start(update, make_context()))
    assert update.effective_message.replies[0][0] == "Welcome back! Alerts are switched on again."


def test_help_mentions_settings():
    assert "/settings" in bot.MESSAGES["help"]


def test_the_flat_remove_and_status_pickers_show_titles(db):
    db.subscriptions = [sub(UUID1, name="killer-pietro", display_title="Killer Pietro")]
    update = make_update()
    run(bot.cmd_remove(update, make_context()))
    markup = update.effective_message.replies[0][1]
    assert [b.text for b in all_buttons(markup)] == ["Killer Pietro"] and keyboard_data(markup) == [f"rm:{UUID1}"]


def test_status_and_removal_messages_use_the_title(db):
    db.subscriptions = [sub(UUID1, name="killer-pietro", display_title="Killer Pietro")]
    assert "<b>Killer Pietro</b>" in press(db, f"rm:{UUID1}").edits[0][0]
    assert "<b>Killer Pietro</b> is now: completed" in press(db, f"ss:{UUID1}:1").edits[0][0]
    assert "Removed <b>Killer Pietro</b>" in press(db, f"ry:{UUID1}").edits[0][0]


# ------------------------------------------------------------------ Telegram limits

def test_every_new_callback_fits_telegrams_64_byte_limit():
    rows = [sub(UUID1, site=ASURA_URL, alert_timing="early"), sub(UUID2, name="b")]
    markups = [
        bot.hub_view(rows, RULES)[1],
        bot.site_view(bot.group_by_site(rows, RULES)[0])[1],
        bot.novel_view(rows[0], RULES)[1],
        bot.timing_view(rows[0], RULES, "member")[1],
        bot.settings_view([{"web_id": WEB_ASURA, "domain": ASURA_URL}], {}, RULES)[1],
        bot.tier_keyboard(WEB_ASURA, {"standard": "a", "member": "b"}),
    ]
    for markup in markups:
        for button in all_buttons(markup):
            assert len(button.callback_data.encode()) <= 64


def test_every_button_label_fits_the_cap_even_for_huge_titles():
    row = sub(UUID1, display_title="x" * 500, site=ASURA_URL)
    for markup in (bot.site_view(bot.group_by_site([row], RULES)[0])[1], bot.picker_keyboard("rm", [row])):
        assert all(len(b.text) <= bot.MAX_BUTTON_LABEL_LEN for b in all_buttons(markup))


def test_the_hub_stays_one_message_for_the_maximum_number_of_sites_and_novels():
    rows = [sub(f"{i:08d}-1111-4111-8111-111111111111", name=f"n{i}", web_id=f"{i % 7:08d}-2222-4222-8222-222222222222", site=f"https://site{i % 7}.example/") for i in range(50)]
    text, markup = bot.hub_view(rows, {})
    assert len(text) < 500 and len(all_buttons(markup)) == 8
