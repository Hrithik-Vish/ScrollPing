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
                return

        except Exception as e:
            if attempt < max_attempts:
                print(f"attempt: {attempt} failed due to exception: {e}, trying again")
                time.sleep(5)
            else:
                print(f"attempt: {attempt} failed,\nall attempts failed, try again later, exception: {e}")



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
                return

        except Exception as e:
            if attempt < max_attempts:
                print(f"attempt: {attempt} failed due to exception: {e}, trying again")
                time.sleep(5)
            else:
                print(f"attempt: {attempt} failed,\nall attempts failed, try again later, exception: {e}")



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

    for attempt in range(1, max_attempts +1):
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
                print(f"all attempts failed, scraper cannot run further, please try againg later. error: {e}")
    
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
                supabase.table("user_subscriptions").select("novel_id, users(telegram_user_id)").in_("novel_id", novel_id_list).eq("reading_status", "reading").execute()
            )
            if read_subscribers.data:
                subscribers_list = read_subscribers.data
                return subscribers_list
            else:
                print("data fetched was empty")
                return

        except Exception as e:
            if attempt < max_attempts:
                print(f"attempt: {attempt} failed due to exception: {e}, trying again")
                time.sleep(5)
            else:
                print(f"attempt: {attempt} failed,\nall attempts failed, try again later, exception: {e}")