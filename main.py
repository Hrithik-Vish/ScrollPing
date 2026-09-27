import time
import re
import json
from src import database
from src import fetching
from src import parsing
from src import telegramMessenger

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
        split_url = data.split("/")

        if len(split_url) != rules["segment_count"]:
            skipped.append(data)
            continue

        chap_match = re.compile(rules["chapter_regex"]).search(split_url[-1])
        if not chap_match:
            skipped.append(data)
            continue
        chap = float(chap_match.group(1))

        name = split_url[1]
        strip_regex = rules.get("slug_hash_strip_regex")
        if strip_regex:
            name = re.sub(strip_regex, "", name)

        if name not in fetched_novels:
            skipped.append(data)
            continue

        if chap > float(fetched_novels[name]["latest_chap"]):
            fetched_novels[name]["latest_chap"] = chap
            fetched_novels[name]["chapter_url"] = data

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
    # TODO: do something with `skipped` — at least log/print for now
    return updated_rows


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

    database.update_scraper_logs(row_id)