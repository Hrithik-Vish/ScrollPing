import requests

def fetch_html(url):
    try:
        user_agent_header = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        }
        response = requests.get(url, timeout= (5, 30), headers=user_agent_header)
        if response.ok:
            print("pass")
            return response.text
        else:
            print("there was a problem, resturned status code: ",response.status_code)
            return ""

    except requests.exceptions.ConnectTimeout:
        print("connection timeout")
        print("fail")
        return ""
    except requests.exceptions.ConnectionError:
        print("connection error")
        print("fail")
        return ""
    except requests.exceptions.RequestException as error:
        print("details: ", error)
        print("fail")
        return ""


# this is for testing the function
# html_txt = fetch_html("https://asurascans.com/")

# if html_txt:
#     with open("html_txt.txt", "w", encoding = "utf-8") as file:
#         file.write(html_txt)
#     print("data saved to html_txt sucessfully")
# else: 
#     print("cannot save data, no data fetched to save")


# -------------------------------------------------------------------------------------------------------------
# Sites on which fetching is working properly:
# SITES = [
#     {"site_name": "asurascans", "url": "https://asurascans.com/", "priority": "core", "mode": "html"},
#     {"site_name": "hivetoons", "url": "https://hivetoons.org/", "priority": "core", "mode": "html"},
#     {"site_name": "kunmanga", "url": "https://www.kunmanga.online/", "priority": "core", "mode": "html"},
#     {"site_name": "mgeko", "url": "https://www.mgeko.cc/jumbo/manga/", "priority": "secondary", "mode": "html_li_block"},
#     {
#         "site_name": "mangadex",
#         "url": "https://api.mangadex.org/chapter?includes[]=manga&order[publishAt]=desc&limit=32&offset=0",
#         "priority": "secondary",
#         "mode": "json",
#     },
#     {"site_name": "vortexscans", "url": "https://vortexscans.org/", "priority": "secondary", "mode": "html"},
# ]
# -------------------------------------------------------------------------------------------------------------
    



