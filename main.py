import time
import re
import json
import sys
import traceback
from src import database
from src import fetching
from src import parsing
from src import telegramMessenger
from telegram.error import InvalidToken
from urllib.parse import urljoin

def finding_updated_novels(site_id_list, web_url_list, site_parse_rules):
    updated_novel_list = []
    for site_id, web_url in zip(site_id_list, web_url_list):
        updated_novel_list.extend(process_website(site_id, web_url, site_parse_rules))
    return updated_novel_list


def retry(func, max_attempts, *args):
    for attempts in range(1, max_attempts + 1):
        try: 
            return func(*args)
        except Exception as e:
            if attempts < max_attempts:
                print(f"Retrying, while performing: {func} an error: {e} occured, attempt: {attempts}")
                time.sleep(5)
            else: 
                print(f"coudn't resolve the issue, error: {e}, max attempts reached, please try again")
                return []


def compilation_fnct(parsing_data, fetched_novels, rules):
    updated_rows = []
    skipped = []
    original_latest = {name: info["latest_chap"] for name, info in fetched_novels.items()}

    for data in parsing_data:
        split_url = data.strip("/").split("/")

        if len(split_url) != rules["segment_count"]:
            skipped.append(data)
            continue

        chap_match = re.compile(rules["chapter_regex"]).search("/".join(split_url[-2:]))
        if not chap_match:
            skipped.append(data)
            continue
        chap = float(chap_match.group(1))

        name = split_url[1]
        strip_regex = rules.get("slug_hash_strip_regex")
        if strip_regex:
            name = re.sub(strip_regex, "", name)
            split_url[1] = name

        if name not in fetched_novels:
            skipped.append(data)
            continue

        path = "/" + "/".join(split_url)
        chapter_url = urljoin(rules["base_url"], path)

        if chap > float(fetched_novels[name]["latest_chap"]):
            fetched_novels[name]["latest_chap"] = chap
            fetched_novels[name]["chapter_url"] = chapter_url

    for name, info in fetched_novels.items():
        if float(info["latest_chap"]) > float(original_latest[name]):
            updated_rows.append({
                "id": info["id"],
                "novel_name": name,
                "latest_chap": info["latest_chap"],
                "chapter_url": info["chapter_url"]
            })

    return updated_rows, skipped


def process_website(site_id, web_url, site_parse_rules):
    max_attempts = 3

    html_structure = retry(fetching.fetch_html, max_attempts, web_url)
    if not html_structure:
        return []

    parsing_data = parsing.parse_html(html_structure)
    if not parsing_data:
        return []

    novels_list = retry(database.fetch_novels, max_attempts, site_id)
    if not novels_list:
        return []

    fetched_novels = {
        novel['novel_name']: {k: v for k, v in novel.items() if k != 'novel_name'}
        for novel in novels_list
    }

    site_rules = site_parse_rules[web_url]
    updated_rows, skipped = compilation_fnct(parsing_data, fetched_novels, site_rules)

    if skipped:
        for data in skipped:
            print(f"{site_id}: {web_url}: {data}")

    return updated_rows


def group_subscribers(subscribers_list, updated_novel_list):
    novel_lookup = {
        novel["id"]: novel
        for novel in updated_novel_list
    }

    user_tele_dict = {}

    for row in subscribers_list:
        novel_id = row.get("novel_id")
        user_data = row.get("users") or {}

        telegram_id = user_data.get("telegram_user_id")

        if not telegram_id:
            continue

        novel_data = novel_lookup.get(novel_id)

        if novel_data is None:
            continue

        user_tele_dict.setdefault(telegram_id, []).append(novel_data)

    return user_tele_dict

def print_send_summary(summary):
    print(
        f"telegram: sent={summary['sent']} blocked={len(summary['blocked'])} "
        f"bad_request={len(summary['bad_request'])} failed={len(summary['failed'])}"
    )


def run():
    status, row_id = database.insert_scraper_logs()

    if status == True:
        site_id = []
        web_url = []
        novel_id_list = []

        websites_list = database.fetch_websites()

        for items in websites_list:
            site_id.append(items['id'])
            web_url.append(items['web_domain_name'])

        with open('site_parse_rules.json', 'r') as file:
            site_parse_rules = json.load(file)

        updated_novel_list = finding_updated_novels(site_id, web_url, site_parse_rules)

        database.update_novels_table(updated_novel_list)

        for data in updated_novel_list:
            novel_id_list.append(data['id'])
        
        subscribers_list = database.fetch_subscribers(novel_id_list)
        user_tele_dict = group_subscribers(subscribers_list, updated_novel_list)

        notify_failed = False
        try:
            send_summary = telegramMessenger.send_all(user_tele_dict)
            print_send_summary(send_summary)
        except InvalidToken:
            print("telegram: bot token was rejected, no notifications were sent")
            notify_failed = True
        except RuntimeError as exc:
            print(f"telegram: not configured: {exc}")
            notify_failed = True
        except Exception:
            traceback.print_exc()
            notify_failed = True

        database.update_scraper_logs(row_id)

        if notify_failed:
            sys.exit(1)

    else:
        print("could not insert into scraper_logs, stopping the run")
        sys.exit(1)


if __name__ == "__main__":
   run()