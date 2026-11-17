# The Target: supabase.table("table_name")

# The Action: .select(), .insert(), .update(), or .delete()

# The Filters (Optional): .eq(), .gt(), .ilike(), etc. (Who are we affecting?)

# The Modifiers (Optional): .order(), .limit(), .single(), etc. (How should the data look?)

# The Trigger: .execute() (Send it to the server!)


import os
import time
from datetime import datetime, timezone
from typing import NamedTuple
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()
url = os.getenv("SUPABASE_URL")
public_key = os.getenv("SUPABASE_PUBLISHABLE_KEY")
secret_key = os.getenv("SUPABASE_SECRET_KEY")

if not url or not public_key or not secret_key:
    raise ValueError("either url or public_key or secret_key does not exists in .env or there is a typo in code")

supabase: Client = create_client (url, secret_key)


def fetch_websites():
    max_attempts = 3

    for attempt in range(1, max_attempts +1):
        print(f"fetching websites, attempt: {attempt}")

        try:
            read_websites = (
                supabase.table("websites").select("id, web_domain_name").execute()
            )
            if read_websites.data:
                websites_list = read_websites.data
                return websites_list
            else:
                print("data fetched was empty")
                return []

        except Exception as e:
            if attempt < max_attempts:
                print(f"attempt: {attempt} failed due to exception: {e}, trying again")
                time.sleep(5)
            else:
                print(f"attempt: {attempt} failed,\nall attempts failed, try again later, exception: {e}")
                return []



def fetch_novels(site_id):
    max_attempts = 2

    for attempt in range(1, max_attempts +1):
        print(f"fetching novels, attempt: {attempt}")

        try:
            read_novels = (
                supabase.table("novels").select("*").eq("web_id", site_id).execute()
            )
            if read_novels.data:
                novels_list = read_novels.data
                return novels_list
            else:
                print("data fetched was empty")
                return []

        except Exception as e:
            if attempt < max_attempts:
                print(f"attempt: {attempt} failed due to exception: {e}, trying again")
                time.sleep(5)
            else:
                print(f"attempt: {attempt} failed,\nall attempts failed, try again later, exception: {e}")
                return []



# =====================================================================
# V1 burn-in, scraper side (spec sections 8 to 12).
#
# Conventions for everything below:
#  - transient failures are retried; after the last attempt the function does NOT raise, it returns
#    an explicit "it failed" value (empty list, 0, None, or ok=False) and prints a counts-only line,
#    so one database hiccup can never stop a whole scrape run. get_app_state is the one exception
#    (see its docstring).
#  - ids are sent to the SQL functions in chunks, so request URLs and bodies stay small.
#  - nothing here decides who is notified or when: that is pure code in main.py (build_queue_records).
# =====================================================================

RETRY_SLEEP_SECONDS = 5
CHUNK_SIZE = 100                 # ids per .in_() filter
RPC_ID_CHUNK = 200               # ids per rpc call
INSERT_BATCH_SIZE = 500          # queue rows per upsert
MAX_ERROR_TEXT = 500
ALERT_FAILURE_THRESHOLD = 3      # consecutive failed runs before the admin is alerted
SITE_STATUSES = ("ok", "degraded", "failed")
QUEUE_RECORD_KEYS = ("user_id", "novel_id", "chapter", "chapter_url", "access_state", "unlock_at", "due_at", "status")


def _chunks(items, size):
    items = list(items)
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _retry(label, call, attempts=3):
    """Run call(); on an exception wait and try again, re-raising after the last attempt."""
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as e:
            if attempt < attempts:
                print(f"{label}: attempt {attempt} failed ({type(e).__name__}: {str(e)[:150]}), trying again")
                time.sleep(RETRY_SLEEP_SECONDS)
            else:
                print(f"{label}: all {attempts} attempts failed ({type(e).__name__}: {str(e)[:150]})")
                raise


def _rows(response):
    """The list of rows of a PostgREST / rpc response ([] when there is none)."""
    data = getattr(response, "data", None)
    if data is None:
        return []
    return data if isinstance(data, list) else [data]


def _scalar(response):
    """The single value a scalar-returning rpc gives back (an int for the ack functions)."""
    data = getattr(response, "data", None)
    if isinstance(data, list):
        if not data:
            return 0
        data = data[0]
        if isinstance(data, dict):
            data = next(iter(data.values()), 0)
    return data if isinstance(data, (int, float)) else 0


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _short_error(error_text):
    if error_text is None:
        return None
    text = " ".join(str(error_text).split())
    return text[:MAX_ERROR_TEXT] or None


# ---- novels ----------------------------------------------------------------------------------

def update_novels_table(updated_novel_list):
    """Write each novel's new chapter (and title, when it has one) in one round trip.
    Returns the ids whose write did NOT happen. That never blocks the alert: the queue rows are
    inserted before this runs (D1), so a failed id only means the next run detects it again."""
    if not updated_novel_list:
        return []
    payload = [
        {
            "id": novel["id"],
            "latest_chap": novel["latest_chap"],
            "chapter_url": novel["chapter_url"],
            "display_title": novel.get("display_title"),
        }
        for novel in updated_novel_list
    ]

    try:
        response = _retry("updating novels table", lambda: supabase.rpc("update_novels_batch", {"items": payload}).execute(), attempts=2)
        updated = {row["updated_id"] for row in _rows(response)}
        return [item["id"] for item in payload if item["id"] not in updated]
    except Exception:
        # The whole batch failed (for example one bad value rejects the call): isolate the bad row.
        failed = []
        for item in payload:
            try:
                got = _rows(supabase.rpc("update_novels_batch", {"items": [item]}).execute())
                if not got:
                    failed.append(item["id"])
            except Exception:
                failed.append(item["id"])
        print(f"novels update fell back to one-by-one, {len(failed)} of {len(payload)} failed")
        return failed


def sync_novel_titles(title_updates):
    """Store scraped titles without touching updated_at (D4). title_updates: [{"id", "display_title"}].
    Fail-soft. Returns the ids that were actually changed."""
    items = [
        {"id": u["id"], "display_title": u["display_title"]}
        for u in (title_updates or [])
        if u.get("id") and u.get("display_title")
    ]
    changed = []
    for chunk in _chunks(items, RPC_ID_CHUNK):
        try:
            response = _retry("syncing novel titles", lambda chunk=chunk: supabase.rpc("sync_novel_titles", {"items": chunk}).execute(), attempts=2)
            changed.extend(row["updated_id"] for row in _rows(response))
        except Exception:
            pass                                   # already printed by _retry; titles are cosmetic
    return changed


# ---- recipients ------------------------------------------------------------------------------

class SubscriberFetch(NamedTuple):
    """recipients: [{"novel_id", "user_id", "alert_timing", "telegram_user_id"}]
    tiers: {(user_id, web_id): "standard" | "member"}  (a missing key means standard)
    failed_novel_ids: novels whose recipients could not be read reliably. The caller must NOT
        advance those novels this run, so the chapter is simply detected again next time."""
    recipients: list
    tiers: dict
    failed_novel_ids: set

    @property
    def ok(self):
        return not self.failed_novel_ids


def fetch_subscribers_with_preferences(novel_id_list, web_id_by_novel=None):
    """Who reads these novels (reading + active users), with their timing override and site tier."""
    novel_ids = list(dict.fromkeys(novel_id_list or []))
    recipients, failed = [], set()

    for chunk in _chunks(novel_ids, CHUNK_SIZE):
        try:
            response = _retry(
                "fetching subscribers",
                lambda chunk=chunk: (
                    supabase.table("user_subscriptions")
                    .select("novel_id, user_id, alert_timing, users!inner(telegram_user_id)")
                    .in_("novel_id", chunk)
                    .eq("reading_status", "reading")
                    .eq("users.status", "active")
                    .execute()
                ),
                attempts=5,
            )
        except Exception:
            failed.update(chunk)
            continue
        for row in _rows(response):
            user = row.get("users") or {}
            if user.get("telegram_user_id") is None:
                continue
            recipients.append({
                "novel_id": row["novel_id"],
                "user_id": row["user_id"],
                "alert_timing": row.get("alert_timing"),
                "telegram_user_id": user["telegram_user_id"],
            })

    tiers = {}
    user_ids = list(dict.fromkeys(r["user_id"] for r in recipients))
    web_ids = list(dict.fromkeys(
        web_id_by_novel[n] for n in novel_ids if web_id_by_novel and web_id_by_novel.get(n)
    ))
    for user_chunk in _chunks(user_ids, CHUNK_SIZE):
        try:
            for web_chunk in (_chunks(web_ids, CHUNK_SIZE) if web_ids else [None]):
                def read_tiers(user_chunk=user_chunk, web_chunk=web_chunk):
                    query = supabase.table("user_site_tiers").select("user_id, web_id, tier").in_("user_id", user_chunk)
                    if web_chunk:
                        query = query.in_("web_id", web_chunk)
                    return query.execute()
                for row in _rows(_retry("fetching site tiers", read_tiers, attempts=3)):
                    tiers[(row["user_id"], row["web_id"])] = row["tier"]
        except Exception:
            # Without the tier, an early-access alert could be scheduled wrongly and never corrected.
            # So hold these users' novels back instead of guessing.
            bad_users = set(user_chunk)
            failed.update(r["novel_id"] for r in recipients if r["user_id"] in bad_users)
            recipients = [r for r in recipients if r["user_id"] not in bad_users]

    return SubscriberFetch(recipients, tiers, failed)


# ---- the notification queue ------------------------------------------------------------------

def _normalise_record(record):
    for key in ("user_id", "novel_id", "chapter", "chapter_url"):
        if record.get(key) is None:
            raise ValueError(f"queue record is missing {key!r}")
    return {
        "user_id": record["user_id"],
        "novel_id": record["novel_id"],
        "chapter": record["chapter"],
        "chapter_url": record["chapter_url"],
        "access_state": record.get("access_state") or "free",
        "unlock_at": _iso(record.get("unlock_at")),
        "due_at": _iso(record.get("due_at")) or datetime.now(timezone.utc).isoformat(),
        "status": record.get("status") or "pending",
    }


def insert_queue_records(records):
    """Queue notifications. Re-inserting a chapter is a no-op (unique key + ignore_duplicates).
    Returns {"attempted", "inserted", "failed", "failed_novel_ids"}: the last two describe batches
    that could not be written, so the caller can leave those novels un-advanced (D1)."""
    prepared = [_normalise_record(r) for r in (records or [])]
    result = {"attempted": len(prepared), "inserted": 0, "failed": 0, "failed_novel_ids": set()}

    for batch in _chunks(prepared, INSERT_BATCH_SIZE):
        try:
            response = _retry(
                "inserting queue rows",
                lambda batch=batch: (
                    supabase.table("pending_notifications")
                    .upsert(batch, on_conflict="user_id,novel_id,chapter", ignore_duplicates=True)
                    .execute()
                ),
                attempts=3,
            )
            result["inserted"] += len(_rows(response))
        except Exception:
            result["failed"] += len(batch)
            result["failed_novel_ids"].update(r["novel_id"] for r in batch)
    return result


def maintain_queue():
    """Recover stale 'sending' rows, cancel rows whose subscription ended, expire rows 24h overdue."""
    try:
        response = _retry("maintaining the queue", lambda: supabase.rpc("maintain_notification_queue", {}).execute(), attempts=3)
        row = (_rows(response) or [{}])[0]
        return {"ok": True, "recovered": int(row.get("recovered") or 0),
                "cancelled": int(row.get("cancelled") or 0), "expired": int(row.get("expired") or 0)}
    except Exception:
        return {"ok": False, "recovered": 0, "cancelled": 0, "expired": 0}


def claim_due(limit=500):
    """Atomically take the rows that are due and eligible (they become 'sending'). Fail-soft: [] on error."""
    try:
        response = _retry("claiming due notifications", lambda: supabase.rpc("claim_due_notifications", {"batch_limit": limit}).execute(), attempts=2)
        return _rows(response)
    except Exception:
        return []


def _ack(label, function, ids, extra=None):
    ids = [i for i in dict.fromkeys(ids or [])]
    total = 0
    for chunk in _chunks(ids, RPC_ID_CHUNK):
        params = {"notification_ids": chunk}
        params.update(extra or {})
        try:
            response = _retry(label, lambda params=params: supabase.rpc(function, params).execute(), attempts=3)
            total += int(_scalar(response))
        except Exception:
            pass          # rows stay 'sending'; maintain recovers them (at-least-once, accepted)
    return total


def ack_sent(ids):
    """Mark rows delivered. Returns how many changed (0 for rows that were not 'sending')."""
    return _ack("acknowledging sent notifications", "mark_notifications_sent", ids)


def release(ids, error_text):
    """Give rows back after a transient send failure: pending again, or dead at the 24-attempt cap / after 24h. (The scraper claims a
    row once per run, so hourly runs spread the attempts over the 24 hours; see main.dispatch_queue.)"""
    return _ack("releasing failed notifications", "release_failed_notifications", ids, {"err_text": _short_error(error_text) or "unknown"})


def mark_dead(ids, error_text):
    """Give up on rows (unrecoverable bad request)."""
    return _ack("marking notifications dead", "mark_notifications_dead", ids, {"err_text": _short_error(error_text) or "unknown"})


def handle_blocked_users(telegram_user_id):
    """The user blocked the bot: pause them and cancel their waiting rows (10.5). /start undoes the pause;
    cancelled rows are not resurrected. Returns {"ok", "paused", "cancelled"}."""
    result = {"ok": False, "paused": False, "cancelled": 0}
    try:
        paused = _retry(
            "pausing blocked user",
            lambda: supabase.table("users").update({"status": "paused"}).eq("telegram_user_id", telegram_user_id).execute(),
            attempts=3,
        )
        users = _rows(paused)
        result["paused"] = bool(users)
        for user in users:
            cancelled = _retry(
                "cancelling blocked user's rows",
                lambda user=user: (
                    supabase.table("pending_notifications")
                    .update({"status": "cancelled", "error_detail": "blocked bot"})
                    .eq("user_id", user["id"])
                    .in_("status", ["pending", "sending"])
                    .execute()
                ),
                attempts=3,
            )
            result["cancelled"] += len(_rows(cancelled))
        result["ok"] = True
    except Exception:
        pass
    return result


def queue_stats():
    """Last-24h queue numbers for the digest, or None if they could not be read."""
    try:
        response = _retry("reading queue stats", lambda: supabase.rpc("queue_stats_24h", {}).execute(), attempts=2)
        rows = _rows(response)
        return rows[0] if rows else None
    except Exception:
        return None


# ---- site health and admin state -------------------------------------------------------------

def record_site_result(web_id, status, error=None):
    """Record one full scrape's outcome. Returns {"consecutive_failures", "failure_alerted"} or None."""
    if status not in SITE_STATUSES:
        raise ValueError(f"invalid site status: {status!r}")
    params = {"p_web_id": web_id, "p_status": status, "p_error": _short_error(error)}
    try:
        response = _retry("recording site result", lambda: supabase.rpc("record_site_result", params).execute(), attempts=2)
        rows = _rows(response)
        if not rows:
            return None
        return {"consecutive_failures": rows[0].get("o_consecutive_failures"), "failure_alerted": rows[0].get("o_failure_alerted")}
    except Exception:
        return None


def _site_row(row):
    site = row.get("websites") or {}
    return {
        "web_id": row.get("web_id"),
        "web_domain_name": site.get("web_domain_name"),
        "last_status": row.get("last_status"),
        "consecutive_failures": row.get("consecutive_failures"),
        "failure_alerted": row.get("failure_alerted"),
        "failing_since": row.get("failing_since"),
        "last_failed_at": row.get("last_failed_at"),
        "last_resolved_at": row.get("last_resolved_at"),
        "last_error": row.get("last_error"),
    }


SITE_HEALTH_COLUMNS = ("web_id, last_status, consecutive_failures, failure_alerted, failing_since, "
                       "last_failed_at, last_resolved_at, last_error, websites(web_domain_name)")


def fetch_site_alerts():
    """Sites that have failed 3+ runs in a row and have not been reported to the admin yet."""
    try:
        response = _retry(
            "reading site alerts",
            lambda: (
                supabase.table("site_health").select(SITE_HEALTH_COLUMNS)
                .gte("consecutive_failures", ALERT_FAILURE_THRESHOLD).eq("failure_alerted", False).execute()
            ),
            attempts=2,
        )
        return [_site_row(r) for r in _rows(response)]
    except Exception:
        return []


def ack_site_alerts(web_ids):
    """Mark sites as reported. Call only after the alert was really sent. Returns how many changed."""
    web_ids = list(dict.fromkeys(web_ids or []))
    changed = 0
    for chunk in _chunks(web_ids, CHUNK_SIZE):
        try:
            response = _retry(
                "acknowledging site alerts",
                lambda chunk=chunk: supabase.table("site_health").update({"failure_alerted": True}).in_("web_id", chunk).execute(),
                attempts=3,
            )
            changed += len(_rows(response))
        except Exception:
            pass
    return changed


def fetch_site_health_rows():
    """Every site's health row (with the site name) for the daily digest. [] on error."""
    try:
        response = _retry("reading site health", lambda: supabase.table("site_health").select(SITE_HEALTH_COLUMNS).execute(), attempts=2)
        return [_site_row(r) for r in _rows(response)]
    except Exception:
        return []


def get_app_state(key):
    """The stored value for key, or None if there is none. UNLIKE the other functions here this
    RAISES when the database cannot be read: 'never sent' (None) and 'could not check' must not look
    the same, or a database blip would make the daily digest send again."""
    response = _retry(
        "reading app state",
        lambda: supabase.table("app_state").select("value").eq("key", key).limit(1).execute(),
        attempts=3,
    )
    rows = _rows(response)
    return rows[0].get("value") if rows else None


def set_app_state(key, value):
    """Store value (text) under key with updated_at = now. Returns True on success, False otherwise."""
    record = {"key": key, "value": value, "updated_at": datetime.now(timezone.utc).isoformat()}
    try:
        _retry("writing app state", lambda: supabase.table("app_state").upsert(record, on_conflict="key").execute(), attempts=3)
        return True
    except Exception:
        return False


def insert_scraper_logs():
    max_attempts = 3
    status = False
    row_id = None

    for attempt in range(1, max_attempts + 1):
        print(f"inserting into scraper_logs, attempt: {attempt}")

        try:
            timestamptz = datetime.now(timezone.utc).isoformat()
            insert_scraper_log = (
                supabase.table("scraper_logs").insert({"last_run_timestamp": timestamptz}).execute()
            )
            if insert_scraper_log.data:
                row_id = insert_scraper_log.data[0]["id"]
                status = True
                break
            else:
                raise ValueError("insert operation succeeded but supabase returned no data")

        except Exception as e:
            if attempt < max_attempts:
                print(f"insert operation failed, trying again. error: {e}")
                time.sleep(5)
            else:
                print(f"all attempts failed, scraper cannot run further, please try again later. error: {e}")
                return False, None

    return status, row_id


def update_scraper_logs(row_id):
    max_attempts = 3
    status = False

    for attempt in range(1, max_attempts +1):
        print(f"updating scraper_logs, attempt: {attempt}")

        try:
            update_scraper_log = (
                supabase.table("scraper_logs").update({"run_status": "success"}).eq("id", row_id).execute()
            )
            if update_scraper_log.data:
                status = True
                break
            else:
                print(f"no row matched id: {row_id}, update did not apply")

        except Exception as e:
            if attempt < max_attempts:
                print(f"updation operation failed, trying again. error: {e}")
                time.sleep(5)
            else:
                print(f"all attempts failed, scraper cannot run further, please try againg later. error: {e}")
    
    return status


# =====================================================================
# Bot-side functions (used by src/bot.py)
#
# Unlike the scraper functions above, these do NOT retry and do NOT hide failures behind
# an empty list: a database error is raised, and bot.py's error handler tells the user
# "something went wrong". That matters here: "user not found" (None) and "the database
# is down" (exception) must never look the same, or a broken connection would make the
# bot try to register people who already exist.
# =====================================================================

READING_STATUSES = ("reading", "completed", "left", "on hiatus")
USER_STATUSES = ("active", "paused")
PLACEHOLDER_EMAIL_DOMAIN = "scrollping.invalid"


def _is_unique_violation(error):
    text = str(error).lower()
    return getattr(error, "code", None) == "23505" or "23505" in text or "duplicate key" in text


def get_user(telegram_user_id):
    """Return the users row for this Telegram id, or None if they never registered."""
    res = (
        supabase.table("users")
        .select("id, username, telegram_user_id, status")
        .eq("telegram_user_id", telegram_user_id)
        .execute()
    )
    return res.data[0] if res.data else None


def create_user(telegram_user_id, username):
    """Register a Telegram user: auth user first (users.id references auth.users), then the
    users row. Supabase refuses to create an auth user without an email or phone, and the bot
    has neither, so a placeholder email is used. No password is set, so nobody can log in as
    it, and the .invalid domain can never receive mail."""
    email = f"tg{telegram_user_id}@{PLACEHOLDER_EMAIL_DOMAIN}"
    auth_res = supabase.auth.admin.create_user({"email": email, "email_confirm": True})
    auth_id = auth_res.user.id

    try:
        inserted = (
            supabase.table("users")
            .insert({"id": auth_id, "username": username, "telegram_user_id": telegram_user_id})
            .execute()
        )
    except Exception as e:
        # do not leave an orphan auth user behind
        try:
            supabase.auth.admin.delete_user(auth_id)
        except Exception as cleanup_error:
            print(f"could not delete orphan auth user {auth_id}: {cleanup_error}")

        if _is_unique_violation(e):
            existing = get_user(telegram_user_id)
            if existing:
                return existing
        raise

    if inserted.data:
        return inserted.data[0]
    return get_user(telegram_user_id)


def set_user_status(user_id, status):
    """Pause or resume notifications. Returns True if a row was updated."""
    if status not in USER_STATUSES:
        raise ValueError(f"invalid user status: {status!r}")
    res = supabase.table("users").update({"status": status}).eq("id", user_id).execute()
    return bool(res.data)


def delete_user_account(user_id):
    """Delete the account and everything tied to it. Deleting the auth user is enough:
    auth.users -> users -> user_subscriptions all cascade."""
    supabase.auth.admin.delete_user(user_id)
    return True


def get_or_create_website(web_domain_name):
    """web_domain_name is a key from site_parse_rules.json (trusted config, never user input).
    The weekly cron deletes websites that have no novels, so the row may be missing even for a
    supported site; it is simply recreated here."""
    res = supabase.table("websites").select("id").eq("web_domain_name", web_domain_name).execute()
    if res.data:
        return res.data[0]["id"]

    try:
        inserted = supabase.table("websites").insert({"web_domain_name": web_domain_name}).execute()
        return inserted.data[0]["id"]
    except Exception as e:
        if _is_unique_violation(e):
            again = supabase.table("websites").select("id").eq("web_domain_name", web_domain_name).execute()
            if again.data:
                return again.data[0]["id"]
        raise


def _find_novel(web_id, slug):
    res = (
        supabase.table("novels")
        .select("id")
        .eq("web_id", web_id)
        .eq("novel_name", slug)
        .execute()
    )
    return res.data[0]["id"] if res.data else None


def _create_novel(web_id, slug):
    # latest_chap is deliberately left out of the payload: sending None would write a real
    # null and skip the column default (-1 = "no chapter scraped yet"). updated_at is set so
    # the 97-day cleanup cron can eventually remove a slug that never turns out to be real.
    timestamptz = datetime.now(timezone.utc).isoformat()
    try:
        inserted = (
            supabase.table("novels")
            .insert({"web_id": web_id, "novel_name": slug, "updated_at": timestamptz})
            .execute()
        )
        return inserted.data[0]["id"]
    except Exception as e:
        if _is_unique_violation(e):
            existing = _find_novel(web_id, slug)
            if existing:
                return existing
        raise


def subscribe_user_to_novel(user_id, web_domain_name, slug, max_subscriptions):
    """Subscribe a user to a novel, creating the website/novel rows if needed.
    Returns "added", "already_subscribed" or "limit_reached"."""
    current = supabase.table("user_subscriptions").select("id").eq("user_id", user_id).execute()
    subscription_count = len(current.data or [])

    web_id = get_or_create_website(web_domain_name)
    novel_id = _find_novel(web_id, slug)

    if novel_id is not None:
        already = (
            supabase.table("user_subscriptions")
            .select("id")
            .eq("user_id", user_id)
            .eq("novel_id", novel_id)
            .execute()
        )
        if already.data:
            return "already_subscribed"

    if subscription_count >= max_subscriptions:
        return "limit_reached"

    if novel_id is None:
        novel_id = _create_novel(web_id, slug)

    try:
        supabase.table("user_subscriptions").insert({"user_id": user_id, "novel_id": novel_id}).execute()
    except Exception as e:
        if _is_unique_violation(e):
            return "already_subscribed"
        raise

    return "added"


def list_subscriptions(user_id):
    """Flat rows for the /list menus, sorted by novel name:
    {"id", "reading_status", "alert_timing", "novel_name", "display_title", "latest_chap",
     "web_id", "web_domain_name"}"""
    res = (
        supabase.table("user_subscriptions")
        .select("id, reading_status, alert_timing, "
                "novels(novel_name, display_title, latest_chap, web_id, websites(web_domain_name))")
        .eq("user_id", user_id)
        .execute()
    )

    rows = []
    for row in res.data or []:
        novel = row.get("novels") or {}
        website = novel.get("websites") or {}
        rows.append({
            "id": row["id"],
            "reading_status": row["reading_status"],
            "alert_timing": row.get("alert_timing"),
            "novel_name": novel.get("novel_name", "?"),
            "display_title": novel.get("display_title"),
            "latest_chap": novel.get("latest_chap"),
            "web_id": novel.get("web_id"),
            "web_domain_name": website.get("web_domain_name"),
        })

    rows.sort(key=lambda r: r["novel_name"])
    return rows


def remove_subscription(user_id, subscription_id):
    """Delete one subscription. Filters on BOTH ids: the secret key bypasses RLS, so this
    filter is the only thing stopping one user from touching another user's row."""
    res = (
        supabase.table("user_subscriptions")
        .delete()
        .eq("id", subscription_id)
        .eq("user_id", user_id)
        .execute()
    )
    return bool(res.data)


def set_subscription_status(user_id, subscription_id, status):
    """Change a subscription's reading status (same ownership filter as remove_subscription)."""
    if status not in READING_STATUSES:
        raise ValueError(f"invalid reading status: {status!r}")
    res = (
        supabase.table("user_subscriptions")
        .update({"reading_status": status})
        .eq("id", subscription_id)
        .eq("user_id", user_id)
        .execute()
    )
    return bool(res.data)


# ---- site tiers, alert timing and novel details (bot side, burn-in) ---------------------------
# Same conventions as the rest of the bot side: no retries, errors propagate to the handler's error
# path, and every write filters on the caller's own user id.

TIERS = ("standard", "member")
ALERT_TIMINGS = ("early", "when_free")


def get_novel_summary(web_domain_name, slug):
    """{"id", "web_id", "novel_name", "display_title"} for a novel, or None. Used to word the /add confirmation."""
    web_id = get_or_create_website(web_domain_name)
    res = (
        supabase.table("novels")
        .select("id, web_id, novel_name, display_title")
        .eq("web_id", web_id)
        .eq("novel_name", slug)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def get_website_domain(web_id):
    """The web_domain_name of a website id, or None. Lets a callback prove an id really is a known site."""
    res = supabase.table("websites").select("web_domain_name").eq("id", web_id).limit(1).execute()
    return res.data[0]["web_domain_name"] if res.data else None


def get_user_tier(user_id, web_id):
    """"standard" / "member", or None when the user has never chosen for that site."""
    res = (
        supabase.table("user_site_tiers")
        .select("tier")
        .eq("user_id", user_id)
        .eq("web_id", web_id)
        .limit(1)
        .execute()
    )
    return res.data[0]["tier"] if res.data else None


def list_user_tiers(user_id):
    """{web_id: tier} for every site the user has chosen a tier for."""
    res = supabase.table("user_site_tiers").select("web_id, tier").eq("user_id", user_id).execute()
    return {row["web_id"]: row["tier"] for row in (res.data or [])}


def set_user_tier(user_id, web_id, tier):
    """Create or change the user's tier for a site. Returns True."""
    if tier not in TIERS:
        raise ValueError(f"invalid tier: {tier!r}")
    (
        supabase.table("user_site_tiers")
        .upsert({"user_id": user_id, "web_id": web_id, "tier": tier}, on_conflict="user_id,web_id")
        .execute()
    )
    return True


def set_alert_timing(user_id, subscription_id, timing):
    """Set one subscription's alert timing: "early", "when_free", or None (follow the site tier).
    Filters on the user's own id, so another user's subscription id changes nothing.
    Returns True if a row was changed."""
    if timing is not None and timing not in ALERT_TIMINGS:
        raise ValueError(f"invalid alert timing: {timing!r}")
    res = (
        supabase.table("user_subscriptions")
        .update({"alert_timing": timing})
        .eq("id", subscription_id)
        .eq("user_id", user_id)
        .execute()
    )
    return bool(res.data)
