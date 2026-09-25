import re
from bs4 import BeautifulSoup

def parse_html(file):
    soup = BeautifulSoup(file,"lxml")

    html_content = soup.find_all("a", href = re.compile(r"[/-]?(chapters?|chs?|episodes?|eps?|)[/-]?(\d+(\.\d+)?)"))
    #re.compile needs working with edge cases
    
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

