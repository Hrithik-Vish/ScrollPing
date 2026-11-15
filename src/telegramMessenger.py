"""telegramMessenger.py - sends ScrollPing chapter alerts through Telegram.

Public entry point: send_all(batches, delivery, rules=None, now=None) -> summary

    batches  = {telegram_id: [row, ...]}     rows claimed from the queue (main.group_claimed)
    row      = {"notification_id", "novel_name", "display_title", "web_domain_name", "chapter",
                "chapter_url", "access_state", "unlock_at", ...}
    delivery = object with ack_sent(ids), release(ids, error_text), mark_dead(ids, error_text),
               blocked(chat_id, ids)          (main.QueueDelivery; tests pass a recorder)
    rules    = site_parse_rules.json contents (only used for each site's display_name)
    now      = a datetime, only for the "free in ~5h" badge (defaults to the current time)

    summary  = {"sent": rows delivered, "blocked": users who blocked the bot,
                "bad_request": rows given up on, "failed": rows released after the retries ran out,
                "retried": rows delivered on attempt 2 or later}        (counts only)

Rules this file follows:
  * No DB access, no import from main, no work at import time (the token is read inside
    get_token(), never at module level). Everything it needs to tell the database goes through
    `delivery`, one acknowledgement per outcome, right after that outcome (not at the end).
  * InvalidToken is NEVER caught here, except to re-raise. It is a run-level problem (every later
    send would fail too), so it propagates up to run(). Rows stay 'sending' and are recovered later.
  * send_one() only classifies what happened. It never sleeps or retries. All retry policy lives in
    send_with_flood_wait() and _Run.

Data flow:

    send_all
      |- get_token()                          fail early if no token
      '- asyncio.run(_send_all_async)
           |- Bot + _preflight()              bad token fails before any send
           |- per user: build_message -> per chunk: send -> settle
           |      sent        -> delivery.ack_sent(ids)
           |      blocked     -> delivery.blocked(chat_id, every unsettled id), stop this user
           |      bad_request -> one message per row (HTML) -> plain text -> delivery.mark_dead
           |      transient   -> retry rounds -> delivery.release(ids, category)
           '- return summary
"""

import asyncio
import html
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

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

from src import scheduling
from src.novel_links import title_for

# --------------------------------------------------------------------------
# Tunable constants (tests patch these so they run instantly)
# --------------------------------------------------------------------------

# Telegram's hard limit is 4096 characters (counted after HTML parsing).
# Raw HTML with tags is longer than the parsed text, so staying under 4000
# on the raw string is a safe margin.
MAX_MESSAGE_LEN = 4000
MAX_NAME_LEN = 200           # titles longer than this are cut with an ellipsis
MAX_URL_LEN = 1000           # a longer URL is not linked (it could never fit a message line)

SEND_SPACING = 0.05          # minimum seconds between ANY two sends (about 20 per second)
CHUNK_PAUSE = 1              # pause between chunks to the SAME user (~1 msg/s/chat)

MAX_FLOOD_RETRIES = 2        # extra attempts after a RetryAfter, inline
MAX_FLOOD_WAIT = 60          # never sleep longer than this inline (seconds)
MAX_RETRY_ROUNDS = 3         # passes over the deferred retry queue
ROUND_DELAY = 5              # pause before each retry round (seconds)

HEADER = "\U0001F4DA New chapters"

# Statuses that are worth trying again later. Everything else is final.
RETRYABLE = ("network", "rate_limited", "unknown")


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


def _display_title(row):
    """The title to show (scraped title, else a guess from the slug), cut to MAX_NAME_LEN with an
    ellipsis. The scraped title is read uncut here: title_for would silently truncate it first."""
    title = " ".join(str(row.get("display_title") or "").split()) or title_for(row)
    if len(title) > MAX_NAME_LEN:
        title = title[: MAX_NAME_LEN - 1] + "\u2026"
    return title


def _safe_url(row):
    """The chapter URL if it is a plain http(s) link of sane length, else None."""
    url = str(row.get("chapter_url") or "").strip()
    if not url or len(url) > MAX_URL_LEN:
        return None
    if urlparse(url).scheme.lower() not in ("http", "https"):
        return None
    return url


def _chapter_of(row):
    return row.get("chapter") if row.get("chapter") is not None else row.get("latest_chap")


def _format_line(row, now):
    """One novel -> one HTML line: bullet, linked title, chapter number, badge."""
    # Text nodes only need & < > escaped; quotes matter only inside the href (below). Leaving them
    # alone also avoids numeric entities such as &#x27; in every apostrophe.
    name = html.escape(_display_title(row), quote=False)
    url = _safe_url(row)
    if url:
        # quote=True also escapes " so the URL cannot break out of href="..."
        title = f'<a href="{html.escape(url, quote=True)}">{name}</a>'
    else:
        title = name
    badge = scheduling.badge_for(row, now)
    return f"\u2022 {title} \u2014 Ch. {_format_chapter(_chapter_of(row))}{badge}"


def _plain_text(row, now):
    """The last-resort version of one row: no markup at all, the URL on its own line."""
    text = f"{_display_title(row)} \u2014 Ch. {_format_chapter(_chapter_of(row))}{scheduling.badge_for(row, now)}"
    url = str(row.get("chapter_url") or "").strip()
    return f"{text}\n{url}" if url else text


def _header(index=None, total=None):
    if total is None or total <= 1:
        return f"<b>{HEADER}</b>"
    return f"<b>{HEADER} ({index}/{total})</b>"


def _site_name(domain, rules):
    name = ((rules or {}).get(domain) or {}).get("display_name")
    if name and str(name).strip():
        return str(name).strip()
    return urlparse(str(domain or "")).netloc or str(domain or "") or "Unknown site"


def _sort_key(row):
    try:
        chapter = float(_chapter_of(row))
    except (TypeError, ValueError):
        chapter = 0.0
    return (_display_title(row).casefold(), chapter)


def build_message(rows, rules, now):
    """Turn one user's claimed rows into message chunks: [{"text": str, "row_ids": [id, ...]}].

    Grouped by site (sites sorted by display name, novels by title then chapter). Splits only on
    whole-line boundaries so a tag is never cut, and repeats the site heading when a site's list
    continues in the next chunk. Chunks are numbered "(1/3)" only when there is more than one.
    Returns [] for empty input.
    """
    if not rows:
        return []

    by_site = {}
    for row in rows:
        by_site.setdefault(row.get("web_domain_name"), []).append(row)
    sites = sorted(((_site_name(domain, rules), domain) for domain in by_site),
                   key=lambda s: (s[0].casefold(), str(s[1])))

    # Reserve room for the longest possible numbered header, plus a newline.
    budget = MAX_MESSAGE_LEN - len(_header(999, 999)) - 1

    chunks = []                      # each: {"lines": [...], "ids": [...]}
    current = {"lines": [], "ids": []}
    length = 0

    def flush():
        nonlocal current, length
        chunks.append(current)
        current, length = {"lines": [], "ids": []}, 0

    for name, domain in sites:
        heading = f"\n<b>{html.escape(name, quote=False)}</b>"       # the leading newline makes the blank line
        needs_heading = True
        for row in sorted(by_site[domain], key=_sort_key):
            line = _format_line(row, now)
            cost = len(line) + 1 + ((len(heading) + 1) if needs_heading else 0)
            if current["lines"] and length + cost > budget:
                flush()
                needs_heading = True
                cost = len(line) + 1 + len(heading) + 1
            if needs_heading:
                current["lines"].append(heading)
                needs_heading = False
            current["lines"].append(line)
            if row.get("notification_id") is not None:
                current["ids"].append(row["notification_id"])
            length += cost
    if current["lines"]:
        flush()

    total = len(chunks)
    return [
        {"text": _header(i, total) + "\n" + "\n".join(chunk["lines"]), "row_ids": chunk["ids"]}
        for i, chunk in enumerate(chunks, start=1)
    ]


# --------------------------------------------------------------------------
# 3. One send attempt (classifies, never sleeps, never retries)
# --------------------------------------------------------------------------

def _to_seconds(value):
    """RetryAfter.retry_after may be an int or a timedelta depending on version."""
    if isinstance(value, timedelta):
        value = value.total_seconds()
    return max(1, math.ceil(float(value)))


async def send_one(bot, chat_id, text, parse_mode=ParseMode.HTML, disable_notification=False):
    """Make ONE attempt to send `text`. Returns (status, detail).

    status is one of: "sent", "blocked", "bad_request", "rate_limited"
    (detail = seconds to wait), "network", "unknown".

    ORDER OF THE except CLAUSES MATTERS. In python-telegram-bot, BadRequest is
    a subclass of NetworkError, and TimedOut is too. So the specific ones must
    come before NetworkError, otherwise a malformed message would be treated
    as a flaky connection and retried for nothing.
    """
    kwargs = {
        "chat_id": chat_id,
        "text": text,
        "link_preview_options": LinkPreviewOptions(is_disabled=True),
        "disable_notification": bool(disable_notification),
    }
    if parse_mode:
        kwargs["parse_mode"] = parse_mode
    try:
        await bot.send_message(**kwargs)
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


async def send_with_flood_wait(bot, chat_id, text, parse_mode=ParseMode.HTML,
                               disable_notification=False, pace=None):
    """send_one, but a rate-limit answer is waited out and retried inline.

    Telegram's flood control applies to the whole bot, so the right response
    is to pause right here rather than queue the message for later. Total
    attempts are at most 1 + MAX_FLOOD_RETRIES, and we never sleep longer
    than MAX_FLOOD_WAIT. If the limit is still active after that, the
    "rate_limited" status is returned so the caller can queue it.
    `pace` (an async callable) runs before every attempt to keep sends spaced out.
    """
    async def attempt():
        if pace:
            await pace()
        return await send_one(bot, chat_id, text, parse_mode, disable_notification)

    status, detail = await attempt()
    waits = 0
    while (
        status == "rate_limited"
        and waits < MAX_FLOOD_RETRIES
        and detail <= MAX_FLOOD_WAIT
    ):
        await asyncio.sleep(detail)
        waits += 1
        status, detail = await attempt()
    return status, detail


# --------------------------------------------------------------------------
# 4. Bookkeeping: one object per run, one Job per message
# --------------------------------------------------------------------------

def new_summary():
    return {"sent": 0, "blocked": 0, "bad_request": 0, "failed": 0, "retried": 0}


def _now():
    """Monotonic clock for the send spacing (tests replace it)."""
    return asyncio.get_running_loop().time()


@dataclass
class Job:
    """One message to deliver and the queue rows it carries."""
    chat_id: int
    row_ids: list
    text: str
    rows: list = field(default_factory=list)      # the claimed rows (needed for the fallbacks)
    parse_mode: object = ParseMode.HTML
    notify: bool = True                            # False for chunks 2+ (silent)
    stage: str = "chunk"                           # "chunk" -> "row_html" -> "row_plain"
    attempts: int = 1
    category: str = ""                             # last transient failure category


class _Run:
    """The state of one send_all call. Every outcome is acknowledged through `delivery` at once."""

    def __init__(self, bot, delivery, rules, now):
        self.bot, self.delivery, self.rules, self.now = bot, delivery, rules, now
        self.summary = new_summary()
        self.remaining = {}          # chat_id -> ids not yet acknowledged one way or the other
        self.blocked = set()         # chat ids that blocked the bot during this run
        self.retry = []              # Jobs waiting for a retry round
        self._last_send = None

    # ---- pacing
    async def pace(self):
        """At least SEND_SPACING seconds between any two sends."""
        now = _now()
        if self._last_send is not None:
            wait = SEND_SPACING - (now - self._last_send)
            if wait > 0:
                await asyncio.sleep(wait)
                now = _now()
        self._last_send = now

    async def attempt(self, job):
        return await send_with_flood_wait(
            self.bot, job.chat_id, job.text, job.parse_mode,
            disable_notification=not job.notify, pace=self.pace,
        )

    # ---- outcomes
    def _settled(self, job):
        self.remaining.get(job.chat_id, set()).difference_update(job.row_ids)

    def ack(self, job):
        self.delivery.ack_sent(job.row_ids)
        self.summary["sent"] += len(job.row_ids)
        if job.attempts > 1:
            self.summary["retried"] += len(job.row_ids)
        self._settled(job)

    async def block(self, job):
        ids = sorted(self.remaining.get(job.chat_id, set()) | set(job.row_ids), key=str)
        self.delivery.blocked(job.chat_id, ids)
        self.summary["blocked"] += 1
        self.blocked.add(job.chat_id)
        self.remaining[job.chat_id] = set()

    def dead(self, job):
        self.delivery.mark_dead(job.row_ids, "bad_request")
        self.summary["bad_request"] += len(job.row_ids)
        self._settled(job)

    async def settle(self, job, status, detail):
        if status == "sent":
            self.ack(job)
        elif status == "blocked":
            await self.block(job)
        elif status == "bad_request":
            await self.fallback(job)
        else:                                         # network / rate_limited / unknown: try again later
            job.category = status
            self.retry.append(job)

    async def fallback(self, job):
        """A message Telegram refused: first one HTML message per row, then plain text, then give up."""
        if job.stage == "chunk" and len(job.rows) > 1:
            for row in job.rows:
                if job.chat_id in self.blocked:
                    break
                single = Job(
                    job.chat_id, [row["notification_id"]],
                    build_message([row], self.rules, self.now)[0]["text"],
                    rows=[row], notify=job.notify, stage="row_html", attempts=job.attempts,
                )
                status, detail = await self.attempt(single)
                await self.settle(single, status, detail)
        elif job.stage in ("chunk", "row_html"):
            row = job.rows[0]
            plain = Job(
                job.chat_id, job.row_ids, _plain_text(row, self.now),
                rows=job.rows, parse_mode=None, notify=job.notify, stage="row_plain", attempts=job.attempts,
            )
            status, detail = await self.attempt(plain)
            await self.settle(plain, status, detail)
        else:                                         # plain text was refused too
            self.dead(job)

    # ---- the retry rounds
    async def retry_rounds(self):
        """Bounded rounds over the transient failures; what is still failing afterwards is released."""
        for _ in range(MAX_RETRY_ROUNDS):
            self.retry = [j for j in self.retry if j.chat_id not in self.blocked]
            if not self.retry:
                break
            await asyncio.sleep(ROUND_DELAY)
            pending, self.retry = self.retry, []
            for job in pending:
                if job.chat_id in self.blocked:
                    continue
                job.attempts += 1
                status, detail = await self.attempt(job)
                await self.settle(job, status, detail)

        for job in self.retry:
            if job.chat_id in self.blocked:
                continue
            self.delivery.release(job.row_ids, job.category)
            self.summary["failed"] += len(job.row_ids)
            self._settled(job)
        self.retry = []

    # ---- one user
    async def send_user(self, chat_id, rows):
        by_id = {row["notification_id"]: row for row in rows if row.get("notification_id") is not None}
        self.remaining[chat_id] = set(by_id)
        for index, chunk in enumerate(build_message(rows, self.rules, self.now)):
            if chat_id in self.blocked:
                break                                  # no point sending this user's remaining chunks
            if index > 0:
                await asyncio.sleep(CHUNK_PAUSE)
            job = Job(chat_id, chunk["row_ids"], chunk["text"],
                      rows=[by_id[i] for i in chunk["row_ids"]], notify=(index == 0))
            status, detail = await self.attempt(job)
            await self.settle(job, status, detail)


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


async def _send_all_async(batches, delivery, rules, now, token):
    async with Bot(token) as bot:  # one Bot, one event loop for the whole run
        await _preflight(bot)
        run = _Run(bot, delivery, rules, now)
        for chat_id, rows in batches.items():
            await run.send_user(chat_id, rows)
        await run.retry_rounds()
    return run.summary


def send_all(batches, delivery, rules=None, now=None):
    """Send every user their message and acknowledge each outcome through `delivery`.
    Returns the counts-only summary described at the top of this file.

    Raises RuntimeError if the token is missing, and InvalidToken if Telegram
    rejects it. run() is expected to handle both.
    """
    token = get_token()  # first thing: without a token nothing can be sent
    if not batches:
        return new_summary()
    return asyncio.run(_send_all_async(batches, delivery, rules or {}, now or datetime.now(timezone.utc), token))


# --------------------------------------------------------------------------
# 6. Admin messages (alerts and the daily digest)
# --------------------------------------------------------------------------

def _strip_tags(text):
    """HTML -> plain text, for the one fallback attempt if Telegram refuses the HTML."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


async def _send_admin_async(chat_ids, text, token):
    async with Bot(token) as bot:
        await _preflight(bot)
        pacer = _Run(bot, None, {}, None)          # only used for the send spacing
        delivered, failed = 0, 0
        pending = list(chat_ids)
        for round_number in range(1 + MAX_RETRY_ROUNDS):
            if not pending:
                break
            if round_number:
                await asyncio.sleep(ROUND_DELAY)
            retry = []
            for chat_id in pending:
                status, _ = await send_with_flood_wait(bot, chat_id, text, pace=pacer.pace)
                if status == "bad_request":        # malformed HTML: one more try as plain text
                    status, _ = await send_with_flood_wait(bot, chat_id, _strip_tags(text), parse_mode=None, pace=pacer.pace)
                if status == "sent":
                    delivered += 1
                elif status in RETRYABLE:
                    retry.append(chat_id)
                else:                              # blocked, or refused even as plain text
                    failed += 1
            pending = retry
        failed += len(pending)
    return {"delivered": delivered, "failed": failed}


def send_admin(chat_ids, text):
    """Send one HTML text to each admin chat, with the same retries as user messages.
    Returns {"delivered": admins reached, "failed": admins not reached} (counts only). Callers act
    only on delivered > 0: an alert is marked as reported, and the digest marked as sent, only then.
    Raises RuntimeError without a token and InvalidToken for a rejected one, like send_all."""
    token = get_token()
    ids = list(dict.fromkeys(chat_ids or []))
    if not ids:
        return {"delivered": 0, "failed": 0}
    return asyncio.run(_send_admin_async(ids, text, token))
