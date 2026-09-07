import time
from src import database
from src import fetching
from src import parsing
from src import telegramMessenger

def finding_updated_novels(site_id, web_url):
    for id, url in zip(site_id, web_url):
        updated_novel_list.append(process_website(id, url))
    return updated_novel_list


def process_website(site_id, web_url):
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
status, row_id = database.insert_scraper_logs()




if status == True:
    
    site_id = []
    web_url = []

    websites_list = database.fetch_websites()

    for items in websites_list:
        site_id.append(items['id'])
        web_url.append(items['web_domain_name'])


    updated_novel_list = []
    updated_novel_list = finding_updated_novels(site_id, web_url)


    novel_id_list = []
    subscribers_list = database.fetch_subscribers(novel_id_list)

    database.update_scraper_logs(row_id)