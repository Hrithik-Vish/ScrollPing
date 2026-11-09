import os
import re
import json

try:
    import requests
    def fetch_url(url: str):
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"}
        r = requests.get(url, headers=headers, timeout=30)
        return r.status_code, r.text
except ImportError:
    import urllib.request
    def fetch_url(url: str):
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", errors="ignore")

def check_asura(html: str):
    print("\n--- 1. ASURASCANS SANITY CHECK ---")
    print(f"HTML Length: {len(html)} bytes")
    astro_islands = re.findall(r'<astro-island\b[^>]*>', html)
    props_matches = re.findall(r'props="({.*?})"', html)
    print(f"Astro Island tags: {len(astro_islands)} | Props matches: {len(props_matches)}")
    
    titles_found = set()
    sample_locked = []
    
    for p in props_matches:
        unescaped = p.replace('&quot;', '"').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        try:
            data = json.loads(unescaped)
            def scan(obj):
                if isinstance(obj, dict):
                    comic = obj.get("comic_name")
                    if comic:
                        name = comic[1] if isinstance(comic, list) and len(comic) > 1 else str(comic)
                        titles_found.add(name)
                        is_prem = obj.get("is_premium")
                        prem_val = is_prem[1] if isinstance(is_prem, list) and len(is_prem) > 1 else is_prem
                        if prem_val is True:
                            sample_locked.append((name, obj.get("early_access_until")))
                    for v in obj.values():
                        scan(v)
                elif isinstance(obj, list):
                    for item in obj:
                        scan(item)
            scan(data)
        except Exception:
            pass
            
    print(f"Unique titles parsed: {len(titles_found)}")
    print(f"Sample titles: {list(titles_found)[:3]}")
    print(f"Locked chapters: {len(sample_locked)}")
    assert len(titles_found) > 0, "Asurascans: No titles parsed from astro-island props!"
    print(">>> Asurascans: PASS <<<")

def check_hivetoons(html: str):
    print("\n--- 2. HIVETOONS SANITY CHECK ---")
    print(f"HTML Length: {len(html)} bytes")
    # Title from line-clamp-2
    titles = re.findall(r'<a[^>]*class="[^"]*line-clamp-2[^"]*"[^>]*title="([^"]+)"', html)
    if not titles:
        # Fallback to TSR postTitle
        titles = re.findall(r'postTitle:"([^"]+)"', html)
    print(f"Titles found: {len(titles)} | Sample: {titles[:3]}")
    
    paid_rows = re.findall(r'class="[^"]*paid-chapter-row[^"]*"', html)
    print(f"paid-chapter-row classes: {len(paid_rows)}")
    assert len(titles) > 0, "HiveToons: No titles found!"
    print(">>> HiveToons: PASS <<<")

def check_vortexscans(html: str):
    print("\n--- 3. VORTEXSCANS SANITY CHECK ---")
    print(f"HTML Length: {len(html)} bytes")
    titles = re.findall(r'<a[^>]*class="[^"]*line-clamp-2[^"]*"[^>]*title="([^"]+)"', html)
    if not titles:
        titles = re.findall(r'postTitle:"([^"]+)"', html)
    print(f"Titles found: {len(titles)} | Sample: {titles[:3]}")
    
    paid_rows = re.findall(r'class="[^"]*paid-chapter-row[^"]*"', html)
    print(f"paid-chapter-row classes: {len(paid_rows)}")
    assert len(titles) > 0, "VortexScans: No titles found!"
    print(">>> VortexScans: PASS <<<")

def check_kunmanga(html: str):
    print("\n--- 4. KUNMANGA SANITY CHECK ---")
    print(f"HTML Length: {len(html)} bytes")
    # .post-title h4 a
    titles = re.findall(r'<div[^>]*class="[^"]*post-title[^"]*"[\s\S]*?<h4[^>]*>[\s\S]*?<a[^>]*>([^<]+)</a>', html)
    if not titles:
        titles = re.findall(r'<h4[^>]*class="[^"]*title[^"]*"[^>]*>[\s\S]*?<a[^>]*>([^<]+)</a>', html)
    clean_titles = [t.strip() for t in titles if t.strip()]
    print(f"Titles found: {len(clean_titles)} | Sample: {clean_titles[:3]}")
    assert len(clean_titles) > 0, "KunManga: No titles found!"
    print(">>> KunManga: PASS <<<")

def check_mgeko(html: str):
    print("\n--- 5. MGEKO SANITY CHECK ---")
    print(f"HTML Length: {len(html)} bytes")
    # h4.novel-title
    titles = re.findall(r'<h4[^>]*class="[^"]*novel-title[^"]*"[^>]*>\s*<a[^>]*>([^<]+)</a>', html)
    if not titles:
        titles = re.findall(r'<h4[^>]*class="[^"]*novel-title[^"]*"[^>]*>([^<]+)</h4>', html)
    clean_titles = [t.strip() for t in titles if t.strip()]
    print(f"Titles found: {len(clean_titles)} | Sample: {clean_titles[:3]}")
    assert len(clean_titles) > 0, "mgeko: No titles found!"
    print(">>> mgeko: PASS <<<")

def main():
    os.makedirs("captured_html", exist_ok=True)
    sites = [
        ("asurascans", "https://asurascans.com/", check_asura),
        ("hivetoons", "https://hivetoons.org/", check_hivetoons),
        ("vortexscans", "https://vortexscans.org/", check_vortexscans),
        ("kunmanga", "https://www.kunmanga.online/", check_kunmanga),
        ("mgeko", "https://www.mgeko.cc/jumbo/manga/", check_mgeko),
    ]
    
    results = {}
    for name, url, check_fn in sites:
        print(f"\n==========================================")
        print(f"Fetching {name}: {url}")
        try:
            status, html = fetch_url(url)
            print(f"HTTP Status: {status}")
            with open(f"captured_html/{name}.html", "w", encoding="utf-8") as f:
                f.write(html)
            check_fn(html)
            results[name] = "PASS"
        except Exception as e:
            print(f"ERROR on {name}: {e}")
            results[name] = f"FAIL ({e})"

    print("\n==========================================")
    print("FINAL 5-SITE RUNNER CAPTURE SUMMARY:")
    for name, res in results.items():
        print(f"  * {name}: {res}")
    print("==========================================")
    
    if any(r != "PASS" for r in results.values()):
        raise SystemExit(1)

if __name__ == "__main__":
    main()
