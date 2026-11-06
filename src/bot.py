"""bot.py - ScrollPing's Telegram command handler (long polling).

Run it as its own always-on process:   python -m src.bot
(the cron scraper, main.py, only SENDS messages; this process only RECEIVES commands)

Commands
    /start   register (or resume if paused) + welcome
    /add     follow a novel: /add <link>, or /add and then send the link
    /list    your novels, tap one to change its status or remove it
    /remove  pick a novel to stop following
    /status  pick a novel to change its reading status
    /stop    pause notifications, or delete your account and data
    /help    command list and supported sites
    /cancel  abandon a half-finished /add

Layout
    * Handlers are thin: they call database.py (all DB access) and novel_links.py (link
      parsing). Blocking DB calls run through asyncio.to_thread so one slow query cannot
      freeze the bot for everyone else.
    * process_add() and the format_* helpers are plain functions so tests need no Telegram.
    * All user-facing text lives in MESSAGES at the top so the wording can be rewritten
      in one place.
    * rate_gate runs before every handler (group -1): private chats only, one token bucket
      per user, over-limit updates are dropped before any database work happens.
    * The bot uses the Supabase SECRET key (it bypasses RLS), so identity comes only from
      update.effective_user.id and every subscription change filters on the user's own id.
"""

import asyncio
import json
import logging
import uuid
from pathlib import Path
from urllib.parse import urlparse

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from src import database, novel_links
from src.ratelimiter import TokenBucketLimiter
from src.telegramMessenger import get_token

logger = logging.getLogger("scrollping.bot")

MAX_SUBSCRIPTIONS = 20
MAX_INPUT_LEN = 300          # longer text is rejected before any parsing
MAX_BUTTON_LABEL_LEN = 60
RULES_PATH = Path(__file__).resolve().parent.parent / "site_parse_rules.json"

# order matters: the index is what callback data carries (see "ss:<id>:<index>")
STATUS_CHOICES = ("reading", "completed", "left", "on hiatus")

BOT_COMMANDS = [
    ("add", "Follow a novel"),
    ("list", "Your novels"),
    ("remove", "Stop following a novel"),
    ("status", "Change a reading status"),
    ("stop", "Pause or delete your account"),
    ("help", "Help and supported sites"),
]

MESSAGES = {
    "welcome": (
        "Welcome to ScrollPing! I message you when a new chapter comes out for the "
        "novels you follow.\n\nSend /add to follow your first novel."
    ),
    "welcome_back": "Welcome back! Your novels are unchanged. Send /list to see them.",
    "resumed": "Welcome back! Notifications are switched on again.",
    "help": (
        "/add - follow a novel\n"
        "/list - your novels\n"
        "/remove - stop following a novel\n"
        "/status - change a reading status\n"
        "/stop - pause notifications or delete your account\n"
        "/cancel - cancel what you were doing"
    ),
    "sites_header": "Supported websites:",
    "add_prompt": (
        "Send me the link of the novel.\n"
        "Tip: copy the whole link from your browser and send it as it is, I will work out the rest."
    ),
    "add_usage_hint": "To follow a novel, send /add first.",
    "need_start": "Please send /start first.",
    "added": "Done! You are now following \"{slug}\". I will message you when a new chapter appears.",
    "already": "You are already following \"{slug}\".",
    "limit": "You can follow at most {limit} novels. Remove one with /remove first.",
    "bad_link": "That does not look like a link. Copy the whole link from your browser and send it as it is.",
    "too_long": "That message is too long. Please send just the link.",
    "empty": "Please send the link of the novel.",
    "unsupported_site": "I can only follow novels from the supported websites.",
    "not_a_novel_link": "That link is from a supported website, but it is not a novel or chapter page. Open the novel (or one of its chapters) and send that link.",
    "bad_slug": "I could not read a novel name from that link. Try the link of the novel's own page.",
    "no_subscriptions": "You are not following any novels yet. Send /add to start.",
    "list_header": "Your novels (tap one to change it):",
    "pick_remove": "Which novel do you want to stop following?",
    "pick_status": "Which novel do you want to change?",
    "choose_status": "New status for \"{slug}\":\n(only \"reading\" sends notifications)",
    "menu_for": "\"{slug}\" - {status}\nWhat do you want to do?",
    "status_changed": "\"{slug}\" is now: {status}.",
    "confirm_remove": "Stop following \"{slug}\"?",
    "removed": "Removed \"{slug}\".",
    "not_found": "I could not find that novel in your list. Send /list to refresh.",
    "unchanged": "Okay, nothing changed.",
    "stop_menu": "What would you like to do?",
    "stop_paused": "Notifications are paused. Send /start whenever you want them back.",
    "stop_confirm_delete": "This permanently deletes your account and every novel you follow. Are you sure?",
    "stop_deleted": "Your account and data have been deleted. Send /start any time to begin again.",
    "cancelled": "Cancelled.",
    "nothing_to_cancel": "Nothing to cancel.",
    "slow_down": "You are sending commands too fast. Please wait a few seconds.",
    "text_only": "I only understand text messages. Try /help.",
    "unknown_command": "I do not know that command. Try /help.",
    "generic_error": "Something went wrong on my side. Please try again in a moment.",
}

ADD_FAILURE_REASONS = ("empty", "too_long", "not_a_link", "unsupported_site", "not_a_novel_link", "bad_slug")


# --------------------------------------------------------------------------
# Plain helpers (no Telegram objects, no I/O) - these are what the tests hit
# --------------------------------------------------------------------------

def load_rules(path=RULES_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def sites_text(rules):
    sites = novel_links.supported_sites(rules)
    return MESSAGES["sites_header"] + "\n" + "\n".join(f"- {site['host']}" for site in sites)


def display_name(telegram_user):
    name = telegram_user.username or telegram_user.first_name or str(telegram_user.id)
    return name[:64]


def chapter_text(latest_chap):
    """latest_chap is -1 (the database default) until the scraper has seen the novel."""
    try:
        value = float(latest_chap)
    except (TypeError, ValueError):
        return "waiting for first update"
    if value < 0:
        return "waiting for first update"
    return f"chapter {value:g}"


def host_of(web_domain_name):
    return novel_links.normalize_host(urlparse(web_domain_name or "").hostname) or "?"


def format_subscriptions(rows):
    lines = []
    for number, row in enumerate(rows, start=1):
        lines.append(
            f"{number}. {row['novel_name']} ({host_of(row['web_domain_name'])}) - "
            f"{chapter_text(row['latest_chap'])} - {row['reading_status']}"
        )
    return "\n".join(lines)


def button_label(row):
    return row["novel_name"][:MAX_BUTTON_LABEL_LEN]


def process_add(user_id, text, rules):
    """Handle a pasted link. Returns (reply_text, done): done=False means the link was
    unusable and the bot should keep waiting so the user can send another one."""
    if text is not None and len(text) > MAX_INPUT_LEN:
        return MESSAGES["too_long"], False

    parsed = novel_links.parse_novel_link(text, rules)
    if not parsed["ok"]:
        reason = parsed["reason"] if parsed["reason"] in ADD_FAILURE_REASONS else "bad_link"
        key = "bad_link" if reason == "not_a_link" else reason
        reply = MESSAGES[key]
        if reason == "unsupported_site":
            reply += "\n\n" + sites_text(rules)
        return reply, False

    slug = parsed["slug"]
    result = database.subscribe_user_to_novel(user_id, parsed["site_key"], slug, MAX_SUBSCRIPTIONS)

    if result == "added":
        return MESSAGES["added"].format(slug=slug), True
    if result == "already_subscribed":
        return MESSAGES["already"].format(slug=slug), True
    return MESSAGES["limit"].format(limit=MAX_SUBSCRIPTIONS), True


def parse_uuid(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


def picker_keyboard(prefix, rows):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(button_label(row), callback_data=f"{prefix}:{row['id']}")] for row in rows]
    )


def status_keyboard(subscription_id):
    buttons = [
        [InlineKeyboardButton(status, callback_data=f"ss:{subscription_id}:{index}")]
        for index, status in enumerate(STATUS_CHOICES)
    ]
    buttons.append([InlineKeyboardButton("Cancel", callback_data="no")])
    return InlineKeyboardMarkup(buttons)


# --------------------------------------------------------------------------
# Telegram plumbing
# --------------------------------------------------------------------------

async def run_db(function, *args):
    return await asyncio.to_thread(function, *args)


async def rate_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs before every handler. Anything that is not a private chat with a real user,
    or that exceeds the user's token bucket, never reaches a handler."""
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None or chat.type != ChatType.PRIVATE:
        raise ApplicationHandlerStop

    verdict = context.application.bot_data["limiter"].check(user.id)
    if verdict == "allow":
        return

    if verdict == "warn":
        if update.callback_query is not None:
            await update.callback_query.answer(MESSAGES["slow_down"])
        elif update.effective_message is not None:
            await update.effective_message.reply_text(MESSAGES["slow_down"])
    raise ApplicationHandlerStop


async def require_user(update, context):
    """Return the users row for whoever is talking, or tell them to /start first."""
    user = await run_db(database.get_user, update.effective_user.id)
    if user is None:
        await update.effective_message.reply_text(MESSAGES["need_start"])
    return user


async def cmd_start(update, context):
    telegram_user = update.effective_user
    user = await run_db(database.get_user, telegram_user.id)

    if user is None:
        await run_db(database.create_user, telegram_user.id, display_name(telegram_user))
        text = MESSAGES["welcome"] + "\n\n" + MESSAGES["help"] + "\n\n" + sites_text(context.application.bot_data["rules"])
    elif user.get("status") == "paused":
        await run_db(database.set_user_status, user["id"], "active")
        text = MESSAGES["resumed"]
    else:
        text = MESSAGES["welcome_back"]

    await update.effective_message.reply_text(text)


async def cmd_help(update, context):
    text = MESSAGES["help"] + "\n\n" + sites_text(context.application.bot_data["rules"])
    await update.effective_message.reply_text(text)


async def cmd_add(update, context):
    user = await require_user(update, context)
    if user is None:
        return

    if context.args:
        await reply_to_link(update, context, user, " ".join(context.args))
        return

    context.user_data["awaiting_link"] = True
    text = MESSAGES["add_prompt"] + "\n\n" + sites_text(context.application.bot_data["rules"])
    await update.effective_message.reply_text(text)


async def reply_to_link(update, context, user, text):
    reply, done = await run_db(process_add, user["id"], text, context.application.bot_data["rules"])
    context.user_data["awaiting_link"] = not done
    await update.effective_message.reply_text(reply)


async def on_text(update, context):
    if not context.user_data.get("awaiting_link"):
        await update.effective_message.reply_text(MESSAGES["add_usage_hint"])
        return

    user = await require_user(update, context)
    if user is None:
        return
    await reply_to_link(update, context, user, update.effective_message.text)


async def on_non_text(update, context):
    await update.effective_message.reply_text(MESSAGES["text_only"])


async def on_unknown_command(update, context):
    await update.effective_message.reply_text(MESSAGES["unknown_command"])


async def cmd_cancel(update, context):
    was_waiting = context.user_data.pop("awaiting_link", False)
    await update.effective_message.reply_text(MESSAGES["cancelled"] if was_waiting else MESSAGES["nothing_to_cancel"])


async def send_picker(update, context, prefix, header, show_list=False):
    user = await require_user(update, context)
    if user is None:
        return

    rows = await run_db(database.list_subscriptions, user["id"])
    if not rows:
        await update.effective_message.reply_text(MESSAGES["no_subscriptions"])
        return

    text = header
    if show_list:
        text = header + "\n\n" + format_subscriptions(rows)
    await update.effective_message.reply_text(text, reply_markup=picker_keyboard(prefix, rows))


async def cmd_list(update, context):
    await send_picker(update, context, "m", MESSAGES["list_header"], show_list=True)


async def cmd_remove(update, context):
    await send_picker(update, context, "rm", MESSAGES["pick_remove"])


async def cmd_status(update, context):
    await send_picker(update, context, "st", MESSAGES["pick_status"])


async def cmd_stop(update, context):
    user = await require_user(update, context)
    if user is None:
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Pause notifications", callback_data="stop:pause")],
        [InlineKeyboardButton("Delete my account and data", callback_data="stop:delete")],
        [InlineKeyboardButton("Cancel", callback_data="no")],
    ])
    await update.effective_message.reply_text(MESSAGES["stop_menu"], reply_markup=keyboard)


async def edit(query, text, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def on_callback(update, context):
    """One router for every inline button. Callback data formats:
        m:<id>  rm:<id>  st:<id>  ry:<id>  ss:<id>:<status index>   and   stop:<action>  no
    <id> is a subscription id; every database call re-checks that it belongs to this user."""
    query = update.callback_query
    await query.answer()

    parts = (query.data or "").split(":")
    action = parts[0]

    if action == "no":
        await edit(query, MESSAGES["unchanged"])
        return

    user = await run_db(database.get_user, update.effective_user.id)
    if user is None:
        await edit(query, MESSAGES["need_start"])
        return

    if action == "stop":
        await handle_stop_action(query, user, parts[1] if len(parts) > 1 else "")
        return

    subscription_id = parse_uuid(parts[1]) if len(parts) > 1 else None
    if subscription_id is None:
        await edit(query, MESSAGES["not_found"])
        return

    rows = await run_db(database.list_subscriptions, user["id"])
    row = next((r for r in rows if r["id"] == subscription_id), None)
    if row is None:
        await edit(query, MESSAGES["not_found"])
        return

    slug = row["novel_name"]

    if action == "m":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Change status", callback_data=f"st:{subscription_id}")],
            [InlineKeyboardButton("Remove", callback_data=f"rm:{subscription_id}")],
            [InlineKeyboardButton("Cancel", callback_data="no")],
        ])
        await edit(query, MESSAGES["menu_for"].format(slug=slug, status=row["reading_status"]), keyboard)

    elif action == "st":
        await edit(query, MESSAGES["choose_status"].format(slug=slug), status_keyboard(subscription_id))

    elif action == "ss":
        try:
            status = STATUS_CHOICES[int(parts[2])]
        except (IndexError, ValueError):
            await edit(query, MESSAGES["not_found"])
            return
        changed = await run_db(database.set_subscription_status, user["id"], subscription_id, status)
        text = MESSAGES["status_changed"].format(slug=slug, status=status) if changed else MESSAGES["not_found"]
        await edit(query, text)

    elif action == "rm":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Yes, remove", callback_data=f"ry:{subscription_id}")],
            [InlineKeyboardButton("No", callback_data="no")],
        ])
        await edit(query, MESSAGES["confirm_remove"].format(slug=slug), keyboard)

    elif action == "ry":
        removed = await run_db(database.remove_subscription, user["id"], subscription_id)
        await edit(query, MESSAGES["removed"].format(slug=slug) if removed else MESSAGES["not_found"])

    else:
        await edit(query, MESSAGES["not_found"])


async def handle_stop_action(query, user, action):
    if action == "pause":
        await run_db(database.set_user_status, user["id"], "paused")
        await edit(query, MESSAGES["stop_paused"])

    elif action == "delete":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Yes, delete everything", callback_data="stop:deleteyes")],
            [InlineKeyboardButton("Cancel", callback_data="no")],
        ])
        await edit(query, MESSAGES["stop_confirm_delete"], keyboard)

    elif action == "deleteyes":
        await run_db(database.delete_user_account, user["id"])
        await edit(query, MESSAGES["stop_deleted"])

    else:
        await edit(query, MESSAGES["not_found"])


async def on_error(update, context):
    logger.error("unhandled error while handling an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(MESSAGES["generic_error"])
        except Exception:
            logger.exception("could not even send the error message")


async def post_init(application):
    await application.bot.set_my_commands([BotCommand(name, description) for name, description in BOT_COMMANDS])


def build_application(token, rules):
    application = Application.builder().token(token).post_init(post_init).build()
    application.bot_data["rules"] = rules
    application.bot_data["limiter"] = TokenBucketLimiter(capacity=5, refill_per_second=0.5, warn_cooldown=10)

    application.add_handler(TypeHandler(Update, rate_gate), group=-1)

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("add", cmd_add))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("remove", cmd_remove))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("stop", cmd_stop))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CallbackQueryHandler(on_callback))

    application.add_handler(MessageHandler(filters.COMMAND, on_unknown_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_handler(MessageHandler(filters.UpdateType.MESSAGE & ~filters.TEXT & ~filters.COMMAND, on_non_text))

    application.add_error_handler(on_error)
    return application


def configure_logging():
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    # httpx logs every request URL at INFO, and a Telegram request URL contains the bot token
    # (https://api.telegram.org/bot<TOKEN>/getUpdates). Keep these loggers quiet so the token
    # never lands in a terminal, a log file or a pasted screenshot.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main():
    configure_logging()
    application = build_application(get_token(), load_rules())
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()