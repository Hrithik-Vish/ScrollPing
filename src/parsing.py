import re
from bs4 import BeautifulSoup

def parse_html(file):
    soup = BeautifulSoup(file,"lxml")

    html_content = soup.find_all("a", href = re.compile(r"[/-]?(chapters?|chs?|episodes?|eps?|comics?|titles?)[/-]?(\d+(\.\d+)?)"))
    #re.compile needs working with edge cases re.compile(r"[/-]?(chapters?|chs?|episodes?|eps?|titles?)[/-]?(\d+(\.\d+)?)"
    
    html_data = []

    for links in html_content:
        html_url = links.get("href")
        if html_url:
            html_data.append(html_url)
    
    return html_data




# this is for testing the function
# with open("html_txt.txt", "r", encoding="utf-8") as file:
#     html_file = file.read()

# url_list = parse_html(html_file)

# if url_list:
#     with open("a_tags.txt", "w", encoding = "utf-8") as file:
#         for links in url_list:
#             file.write(f"{links}\n")
#     print("Done writing all href's")



# -------------------------------------------------------------------------------------------------------------
# Sites on which parsing is working perfectly:
# SITES_WORKING = [
#     {"site_name": "asurascans", "url": "https://asurascans.com/", "priority": "core", "mode": "html"},
#     {"site_name": "hivetoons", "url": "https://hivetoons.org/", "priority": "core", "mode": "html"},
#     {"site_name": "kunmanga", "url": "https://www.kunmanga.online/", "priority": "core", "mode": "html"},
#     {"site_name": "vortexscans", "url": "https://vortexscans.org/", "priority": "secondary", "mode": "html"},
# ]


# Sites on which parsing is not working (mainly whose output is different from pure HTML type):
# SITES_NOT_WORKING = [
#     {"site_name": "mgeko", "url": "https://www.mgeko.cc/jumbo/manga/", "priority": "secondary", "mode": "html_li_block"},
#     {
#         "site_name": "mangadex",
#         "url": "https://api.mangadex.org/chapter?includes[]=manga&order[publishAt]=desc&limit=32&offset=0",
#         "priority": "secondary",
#         "mode": "json",
#     }
# ]
# -------------------------------------------------------------------------------------------------------------