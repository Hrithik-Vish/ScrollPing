"""telegramMessenger.py - sends ScrollPing chapter alerts through Telegram.

Public entry point: send_all(user_tele_dict) -> summary

    user_tele_dict = {telegram_id: [row, ...]}   (output of group_subscribers)
    row            = {"novel_name": ..., "latest_chap": ..., "chapter_url": ...}
                     (chapter_url must already be an absolute URL)

Rules this file follows:
  * No DB access, no import from main, no work at import time
    (the token is read inside get_token(), never at module level).
  * InvalidToken is NEVER caught here, except to re-raise. It is a run-level
    problem (every later send would fail too), so it propagates up to run().
  * send_one() only classifies what happened. It never sleeps or retries.
    All retry policy lives in send_with_flood_wait() and retry_failed().

Data flow:

    send_all
      |- get_token()                         fail early if no token
      '- asyncio.run(_send_all_async)
           |- Bot + _preflight()             bad token fails before any send
           |- per user: build_message -> per chunk:
           |      send_with_flood_wait -> send_one
           |      record_outcome  --> summary / retry_queue
           |- retry_failed                   rounds, leftovers -> summary["failed"]
           '- return summary
"""

import asyncio
import html
import math
import os
from collections import defaultdict
from datetime import timedelta

from dotenv import load_dotenv
from telegram import Bot, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

# --------------------------------------------------------------------------
# Tunable constants (tests patch these so they run instantly)
# --------------------------------------------------------------------------

# Telegram's hard limit is 4096 characters (counted after HTML parsing).
# Raw HTML with tags is longer than the parsed text, so staying under 4000
# on the raw string is a safe margin.
MAX_MESSAGE_LEN = 4000
MAX_NAME_LEN = 200           # novel names longer than this get cut with "..."

BATCH_SIZE = 25              # after this many send attempts...
BATCH_PAUSE = 5              # ...pause this many seconds (global rate safety)
CHUNK_PAUSE = 1              # pause between chunks to the SAME user (~1 msg/s/chat)

MAX_FLOOD_RETRIES = 2        # extra attempts after a RetryAfter, inline
MAX_FLOOD_WAIT = 60          # never sleep longer than this inline (seconds)
MAX_RETRY_ROUNDS = 3         # passes over the deferred retry queue
ROUND_DELAY = 5              # pause before each retry round (seconds)

HEADER = "New chapters"

# Statuses that are worth trying again later. Everything else is final.
RETRYABLE = ("network", "rate_limited")


# --------------------------------------------------------------------------
# 1. Token
# --------------------------------------------------------------------------

def get_token():
    """Return the bot token from the environment, or raise a clear error.

    load_dotenv() does not override variables that are already set, so on
    GitHub Actions (real secrets) it is a harmless no-op, and locally it
    loads .env. Kept as its own function so tests can set/unset the env var.
    """
    load_dotenv()
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing or empty. Set it in .env (local) "
            "or in the environment / repository secrets (CI)."
        )
    return token


# --------------------------------------------------------------------------
# 2. Building the message text (pure: no network, no I/O)
# --------------------------------------------------------------------------

def _format_chapter(value):
    """631.0 -> '631', 12.5 -> '12.5', None -> '?', anything odd -> str()."""
    if value is None:
        return "?"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return str(number)


def _format_line(row):
    """One novel -> one HTML line: linked title plus chapter number."""
    name = str(row.get("novel_name") or "Unknown title")
    if len(name) > MAX_NAME_LEN:
        name = name[: MAX_NAME_LEN - 1] + "\u2026"
    name = html.escape(name)  # titles contain <, &, quotes...

    url = row.get("chapter_url")
    if url:
        # quote=True also escapes " so the URL cannot break out of href="..."
        title = f'<a href="{html.escape(str(url), quote=True)}">{name}</a>'
    else:
        title = name

    return f"{title} \u2014 Ch. {_format_chapter(row.get('latest_chap'))}"


def _header(index=None, total=None):
    if total is None or total <= 1:
        return f"<b>{HEADER}</b>"
    return f"<b>{HEADER} ({index}/{total})</b>"


def build_message(rows):
    """Turn one user's rows into a list of HTML message chunks.

    Splits only on whole-line boundaries, so an <a> tag is never cut in half.
    Chunks are numbered "(1/2)" only when there is more than one. Returns []
    for empty input.
    """
    if not rows:
        return []

    lines = [_format_line(row) for row in rows]

    # Reserve room for the longest possible numbered header, plus a newline.
    budget = MAX_MESSAGE_LEN - len(_header(999, 999)) - 1

    groups, current, current_len = [], [], 0
    for line in lines:
        extra = len(line) + (1 if current else 0)
        if current and current_len + extra > budget:
            groups.append(current)
            current, current_len = [], 0
            extra = len(line)
        current.append(line)
        current_len += extra
    groups.append(current)

    total = len(groups)
    return [
        _header(i, total) + "\n" + "\n".join(group)
        for i, group in enumerate(groups, start=1)
    ]


# --------------------------------------------------------------------------
# 3. One send attempt (classifies, never sleeps, never retries)
# --------------------------------------------------------------------------

def _to_seconds(value):
    """RetryAfter.retry_after may be an int or a timedelta depending on version."""
    if isinstance(value, timedelta):
        value = value.total_seconds()
    return max(1, math.ceil(float(value)))


async def send_one(bot, chat_id, text):
    """Make ONE attempt to send `text`. Returns (status, detail).

    status is one of: "sent", "blocked", "bad_request", "rate_limited"
    (detail = seconds to wait), "network", "unknown".

    ORDER OF THE except CLAUSES MATTERS. In python-telegram-bot, BadRequest is
    a subclass of NetworkError, and TimedOut is too. So the specific ones must
    come before NetworkError, otherwise a malformed message would be treated
    as a flaky connection and retried for nothing.
    """
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return "sent", None
    except InvalidToken:
        raise  # run-level problem: let it escape to run()
    except RetryAfter as exc:
        return "rate_limited", _to_seconds(exc.retry_after)
    except Forbidden as exc:  # user blocked the bot / deleted the chat
        return "blocked", str(exc)
    except BadRequest as exc:  # chat not found, bad HTML, text too long, ...
        return "bad_request", str(exc)
    except TimedOut as exc:
        return "network", str(exc)
    except NetworkError as exc:
        return "network", str(exc)
    except TelegramError as exc:  # anything else Telegram-shaped
        return "unknown", str(exc)


async def send_with_flood_wait(bot, chat_id, text):
    """send_one, but a rate-limit answer is waited out and retried inline.

    Telegram's flood control applies to the whole bot, so the right response
    is to pause right here rather than queue the message for later. Total
    attempts are at most 1 + MAX_FLOOD_RETRIES, and we never sleep longer
    than MAX_FLOOD_WAIT. If the limit is still active after that, the
    "rate_limited" status is returned so the caller can queue it.
    """
    status, detail = await send_one(bot, chat_id, text)
    waits = 0
    while (
        status == "rate_limited"
        and waits < MAX_FLOOD_RETRIES
        and detail <= MAX_FLOOD_WAIT
    ):
        await asyncio.sleep(detail)
        waits += 1
        status, detail = await send_one(bot, chat_id, text)
    return status, detail


# --------------------------------------------------------------------------
# 4. Bookkeeping
# --------------------------------------------------------------------------

def new_summary():
    return {"sent": 0, "blocked": [], "bad_request": [], "failed": []}


def record_outcome(summary, retry_queue, chat_id, text, status, detail, attempts=1):
    """Route one send result to the right place. Pure bookkeeping.

    sent                    -> summary["sent"] += 1
    blocked / bad_request   -> summary (final, never retried)
    network / rate_limited  -> retry_queue[status] (retried later)
    anything else           -> summary["failed"]
    """
    if status == "sent":
        summary["sent"] += 1
    elif status == "blocked":
        if chat_id not in summary["blocked"]:
            summary["blocked"].append(chat_id)
    elif status == "bad_request":
        summary["bad_request"].append((chat_id, detail))
    elif status in RETRYABLE:
        retry_queue[status].append(
            {"chat_id": chat_id, "text": text, "attempts": attempts, "detail": detail}
        )
    else:
        summary["failed"].append((chat_id, status, detail))


async def retry_failed(bot, retry_queue, summary):
    """Work through the deferred queue in bounded rounds.

    Each round waits ROUND_DELAY, then resends every queued item. Items that
    succeed are counted, final failures go to the summary, and retryable
    failures are queued again. When the rounds run out, whatever is still
    queued is moved to summary["failed"]. InvalidToken propagates.
    """
    for _ in range(MAX_RETRY_ROUNDS):
        if not any(retry_queue.values()):
            break
        await asyncio.sleep(ROUND_DELAY)

        pending = [item for items in retry_queue.values() for item in items]
        retry_queue.clear()
        for item in pending:
            status, detail = await send_with_flood_wait(
                bot, item["chat_id"], item["text"]
            )
            record_outcome(
                summary,
                retry_queue,
                item["chat_id"],
                item["text"],
                status,
                detail,
                attempts=item["attempts"] + 1,
            )

    for category, items in retry_queue.items():
        for item in items:
            summary["failed"].append((item["chat_id"], category, item["detail"]))
    retry_queue.clear()


# --------------------------------------------------------------------------
# 5. The run
# --------------------------------------------------------------------------

async def _preflight(bot):
    """Cheap get_me() call so a bad token fails before any message is sent.

    Only InvalidToken matters here. Other Telegram errors (a network blip) are
    just reported; the real sends will classify them properly.
    """
    try:
        await bot.get_me()
    except InvalidToken:
        raise
    except TelegramError as exc:
        print(f"[telegram] preflight get_me failed, continuing anyway: {exc}")


async def _send_all_async(user_tele_dict, token):
    summary = new_summary()
    retry_queue = defaultdict(list)

    async with Bot(token) as bot:  # one Bot, one event loop for the whole run
        await _preflight(bot)

        attempted = 0
        for chat_id, rows in user_tele_dict.items():
            for index, text in enumerate(build_message(rows)):
                if index > 0:
                    await asyncio.sleep(CHUNK_PAUSE)
                if attempted and attempted % BATCH_SIZE == 0:
                    await asyncio.sleep(BATCH_PAUSE)

                status, detail = await send_with_flood_wait(bot, chat_id, text)
                attempted += 1
                record_outcome(summary, retry_queue, chat_id, text, status, detail)

                if status == "blocked":
                    break  # no point sending this user's remaining chunks

        await retry_failed(bot, retry_queue, summary)

    return summary


def send_all(user_tele_dict):
    """Send every user their compiled message. Returns the summary dict:

        {"sent": int,
         "blocked": [chat_id, ...],
         "bad_request": [(chat_id, detail), ...],
         "failed": [(chat_id, category, detail), ...]}

    Raises RuntimeError if the token is missing, and InvalidToken if Telegram
    rejects it. run() is expected to handle InvalidToken.
    """
    token = get_token()  # first thing: without a token nothing can be sent
    return asyncio.run(_send_all_async(user_tele_dict, token))