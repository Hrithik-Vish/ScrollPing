"""Pure decisions about WHO is notified, WHEN, and with what badge (spec sections 8 and 10.3).

Nothing in here touches the network or the database, and no function reads the clock: `now` is always
passed in. main.py is the impure shell that feeds these functions and acts on their answers.

Vocabulary (see the spec):
  access_state  "free" | "locked" | "unknown"     what the site told us about the chapter
  unlock_at     when a locked chapter becomes free (only when the site gives a timestamp), or None
  tier          "standard" | "member"             the user's account level on that site
  alert_timing  "early" | "when_free" | None      per-novel override; None follows the tier
"""

import math
from datetime import datetime, timezone

ALERT_TIMINGS = ("early", "when_free")


def parse_utc(value):
    """datetime or ISO 8601 text (trailing Z accepted) -> aware UTC datetime, or None if unusable.
    A naive datetime is taken to be UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text[-1] in "Zz":
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def resolve_due_at(access_state, unlock_at, alert_timing, tier, now):
    """The moment this recipient should be notified about this chapter (an aware UTC datetime).

    Not locked, or lock state unknown     -> now
    Locked, unlock time unknown or past   -> now (the badge says it is early access)
    Locked, unlock time in the future     -> the user's choice: "early" is now, "when_free" is unlock_at.
                                             With no per-novel override, members default to early and
                                             everybody else waits.
    """
    now = parse_utc(now)
    if access_state != "locked":
        return now
    unlock = parse_utc(unlock_at)
    if unlock is None or unlock <= now:
        return now

    timing = alert_timing if alert_timing in ALERT_TIMINGS else None
    if timing is None:
        timing = "early" if tier == "member" else "when_free"
    return now if timing == "early" else unlock


def build_queue_records(rows, recipients, tiers, now):
    """One queue record per (updated chapter, recipient).

    rows        compile output (spec 7.2) plus "web_id": id, latest_chap, chapter_url, access_state, unlock_at
    recipients  [{"novel_id", "user_id", "alert_timing", "telegram_user_id"}]
    tiers       {(user_id, web_id): "standard" | "member"}; a missing key means standard
    Every record has exactly the same keys (a PostgREST batch insert requires that). Records are
    ordered by row, then recipient; a recipient listed twice for one novel yields one record.
    """
    now = parse_utc(now)
    by_novel = {}
    for recipient in recipients or []:
        by_novel.setdefault(recipient["novel_id"], []).append(recipient)

    records, seen = [], set()
    for row in rows or []:
        if row.get("chapter_url") is None or row.get("latest_chap") is None:
            continue
        access_state = row.get("access_state") or "free"
        unlock = parse_utc(row.get("unlock_at"))
        for recipient in by_novel.get(row["id"], []):
            key = (recipient["user_id"], row["id"], float(row["latest_chap"]))
            if key in seen:
                continue
            seen.add(key)
            tier = (tiers or {}).get((recipient["user_id"], row.get("web_id")), "standard")
            due_at = resolve_due_at(access_state, unlock, recipient.get("alert_timing"), tier, now)
            records.append({
                "user_id": recipient["user_id"],
                "novel_id": row["id"],
                "chapter": float(row["latest_chap"]),
                "chapter_url": row["chapter_url"],
                "access_state": access_state,
                # an unlock time only means something for a locked chapter (free rows can carry an old one)
                "unlock_at": unlock.isoformat() if unlock and access_state == "locked" else None,
                "due_at": due_at.isoformat(),
                "status": "pending",
            })
    return records


def format_remaining(seconds):
    """'<1h' under an hour, 'Nh' (rounded up) under 48 hours, otherwise 'Nd' (whole days)."""
    if seconds < 3600:
        return "<1h"
    hours = math.ceil(seconds / 3600)
    if seconds < 48 * 3600:
        return f"{hours}h"
    return f"{int(seconds // 86400)}d"


def badge_for(row, now):
    """Text appended to a chapter line in the Telegram message ('' for no badge). Computed at SEND time.

    locked, unlock in the future   " 🔒 free in ~{remaining}"
    locked, unlock unknown         " 🔒 early access"
    locked, unlock already passed  ""   (it is free by now)
    free / unknown                 ""
    """
    if (row.get("access_state") or "free") != "locked":
        return ""
    unlock = parse_utc(row.get("unlock_at"))
    if unlock is None:
        return " 🔒 early access"
    remaining = (unlock - parse_utc(now)).total_seconds()
    if remaining <= 0:
        return ""
    return f" 🔒 free in ~{format_remaining(remaining)}"
