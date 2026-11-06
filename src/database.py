# The Target: supabase.table("table_name")

# The Action: .select(), .insert(), .update(), or .delete()

# The Filters (Optional): .eq(), .gt(), .ilike(), etc. (Who are we affecting?)

# The Modifiers (Optional): .order(), .limit(), .single(), etc. (How should the data look?)

# The Trigger: .execute() (Send it to the server!)


import os
import time
from datetime import datetime, timezone
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



def update_novels_table(updated_novel_list):
    failToUpdate_list = []
    for novels in updated_novel_list:
        max_attempts = 3

        for attempt in range(1, max_attempts +1):
            print(f"attempting to update novels table, attepmt: {attempt}")

            try: 
                print(f'updating the novels table for the novel_id: {novels["id"]} and novel_name: {novels["novel_name"]}')
                timestamptz = datetime.now(timezone.utc).isoformat()
                update_novels = (
                    supabase.table("novels").update({"latest_chap": novels["latest_chap"],"chapter_url": novels["chapter_url"], "updated_at": timestamptz}).eq("id", novels["id"]).execute()
                )
                break
                
            except Exception as e:
                if attempt < max_attempts:
                    print(f"error happened, error: {e}, trying again")
                    time.sleep(5)
                else:
                    print(f"all attempts failed, error: {e}, moving to next novel if exists")
                    failToUpdate_list.append(novels["id"])
    
    return failToUpdate_list


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


    
def fetch_subscribers(novel_id_list):
    max_attempts = 5

    for attempt in range(1, max_attempts +1):
        print(f"fetching subscribers, attempt: {attempt}")

        try:
            read_subscribers = (
                supabase.table("user_subscriptions").select("novel_id, users!inner(telegram_user_id)").in_("novel_id", novel_id_list).eq("reading_status", "reading").eq("users.status", "active").execute()
            )
            if read_subscribers.data:
                subscribers_list = read_subscribers.data
                return subscribers_list
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
    """Flat rows for /list, sorted by novel name:
    {"id", "reading_status", "novel_name", "latest_chap", "web_domain_name"}"""
    res = (
        supabase.table("user_subscriptions")
        .select("id, reading_status, novels(novel_name, latest_chap, websites(web_domain_name))")
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
            "novel_name": novel.get("novel_name", "?"),
            "latest_chap": novel.get("latest_chap"),
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
