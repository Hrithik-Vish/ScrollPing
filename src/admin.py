"""Pure helpers for the admin side of the scraper (spec section 12): who the admins are, when the
daily digest is due, and the text of the alerts and the digest.

Nothing here touches the network, the database or the clock: `now` is always passed in, and main.py
(the impure shell) does the reading, sending and acknowledging.

All times shown to the admin are Indian Standard Time (IST, UTC+05:30), formatted like "09:35 AM".
All text is Telegram HTML; every value that is not literal text is escaped.
"""

import html
import os
import re
from datetime import timedelta, timezone

from src.novel_links import normalize_host
from src.scheduling import parse_utc

IST = timezone(timedelta(hours=5, minutes=30))

DIGEST_HOUR = 23
DIGEST_MINUTE = 35
DIGEST_STATE_KEY = "last_digest_sent_at"
RECENT_ISSUE_WINDOW = timedelta(hours=24)
MAX_ERROR_SHOWN = 300           # characters of an error text shown to the admin

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def esc(value):
    return html.escape(str(value), quote=False)


# ---------------------------------------------------------------- who the admins are

def get_admin_chat_ids(env=None):
    """Telegram ids from ADMIN_TELEGRAM_USER_IDS ("123, 456"). Anything that is not a whole number is
    ignored; duplicates are dropped. An unset or empty variable gives [] (alerts and digest are skipped)."""
    environment = os.environ if env is None else env
    raw = environment.get("ADMIN_TELEGRAM_USER_IDS", "") or ""
    # A strict pattern: "--5" would pass a lstrip("-").isdigit() check and then crash int().
    ids = [int(t) for t in (p.strip() for p in raw.split(",")) if re.fullmatch(r"-?[0-9]+", t)]
    return list(dict.fromkeys(ids))


# ---------------------------------------------------------------- the digest schedule

def latest_digest_target(now_utc):
    """The most recent 23:35 IST that is not in the future: the moment today's digest is due.
    Always the latest one, so a run that starts late (after midnight) still sends yesterday's."""
    now_ist = parse_utc(now_utc).astimezone(IST)
    target = now_ist.replace(hour=DIGEST_HOUR, minute=DIGEST_MINUTE, second=0, microsecond=0)
    return target if now_ist >= target else target - timedelta(days=1)


def digest_due(now_utc, last_sent_iso):
    """True when no digest has been sent since the latest 23:35 IST. A missing or unreadable marker
    counts as never sent (the next successful send overwrites it)."""
    last_sent = parse_utc(last_sent_iso)
    if last_sent is None:
        return True
    return last_sent.astimezone(IST) < latest_digest_target(now_utc)


# ---------------------------------------------------------------- formatting

def format_ist(value, now=None):
    """'09:35 AM' for today (in IST) and '10 Nov, 09:35 AM' for any other day. '?' if unreadable."""
    moment = parse_utc(value)
    if moment is None:
        return "?"
    moment = moment.astimezone(IST)
    clock = f"{moment.hour % 12 or 12:02d}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"
    if now is not None and moment.date() == parse_utc(now).astimezone(IST).date():
        return clock
    return f"{moment.day} {MONTHS[moment.month - 1]}, {clock}"


def site_label(domain, rules):
    name = ((rules or {}).get(domain) or {}).get("display_name")
    if name and str(name).strip():
        return str(name).strip()
    return normalize_host(domain.split("//")[-1].split("/")[0]) if domain else "Unknown site"


def short_error(text):
    text = " ".join(str(text or "").split())
    return (text[: MAX_ERROR_SHOWN - 1] + "\u2026") if len(text) > MAX_ERROR_SHOWN else (text or "unknown")


# ---------------------------------------------------------------- the immediate alert (12.2)

def build_alert(site, rules=None):
    """The message for a site that has failed three or more runs in a row.
    site: a fetch_site_alerts() row (web_domain_name, consecutive_failures, failing_since, last_error)."""
    name = site_label(site.get("web_domain_name"), rules)
    since = f"{format_ist(site['failing_since'])} IST" if site.get("failing_since") else "unknown"
    return (
        "\U0001F6A8 <b>ScrollPing alert</b>\n"
        f"<b>{esc(name)}</b> has failed {esc(site.get('consecutive_failures'))} runs in a row.\n"
        f"Since: {esc(since)}\n"
        f"Error: <code>{esc(short_error(site.get('last_error')))}</code>"
    )


# ---------------------------------------------------------------- the daily digest (12.5)

def _site_line(row, now):
    """(kind, text) for one site; the first matching case wins."""
    error = esc(short_error(row.get("last_error")))
    status = row.get("last_status")
    if status == "failed":
        return "failed", (f"\u274C Currently failing since {esc(format_ist(row.get('failing_since'), now))} "
                          f"({esc(row.get('consecutive_failures'))} runs) \u00B7 {error}")
    failed_at = parse_utc(row.get("last_failed_at"))
    if failed_at is not None and parse_utc(now) - failed_at <= RECENT_ISSUE_WINDOW:
        text = f"\u26A0\uFE0F Issue resolved \u00B7 failed {esc(format_ist(failed_at, now))}"
        if row.get("last_resolved_at"):
            text += f", resolved {esc(format_ist(row['last_resolved_at'], now))}"
        return "resolved", text
    if status == "degraded":
        return "degraded", f"\U0001F7E1 Degraded \u00B7 {error}"
    return "healthy", "\u2705 Healthy"


def build_digest(site_rows, queue_stats, now, rules=None):
    """The daily digest text. site_rows: fetch_site_health_rows() rows; queue_stats: queue_stats() or None."""
    lines = [f"\U0001F4CA <b>ScrollPing daily digest</b> \u00B7 {esc(format_ist(now))} IST", ""]

    rows = sorted(site_rows or [], key=lambda r: site_label(r.get("web_domain_name"), rules).casefold())
    classified = [(row, *_site_line(row, now)) for row in rows]
    if not classified:
        lines.append("No site health recorded yet.")
    elif all(kind == "healthy" for _, kind, _ in classified):
        count = len(classified)
        lines.append(f"\u2705 All {count} sites healthy" if count != 1 else "\u2705 1 site healthy")
    else:
        for row, _, text in classified:
            lines.append(f"\u2022 <b>{esc(site_label(row.get('web_domain_name'), rules))}</b> \u2014 {text}")

    lines.append("")
    if queue_stats:
        lines.append(
            f"\U0001F4EC Last 24h: delivered {esc(queue_stats.get('sent_24h', 0))} \u00B7 "
            f"retried {esc(queue_stats.get('retried_24h', 0))} \u00B7 dead {esc(queue_stats.get('dead_24h', 0))} \u00B7 "
            f"cancelled {esc(queue_stats.get('cancelled_24h', 0))}"
        )
        lines.append(f"Waiting now: {esc(queue_stats.get('pending_now', 0))} \u00B7 in flight: {esc(queue_stats.get('sending_now', 0))}")
    else:
        lines.append("\U0001F4EC Queue numbers are unavailable right now.")
    return "\n".join(lines)
