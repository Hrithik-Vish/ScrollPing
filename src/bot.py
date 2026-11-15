"""bot.py - ScrollPing's Telegram command handler (long polling).

Run it as its own always-on process:   python -m src.bot
(the cron scraper, main.py, only SENDS messages; this process only RECEIVES commands)

Commands
    /start     register (or resume if paused) + welcome
    /add       follow a novel: /add <link>, or /add and then send the link
    /list      your novels: pick a site, then a novel, then change its status, alert timing or remove it
    /settings  your account type on sites with early access (decides when early chapters alert you)
    /remove    pick a novel to stop following
    /status    pick a novel to change its reading status
    /stop      pause notifications, or delete your account and data
    /help      command list and supported sites
    /cancel    abandon a half-finished /add

Layout
    * Handlers are thin: they call database.py (all DB access) and novel_links.py (link
      parsing). Blocking DB calls run through asyncio.to_thread so one slow query cannot
      freeze the bot for everyone else.
    * process_add() and the *_view helpers are plain functions so tests need no Telegram.
    * All user-facing text lives in MESSAGES at the top so the wording can be rewritten
      in one place. Every message is sent in HTML mode (Defaults(parse_mode=HTML)), so EVERY value
      that is not literal text is escaped: fmt() does it for template values, esc() for the rest.
    * rate_gate runs before every handler (group -1): private chats only, one token bucket
      per user, over-limit updates are dropped before any database work happens.
    * The bot uses the Supabase SECRET key (it bypasses RLS), so identity comes only from
      update.effective_user.id and every database change filters on the user's own id.

Callback data (all well under Telegram's 64 bytes; <id> is a subscription id, <web> a website id):
    hub  site:<web>  nov:<id>  m:<id> (old /list buttons)  tim:<id>  tset:<id>:<d|e|w>
    tier:<web>:<s|m>  tog:<web>  st:<id>  ss:<id>:<n>  rm:<id>  ry:<id>  stop:<action>  no  close
Every one that names a subscription re-checks it belongs to the caller; every one that names a
website re-checks it is a real site that has early access.
"""

import asyncio
import html
import json
import logging
import os
import uuid
from pathlib import Path
from typing import NamedTuple, Optional
from urllib.parse import urlparse

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    TypeHandler,
    filters,
)

from src import database, novel_links
from src.health import STATUS, start_health_server
from src.ratelimiter import TokenBucketLimiter
from src.telegramMessenger import get_token

logger = logging.getLogger("scrollping.bot")

MAX_SUBSCRIPTIONS = 50       # raised from 20 for the burn-in
MAX_LIST_CHUNK = 3500        # /list text is split below Telegram's 4096-character message limit
MAX_INPUT_LEN = 300          # longer text is rejected before any parsing
MAX_BUTTON_LABEL_LEN = 60
RULES_PATH = Path(__file__).resolve().parent.parent / "site_parse_rules.json"

# order matters: the index is what callback data carries (see "ss:<id>:<index>")
STATUS_CHOICES = ("reading", "completed", "left", "on hiatus")

BOT_COMMANDS = [
    ("add", "Follow a novel"),
    ("list", "Your novels"),
    ("settings", "Early-access account type"),
    ("remove", "Stop following a novel"),
    ("status", "Change a reading status"),
    ("stop", "Pause or delete your account"),
    ("help", "Help and supported sites"),
]

# alert timing choices: callback code -> stored value (None = follow the site tier)
TIMING_CODES = {"d": None, "e": "early", "w": "when_free"}
TIMING_BUTTONS = (("d", "Follow my site setting"), ("e", "Always early"), ("w", "Only when free"))
TIER_CODES = {"s": "standard", "m": "member"}

MESSAGES = {
    "welcome": (
        "\U0001F44B <b>Welcome to ScrollPing!</b>\n"
        "I'll message you when novels you follow get a new chapter.\n\n"
        "To begin, send /add and paste a novel link.\n"
        "Supported sites: {sites}\n\n"
        "Send /help to see every command."
    ),
    "welcome_back": "Welcome back! You're following {count}. /list to see them, /add to follow another.",
    "resumed": "Welcome back! Alerts are switched on again.",
    "help": (
        "/add - follow a novel\n"
        "/list - your novels\n"
        "/settings - early-access account type\n"
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
    "added": "Done! You are now following <b>{title}</b> ({site}). I will message you when a new chapter appears.",
    "already": "You are already following <b>{title}</b>.",
    "limit": "You can follow at most {limit} novels. Remove one with /remove first.",
    "tier_question": (
        "This site has early access for paying members. Which are you?\n"
        "(This decides when you are alerted to early chapters; change it any time in /settings.)"
    ),
    "tier_saved": "Saved: {label}. You can change this any time in /settings.",
    "bad_link": "That does not look like a link. Copy the whole link from your browser and send it as it is.",
    "too_long": "That message is too long. Please send just the link.",
    "empty": "Please send the link of the novel.",
    "unsupported_site": "I can only follow novels from the supported websites.",
    "not_a_novel_link": "That link is from a supported website, but it is not a novel or chapter page. Open the novel (or one of its chapters) and send that link.",
    "bad_slug": "I could not read a novel name from that link. Try the link of the novel's own page.",
    "no_subscriptions": "You are not following any novels yet. Send /add to start.",
    "hub_header": "\U0001F4DA <b>Your novels</b>\nYou follow {novels} across {sites}. Pick a site:",
    "site_header": "<b>{site}</b>\n{novels}. Pick one to change it:",
    "pick_remove": "Which novel do you want to stop following?",
    "pick_status": "Which novel do you want to change?",
    "choose_status": "New status for <b>{title}</b>:\n(only \"reading\" sends notifications)",
    "novel_menu": "<b>{title}</b>\n{site}\nStatus: {status}\n{chapter}",
    "waiting_note": "I'll message you when I see a new chapter.",
    "timing_menu": (
        "<b>Alert timing</b> for <b>{title}</b>\n"
        "Now: {current}\n\n"
        "Early alerts tell you as soon as a chapter appears for members. "
        "Otherwise you are told when it becomes free."
    ),
    "timing_saved": "\u2705 Saved.",
    "status_changed": "<b>{title}</b> is now: {status}.",
    "confirm_remove": "Stop following <b>{title}</b>?",
    "removed": "Removed <b>{title}</b>.",
    "settings_header": (
        "\u2699\ufe0f <b>Settings</b>\n"
        "Your account type on sites with early access. It decides when early chapters alert you "
        "(you can still override it for one novel from /list)."
    ),
    "settings_none": "There is nothing to set up yet.",
    "not_found": "I could not find that novel in your list. Send /list to refresh.",
    "unchanged": "Okay, nothing changed.",
    "closed": "Closed.",
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


def esc(value):
    """Escape one value for Telegram's HTML mode. Text nodes only need & < > escaped."""
    return html.escape(str(value), quote=False)


def fmt(key, **values):
    """MESSAGES[key] with every value escaped. Never build user-facing text from raw values."""
    return MESSAGES[key].format(**{name: esc(value) for name, value in values.items()})


def host_of(web_domain_name):
    return novel_links.normalize_host(urlparse(web_domain_name or "").hostname) or "?"


def site_name(rules, domain):
    """A site's display name from the rules, else its host."""
    name = ((rules or {}).get(domain) or {}).get("display_name")
    return str(name).strip() if name and str(name).strip() else host_of(domain)


def timing_capable(rules, domain):
    """True for sites that publish early-access chapters (asura), i.e. where tier and timing matter."""
    return bool(((rules or {}).get(domain) or {}).get("lock_detection"))


def tier_label(rules, domain, tier):
    labels = ((rules or {}).get(domain) or {}).get("tier_labels") or {}
    return labels.get(tier) or tier.capitalize()


def sites_text(rules):
    sites = novel_links.supported_sites(rules)
    return MESSAGES["sites_header"] + "\n" + "\n".join(f"- {esc(site['host'])}" for site in sites)


def sites_inline(rules):
    """The supported sites as one comma-separated line of display names (welcome message)."""
    names = [site_name(rules, site["key"]) for site in novel_links.supported_sites(rules)]
    return esc(", ".join(names))


def display_name(telegram_user):
    name = telegram_user.username or telegram_user.first_name or str(telegram_user.id)
    return name[:64]


def plural(count, word):
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def chapter_text(latest_chap):
    """latest_chap is -1 (the database default) until the scraper has seen the novel."""
    try:
        value = float(latest_chap)
    except (TypeError, ValueError):
        return "waiting for first update"
    if value < 0:
        return "waiting for first update"
    return f"chapter {value:g}"


def chapter_label(latest_chap):
    """'Ch. 157', or 'waiting for first update' (never a bare -1)."""
    try:
        value = float(latest_chap)
    except (TypeError, ValueError):
        return "waiting for first update"
    if value < 0:
        return "waiting for first update"
    return f"Ch. {int(value)}" if value.is_integer() else f"Ch. {value:g}"


def split_text(text, limit=MAX_LIST_CHUNK):
    """Split text into chunks of at most `limit` characters, breaking only between lines
    (a single line longer than the limit is cut). 50 subscriptions with long names can
    exceed Telegram's 4096-character message limit, which would make a list fail."""
    chunks, current, size = [], [], 0
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        extra = len(line) + (1 if current else 0)
        if current and size + extra > limit:
            chunks.append("\n".join(current))
            current, size, extra = [], 0, len(line)
        current.append(line)
        size += extra
    chunks.append("\n".join(current))
    return chunks


def title_of(row):
    """What to call a novel: its scraped title, else a readable guess from the slug."""
    return novel_links.title_for(row)


def button_label(row):
    return title_of(row)[:MAX_BUTTON_LABEL_LEN]


def status_tag(row):
    status = row.get("reading_status")
    return f" ({esc(status)})" if status and status != "reading" else ""


def group_by_site(rows, rules):
    """[{"web_id", "domain", "name", "rows"}] sorted by site name; rows keep their (title) order."""
    groups = {}
    for row in rows:
        key = row.get("web_id") or row.get("web_domain_name")
        group = groups.setdefault(key, {"web_id": row.get("web_id"), "domain": row.get("web_domain_name"), "rows": []})
        group["rows"].append(row)
    result = []
    for group in groups.values():
        group["name"] = site_name(rules, group["domain"])
        group["rows"].sort(key=lambda r: (title_of(r).casefold(), r["id"]))
        result.append(group)
    return sorted(result, key=lambda g: (g["name"].casefold(), str(g["web_id"])))


def close_row():
    return [InlineKeyboardButton("Close", callback_data="close")]


def hub_view(rows, rules):
    """The /list start page: one button per site with how many novels are followed there."""
    groups = group_by_site(rows, rules)
    text = fmt("hub_header", novels=plural(len(rows), "novel"), sites=plural(len(groups), "site"))
    buttons = [
        [InlineKeyboardButton(f"{group['name']} ({len(group['rows'])})"[:MAX_BUTTON_LABEL_LEN], callback_data=f"site:{group['web_id']}")]
        for group in groups
    ]
    buttons.append(close_row())
    return text, InlineKeyboardMarkup(buttons)


def site_view(group):
    """One site's novels. Returns (text, markup); the text may need split_text before sending."""
    lines = [
        f"\u2022 {esc(title_of(row))} \u2014 {esc(chapter_label(row.get('latest_chap')))}{status_tag(row)}"
        for row in group["rows"]
    ]
    header = fmt("site_header", site=group["name"], novels=plural(len(group["rows"]), "novel"))
    buttons = [[InlineKeyboardButton(button_label(row), callback_data=f"nov:{row['id']}")] for row in group["rows"]]
    buttons.append([InlineKeyboardButton("\u00ab Back", callback_data="hub")])
    return header + "\n\n" + "\n".join(lines), InlineKeyboardMarkup(buttons)


def novel_view(row, rules):
    """One novel's page: current status and chapter, and what you can do with it."""
    domain = row.get("web_domain_name")
    chapter = chapter_label(row.get("latest_chap"))
    if chapter.startswith("waiting"):
        chapter = chapter + ". " + MESSAGES["waiting_note"]
    text = fmt("novel_menu", title=title_of(row), site=site_name(rules, domain), status=row["reading_status"], chapter=chapter)
    buttons = [[InlineKeyboardButton("Change status", callback_data=f"st:{row['id']}")]]
    if timing_capable(rules, domain):
        buttons.append([InlineKeyboardButton("Alert timing", callback_data=f"tim:{row['id']}")])
    buttons.append([InlineKeyboardButton("Stop following", callback_data=f"rm:{row['id']}")])
    buttons.append([InlineKeyboardButton("\u00ab Back", callback_data=f"site:{row.get('web_id')}")])
    return text, InlineKeyboardMarkup(buttons)


def timing_current(row, rules, tier):
    domain = row.get("web_domain_name")
    timing = row.get("alert_timing")
    if timing == "early":
        return "Always early"
    if timing == "when_free":
        return "Only when free"
    return f"Follow my site setting ({tier_label(rules, domain, tier)})"


def timing_view(row, rules, tier, saved=False):
    text = fmt("timing_menu", title=title_of(row), current=timing_current(row, rules, tier))
    if saved:
        text += "\n\n" + MESSAGES["timing_saved"]
    current = row.get("alert_timing")
    buttons = []
    for code, label in TIMING_BUTTONS:
        mark = "\u2713 " if TIMING_CODES[code] == current else ""
        buttons.append([InlineKeyboardButton(mark + label, callback_data=f"tset:{row['id']}:{code}")])
    buttons.append([InlineKeyboardButton("\u00ab Back", callback_data=f"nov:{row['id']}")])
    return text, InlineKeyboardMarkup(buttons)


def tier_keyboard(web_id, labels):
    """The two-button tier question shown after following a novel on an early-access site."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(labels["standard"], callback_data=f"tier:{web_id}:s")],
        [InlineKeyboardButton(labels["member"], callback_data=f"tier:{web_id}:m")],
    ])


def settings_view(entries, tiers, rules):
    """/settings: one toggle button per early-access site. entries: [{"web_id", "domain"}]."""
    if not entries:
        return MESSAGES["settings_none"], None
    buttons = []
    for entry in entries:
        tier = tiers.get(entry["web_id"], "standard")
        label = f"{site_name(rules, entry['domain'])}: {tier_label(rules, entry['domain'], tier)} (tap to switch)"
        buttons.append([InlineKeyboardButton(label[:MAX_BUTTON_LABEL_LEN], callback_data=f"tog:{entry['web_id']}")])
    buttons.append(close_row())
    return MESSAGES["settings_header"], InlineKeyboardMarkup(buttons)


def without_question(text_html):
    """The /add confirmation without its trailing tier question (used when the answer is saved)."""
    question = "\n\n" + MESSAGES["tier_question"]
    text = text_html or ""
    if text.endswith(question):
        return text[: -len(question)]
    index = text.find(MESSAGES["tier_question"].split("\n")[0])
    return text[:index].rstrip() if index >= 0 else text


class AddOutcome(NamedTuple):
    text: str
    done: bool                          # False: the link was unusable, keep waiting for another one
    tier_web_id: Optional[str] = None   # set when the early-access tier question should be shown
    tier_labels: Optional[dict] = None


def process_add(user_id, text, rules):
    """Handle a pasted link. done=False means the link was unusable and the bot should keep waiting
    so the user can send another one. After a successful follow on an early-access site where the
    user has not chosen a tier yet, tier_web_id is set so the handler can ask."""
    if text is not None and len(text) > MAX_INPUT_LEN:
        return AddOutcome(MESSAGES["too_long"], False)

    parsed = novel_links.parse_novel_link(text, rules)
    if not parsed["ok"]:
        reason = parsed["reason"] if parsed["reason"] in ADD_FAILURE_REASONS else "bad_link"
        key = "bad_link" if reason == "not_a_link" else reason
        reply = MESSAGES[key]
        if reason == "unsupported_site":
            reply += "\n\n" + sites_text(rules)
        return AddOutcome(reply, False)

    slug, site_key = parsed["slug"], parsed["site_key"]
    result = database.subscribe_user_to_novel(user_id, site_key, slug, MAX_SUBSCRIPTIONS)
    if result == "limit_reached":
        return AddOutcome(fmt("limit", limit=MAX_SUBSCRIPTIONS), True)

    # The follow itself is done. Anything below only improves the wording, so a failure must not
    # turn a successful follow into an error message.
    summary = None
    try:
        summary = database.get_novel_summary(site_key, slug)
    except Exception:
        logger.warning("could not read the novel summary after following", exc_info=True)
    title = title_of({"display_title": (summary or {}).get("display_title"), "novel_name": slug})

    if result == "already_subscribed":
        return AddOutcome(fmt("already", title=title), True)

    reply = fmt("added", title=title, site=site_name(rules, site_key))
    if summary and timing_capable(rules, site_key):
        try:
            if database.get_user_tier(user_id, summary["web_id"]) is None:
                labels = {tier: tier_label(rules, site_key, tier) for tier in ("standard", "member")}
                return AddOutcome(reply + "\n\n" + MESSAGES["tier_question"], True, summary["web_id"], labels)
        except Exception:
            logger.warning("could not check the site tier after following", exc_info=True)
    return AddOutcome(reply, True)


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
    rules = context.application.bot_data["rules"]

    if user is None:
        await run_db(database.create_user, telegram_user.id, display_name(telegram_user))
        text = MESSAGES["welcome"].format(sites=sites_inline(rules))     # sites_inline already escapes
    elif user.get("status") == "paused":
        await run_db(database.set_user_status, user["id"], "active")
        text = MESSAGES["resumed"]
    else:
        rows = await run_db(database.list_subscriptions, user["id"])
        text = fmt("welcome_back", count=plural(len(rows), "novel"))

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
    outcome = await run_db(process_add, user["id"], text, context.application.bot_data["rules"])
    context.user_data["awaiting_link"] = not outcome.done
    markup = tier_keyboard(outcome.tier_web_id, outcome.tier_labels) if outcome.tier_web_id else None
    await update.effective_message.reply_text(outcome.text, reply_markup=markup)


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


async def send_picker(update, context, prefix, header):
    user = await require_user(update, context)
    if user is None:
        return

    rows = await run_db(database.list_subscriptions, user["id"])
    if not rows:
        await update.effective_message.reply_text(MESSAGES["no_subscriptions"])
        return

    await update.effective_message.reply_text(header, reply_markup=picker_keyboard(prefix, rows))


async def cmd_list(update, context):
    user = await require_user(update, context)
    if user is None:
        return
    rows = await run_db(database.list_subscriptions, user["id"])
    if not rows:
        await update.effective_message.reply_text(MESSAGES["no_subscriptions"])
        return
    text, markup = hub_view(rows, context.application.bot_data["rules"])
    await update.effective_message.reply_text(text, reply_markup=markup)


async def load_settings(user_id, rules):
    """The early-access sites (with their website ids) and the user's tiers, for /settings."""
    entries = []
    for domain in rules:
        if timing_capable(rules, domain) and rules[domain].get("mode") in ("html", "html_li_block"):
            web_id = await run_db(database.get_or_create_website, domain)
            entries.append({"web_id": web_id, "domain": domain})
    tiers = await run_db(database.list_user_tiers, user_id) if entries else {}
    return entries, tiers


async def cmd_settings(update, context):
    user = await require_user(update, context)
    if user is None:
        return
    rules = context.application.bot_data["rules"]
    entries, tiers = await load_settings(user["id"], rules)
    text, markup = settings_view(entries, tiers, rules)
    await update.effective_message.reply_text(text, reply_markup=markup)


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


async def edit_chunked(query, text, markup):
    """Edit the tapped message with a long text: the first chunk replaces it, the rest follow as new
    messages, and the buttons go on the last one so they sit under the end of the list."""
    chunks = split_text(text)
    if len(chunks) == 1:
        await edit(query, chunks[0], markup)
        return
    await edit(query, chunks[0], None)
    for chunk in chunks[1:-1]:
        await query.message.reply_text(chunk)
    await query.message.reply_text(chunks[-1], reply_markup=markup)


async def known_early_access_site(web_id, rules):
    """The website's domain if web_id is a real site that has early access, else None."""
    if web_id is None:
        return None
    domain = await run_db(database.get_website_domain, web_id)
    return domain if domain is not None and timing_capable(rules, domain) else None


async def on_callback(update, context):
    """One router for every inline button (formats are listed in the module docstring)."""
    query = update.callback_query
    await query.answer()

    parts = (query.data or "").split(":")
    action = parts[0]
    rules = context.application.bot_data["rules"]

    if action == "no":
        await edit(query, MESSAGES["unchanged"])
        return

    if action == "close":
        try:
            await query.message.delete()
        except Exception:
            await edit(query, MESSAGES["closed"])
        return

    user = await run_db(database.get_user, update.effective_user.id)
    if user is None:
        await edit(query, MESSAGES["need_start"])
        return

    if action == "stop":
        await handle_stop_action(query, user, parts[1] if len(parts) > 1 else "")
        return

    if action == "hub":
        rows = await run_db(database.list_subscriptions, user["id"])
        if not rows:
            await edit(query, MESSAGES["no_subscriptions"])
            return
        text, markup = hub_view(rows, rules)
        await edit(query, text, markup)
        return

    if action in ("site", "tier", "tog"):
        web_id = parse_uuid(parts[1]) if len(parts) > 1 else None
        if web_id is None:
            await edit(query, MESSAGES["not_found"])
        elif action == "site":
            await handle_site(query, user, web_id, rules)
        elif action == "tier":
            await handle_tier(query, user, web_id, parts[2] if len(parts) > 2 else "", rules)
        else:
            await handle_toggle(query, user, web_id, rules)
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

    title = title_of(row)

    if action in ("nov", "m"):
        text, markup = novel_view(row, rules)
        await edit(query, text, markup)

    elif action == "tim":
        if not timing_capable(rules, row.get("web_domain_name")):
            await edit(query, MESSAGES["not_found"])
            return
        tier = await run_db(database.get_user_tier, user["id"], row.get("web_id")) or "standard"
        text, markup = timing_view(row, rules, tier)
        await edit(query, text, markup)

    elif action == "tset":
        code = parts[2] if len(parts) > 2 else ""
        if code not in TIMING_CODES or not timing_capable(rules, row.get("web_domain_name")):
            await edit(query, MESSAGES["not_found"])
            return
        timing = TIMING_CODES[code]
        changed = await run_db(database.set_alert_timing, user["id"], subscription_id, timing)
        if not changed:
            await edit(query, MESSAGES["not_found"])
            return
        row = dict(row, alert_timing=timing)
        tier = await run_db(database.get_user_tier, user["id"], row.get("web_id")) or "standard"
        text, markup = timing_view(row, rules, tier, saved=True)
        await edit(query, text, markup)

    elif action == "st":
        await edit(query, fmt("choose_status", title=title), status_keyboard(subscription_id))

    elif action == "ss":
        try:
            status = STATUS_CHOICES[int(parts[2])]
        except (IndexError, ValueError):
            await edit(query, MESSAGES["not_found"])
            return
        changed = await run_db(database.set_subscription_status, user["id"], subscription_id, status)
        await edit(query, fmt("status_changed", title=title, status=status) if changed else MESSAGES["not_found"])

    elif action == "rm":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Yes, remove", callback_data=f"ry:{subscription_id}")],
            [InlineKeyboardButton("No", callback_data="no")],
        ])
        await edit(query, fmt("confirm_remove", title=title), keyboard)

    elif action == "ry":
        removed = await run_db(database.remove_subscription, user["id"], subscription_id)
        await edit(query, fmt("removed", title=title) if removed else MESSAGES["not_found"])

    else:
        await edit(query, MESSAGES["not_found"])


async def handle_site(query, user, web_id, rules):
    """The novels the caller follows on one site. The site must be one of the caller's own."""
    rows = await run_db(database.list_subscriptions, user["id"])
    group = next((g for g in group_by_site(rows, rules) if g["web_id"] == web_id), None)
    if group is None:
        await edit(query, MESSAGES["not_found"])
        return
    text, markup = site_view(group)
    await edit_chunked(query, text, markup)


async def handle_tier(query, user, web_id, code, rules):
    """The answer to the tier question after /add. The website must be a real early-access site."""
    tier = TIER_CODES.get(code)
    domain = await known_early_access_site(web_id, rules)
    if tier is None or domain is None:
        await edit(query, MESSAGES["not_found"])
        return
    await run_db(database.set_user_tier, user["id"], web_id, tier)
    saved = fmt("tier_saved", label=tier_label(rules, domain, tier))
    original = without_question(getattr(query.message, "text_html", None))
    await edit(query, (original + "\n\n" + saved) if original else saved)


async def handle_toggle(query, user, web_id, rules):
    """/settings: flip the caller's tier for one early-access site and redraw the page."""
    domain = await known_early_access_site(web_id, rules)
    if domain is None:
        await edit(query, MESSAGES["not_found"])
        return
    current = await run_db(database.get_user_tier, user["id"], web_id) or "standard"
    await run_db(database.set_user_tier, user["id"], web_id, "member" if current == "standard" else "standard")
    entries, tiers = await load_settings(user["id"], rules)
    text, markup = settings_view(entries, tiers, rules)
    await edit(query, text, markup)


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
    # the command menu is a nicety: a transient Telegram error here must not stop the bot starting
    try:
        await application.bot.set_my_commands([BotCommand(name, description) for name, description in BOT_COMMANDS])
    except Exception:
        logger.warning("could not publish the command menu; continuing without it", exc_info=True)
    STATUS.set_running(True)


async def post_shutdown(application):
    STATUS.set_running(False)


def build_application(token, rules):
    application = (
        Application.builder()
        .token(token)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["rules"] = rules
    application.bot_data["limiter"] = TokenBucketLimiter(capacity=5, refill_per_second=0.5, warn_cooldown=10)

    application.add_handler(TypeHandler(Update, rate_gate), group=-1)

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("add", cmd_add))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("settings", cmd_settings))
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


def health_port_from_env():
    """The port for the health endpoint, or None to open no port.

    HEALTH_PORT wins; otherwise PORT, which hosting platforms such as Render set for web
    services and expect the app to listen on. Neither set (a normal local run) = no port."""
    name, raw = None, ""
    for candidate in ("HEALTH_PORT", "PORT"):
        value = os.getenv(candidate, "").strip()
        if value:
            name, raw = candidate, value
            break
    if name is None:
        return None
    try:
        port = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if not 0 < port < 65536:
        raise ValueError(f"{name} must be between 1 and 65535, got {port}")
    return port


def main():
    configure_logging()
    application = build_application(get_token(), load_rules())

    port = health_port_from_env()
    if port is not None:
        start_health_server(STATUS, port)
        logger.info("health endpoint listening on port %s", port)

    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()