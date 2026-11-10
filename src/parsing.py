import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urldefrag, urljoin, urlparse

import soupsieve
from bs4 import BeautifulSoup

from src.novel_links import clean_title, normalize_host

def parse_html(file):
    soup = BeautifulSoup(file,"lxml")

    html_content = soup.find_all("a", href = re.compile(r"[/-]?(chapters?|chs?|episodes?|eps?|comics?|titles?|series|mangas?)[/-]?(\d+(\.\d+)?)"))
    #re.compile needs working with edge cases re.compile(r"[/-]?(chapters?|chs?|episodes?|eps?|titles?)[/-]?(\d+(\.\d+)?)"
    
    html_data = []

    for links in html_content:
        html_url = links.get("href")
        if html_url:
            html_data.append(html_url)
    
    return html_data


def parse_html_li_block(file, rules):
    soup = BeautifulSoup(file, "lxml")

    blocks = []

    for item in soup.select(rules["item_selector"]):
        identity_tag = item.select_one(rules["identity_selector"])
        chapter_tag = item.select_one(rules["chapter_text_selector"])

        if identity_tag is None or chapter_tag is None:
            continue

        identity = identity_tag.get(rules["identity_attr"])
        chapter_text = chapter_tag.get_text(strip=True)

        if identity and chapter_text:
            blocks.append({"identity": identity, "chapter_text": chapter_text})

    return blocks

# ============================================================================================
# parse_page: one entry point that returns everything the compile step needs (spec section 7).
# Pure: html text + site rules in, plain data out. No network, no database.
#
#   {"items":  [{"href", "chapter_text", "locked", "title", "slug", "chapter", "unlock_at"}, ...],
#    "titles": {slug: title},
#    "diagnostics": {"items": int, "titles": int, "lock": "ok" | "missing" | "n/a", ...counts only}}
#
# item["locked"] is True/False when lock state is known and None when it is not (asura island
# missing). Sites without locks report False.
# ============================================================================================

DEFAULT_FALLBACK_WINDOW_HOURS = 6


def unwrap_astro(value):
    """Astro props are [0, plain_or_object] or [1, [wrapped, ...]]."""
    if isinstance(value, list) and len(value) == 2 and value[0] in (0, 1):
        kind, payload = value
        if kind == 1:
            return [unwrap_astro(v) for v in payload] if isinstance(payload, list) else payload
        if isinstance(payload, dict):
            return {k: unwrap_astro(v) for k, v in payload.items()}
        return payload
    return value  # unknown shape: returned as is, the validation in parse_astro_island catches it


def _parse_iso(value):
    """ISO 8601 text (trailing Z accepted) -> aware UTC datetime, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _chapter_text(number):
    """Chapter number as the site writes it in its own links: 156 not 156.0, 12.5 stays 12.5."""
    if isinstance(number, str):
        text = number.strip()
        value = float(text)
    else:
        value = float(number)
        text = repr(number) if isinstance(number, float) else str(number)
    if value.is_integer():
        return str(int(value))
    return text


def _normalise_slug(raw, rules):
    """unquote, lowercase, strip the site's hash suffix. The same normalisation novel_links applies
    to a pasted link, so the slug the scraper finds equals the slug stored for a user."""
    slug = unquote(raw or "").strip().lower()
    strip_regex = rules.get("slug_hash_strip_regex")
    if strip_regex:
        slug = re.sub(strip_regex, "", slug)
    return slug


def _site_path(href, rules):
    """Path of a link if it is relative or points at this site, else None (external links ignored)."""
    parsed = urlparse(href or "")
    if parsed.netloc:
        base_host = normalize_host(urlparse(rules.get("base_url", "")).hostname)
        if not base_host or normalize_host(parsed.hostname) != base_host:
            return None
    return unquote(parsed.path)


def _slug_from_link(href, rules):
    """Slug of a novel's own link (title anchors, series cards) via the site's novel_link_patterns."""
    path = _site_path(href, rules)
    if path is None:
        return None
    for pattern in rules.get("novel_link_patterns") or []:
        match = re.match(pattern, path)
        if match:
            slug = _normalise_slug(match.group("slug"), rules)
            return slug or None
    return None


def parse_astro_island(html, rules, soup=None):
    """Read asura-style chapter data out of an <astro-island> props attribute.
    Returns {"items", "titles", "skipped", "slug_mismatches"} or None when the island is missing or
    malformed (more than half the entries unusable). Needs the whole site rules: base_url and
    slug_hash_strip_regex are used as well as lock_detection."""
    lock_rules = rules.get("lock_detection") or {}
    fields = lock_rules.get("fields") or {}
    list_key = lock_rules.get("list_key", "chapters")
    if soup is None:
        soup = BeautifulSoup(html, "lxml")

    islands = soup.find_all("astro-island")
    component = lock_rules.get("component")
    matching = [i for i in islands if component and component in (i.get("component-url") or "")]
    candidates = matching + [i for i in islands if i not in matching] if matching else islands

    entries = None
    for island in candidates:
        try:
            props = json.loads(island.get("props") or "")
        except (ValueError, TypeError):
            continue
        if not isinstance(props, dict):
            continue
        unwrapped = {k: unwrap_astro(v) for k, v in props.items()}
        found = unwrapped.get(list_key)
        if isinstance(found, list) and found and all(isinstance(e, dict) for e in found):
            entries = found
            break
    if entries is None:
        return None

    f_url, f_slug, f_chapter = fields.get("public_url"), fields.get("slug"), fields.get("chapter")
    f_title, f_locked = fields.get("title"), fields.get("is_locked")
    f_published, f_unlock = fields.get("published_at"), fields.get("unlock_at")
    window = timedelta(hours=lock_rules.get("fallback_window_hours", DEFAULT_FALLBACK_WINDOW_HOURS))
    base_url = rules.get("base_url", "")

    items, titles, skipped, mismatches = [], {}, 0, 0
    for entry in entries:
        public_url = entry.get(f_url) if f_url else None
        slug_field = entry.get(f_slug) if f_slug else None
        number = entry.get(f_chapter) if f_chapter else None
        if f_locked not in entry or number is None or not (public_url or slug_field):
            skipped += 1
            continue
        try:
            chapter = float(number)
            chapter_text = _chapter_text(number)
        except (TypeError, ValueError):
            skipped += 1
            continue

        derived = None
        if isinstance(public_url, str) and public_url.strip():
            derived = _normalise_slug(public_url.strip("/").split("/")[-1], rules) or None
        slug = derived or _normalise_slug(slug_field if isinstance(slug_field, str) else "", rules)
        if not slug:
            skipped += 1
            continue
        if derived and isinstance(slug_field, str) and slug_field.strip() \
                and _normalise_slug(slug_field, rules) != derived:
            mismatches += 1

        if isinstance(public_url, str) and public_url.strip():
            url = urljoin(base_url, f"{public_url.rstrip('/')}/chapter/{chapter_text}")
        else:
            url = urljoin(base_url, f"/comics/{slug}/chapter/{chapter_text}")

        locked = bool(entry.get(f_locked))
        unlock = _parse_iso(entry.get(f_unlock)) if f_unlock else None
        if unlock is None and locked:
            published = _parse_iso(entry.get(f_published)) if f_published else None
            unlock = published + window if published else None

        title = clean_title(entry.get(f_title)) if f_title else None
        if title:
            titles.setdefault(slug, title)

        items.append({
            "href": url,
            "chapter_text": None,
            "locked": locked,
            "title": title,
            "slug": slug,
            "chapter": chapter,
            "unlock_at": unlock.isoformat() if unlock else None,
        })

    if skipped * 2 > len(entries) or not items:
        return None
    return {"items": items, "titles": titles, "skipped": skipped, "slug_mismatches": mismatches}


def _chapter_from_href(href, rules):
    """(slug, chapter) for a chapter link, using the same segment_count / chapter_regex logic the
    compile step has always used; None when the link is not a chapter link of this site."""
    path = _site_path(href, rules)
    if path is None:
        return None
    segments = path.strip("/").split("/")
    if len(segments) != rules.get("segment_count") or len(segments) < 2:
        return None
    match = re.compile(rules["chapter_regex"]).search("/".join(segments[-2:]))
    if not match:
        return None
    slug = _normalise_slug(segments[1], rules)
    if not slug:
        return None
    return slug, float(match.group(1))


def _href_items(soup, rules, badge_rules):
    """Chapter items from plain <a href> links. Fragments are dropped first (hive/vortex also link
    '/series/x/chapter-5#comment-123' to a chapter's comments, which must never become the chapter URL).
    Duplicate links (a chapter shown in several page sections) are merged; a chapter counts as locked
    if any copy of its link carries the lock class."""
    locked_class = (badge_rules or {}).get("locked_class")
    by_href = {}
    for anchor in soup.find_all("a", href=True):
        href = urldefrag(anchor["href"])[0]
        found = _chapter_from_href(href, rules)
        if found is None:
            continue
        slug, chapter = found
        locked = (locked_class in (anchor.get("class") or [])) if locked_class else False
        if href in by_href:
            by_href[href]["locked"] = by_href[href]["locked"] or locked
            continue
        by_href[href] = {
            "href": href, "chapter_text": None, "locked": locked, "title": None,
            "slug": slug, "chapter": chapter, "unlock_at": None,
        }
    return list(by_href.values())


def _selector_titles(soup, rules):
    """{slug: title} from every element matching title_selector (slug from the element's own href)."""
    selector = rules.get("title_selector")
    if not selector:
        return {}
    attr = rules.get("title_attr")
    titles = {}
    for element in soup.select(selector):
        slug = _slug_from_link(element.get("href"), rules)
        if not slug:
            continue
        title = clean_title(element.get(attr) if attr else element.get_text())
        if title:
            titles.setdefault(slug, title)
    return titles


def _badge_lock_diagnostic(soup, badge_rules):
    """"ok" when the lock icon and the lock class are both absent (a quiet moment) or the class is
    present; "missing" when the icon is on the page but no element has the class (site renamed it)."""
    icons = len(soup.select(badge_rules["marker_selector"])) if badge_rules.get("marker_selector") else 0
    classed = len(soup.find_all(class_=badge_rules["locked_class"])) if badge_rules.get("locked_class") else 0
    return "missing" if icons > 0 and classed == 0 else "ok"


def _parse_li_block_page(soup, rules):
    items, titles = [], {}
    chapter_re = re.compile(rules["chapter_regex"])
    for block in soup.select(rules["item_selector"]):
        identity_tag = block.select_one(rules["identity_selector"])
        identity = identity_tag.get(rules["identity_attr"]) if identity_tag is not None else None
        slug = None
        if identity:
            parts = urlparse(identity).path.strip("/").split("/")
            slug = unquote(parts[-1]).lower() if parts and parts[-1] else None

        title_tag = block.select_one(rules["title_selector"]) if rules.get("title_selector") else None
        title = clean_title(title_tag.get_text()) if title_tag is not None else None
        if slug and title:
            titles.setdefault(slug, title)

        chapter_tag = block.select_one(rules["chapter_text_selector"])
        chapter_text = chapter_tag.get_text(strip=True) if chapter_tag is not None else None
        if not identity or not chapter_text:
            continue
        match = chapter_re.search(chapter_text)
        chapter = float(match.group(1).replace("-", ".")) if match else None
        items.append({
            "href": identity, "chapter_text": chapter_text, "locked": False, "title": title,
            "slug": slug, "chapter": chapter, "unlock_at": None,
        })
    return items, titles


def parse_page(html, rules):
    soup = BeautifulSoup(html, "lxml")
    diagnostics = {"items": 0, "titles": 0, "lock": "n/a"}

    if rules.get("mode") == "html_li_block":
        items, titles = _parse_li_block_page(soup, rules)
    elif rules.get("lock_detection"):
        island = parse_astro_island(html, rules, soup=soup)
        if island is not None:
            items, titles = island["items"], island["titles"]
            diagnostics["lock"] = "ok"
            diagnostics["skipped_entries"] = island["skipped"]
            diagnostics["slug_mismatches"] = island["slug_mismatches"]
        else:
            # Island missing or malformed: alerts must still work, so fall back to plain links.
            items = _href_items(soup, rules, None)
            for item in items:
                item["locked"] = None
            titles = {}
            diagnostics["lock"] = "missing"
    else:
        badge_rules = rules.get("lock_badge")
        items = _href_items(soup, rules, badge_rules)
        titles = _selector_titles(soup, rules)
        if badge_rules:
            diagnostics["lock"] = _badge_lock_diagnostic(soup, badge_rules)

    for item in items:
        if item.get("title") is None:
            item["title"] = titles.get(item["slug"])
    diagnostics["items"] = len(items)
    diagnostics["titles"] = len(titles)
    return {"items": items, "titles": titles, "diagnostics": diagnostics}


def classify_site(diagnostics, fetched_ok, exception):
    """Pure site status: "failed", "degraded" or "ok" (spec 7.8). "skipped" is decided by the caller,
    before any fetch, and is never recorded in site_health."""
    if exception is not None or not fetched_ok or not diagnostics:
        return "failed"
    if diagnostics.get("items", 0) == 0:
        return "failed"
    if diagnostics.get("titles", 0) == 0 or diagnostics.get("lock") == "missing":
        return "degraded"
    return "ok"


# ============================================================================================
# validate_rules: sanity-check site_parse_rules.json. Returns a list of problems (empty = fine).
# ============================================================================================

KNOWN_MODES = ("html", "html_li_block", "json")
TIERS = ("standard", "member")


def _bad_regex(pattern):
    try:
        re.compile(pattern)
        return None
    except (re.error, TypeError):
        return f"not a valid regex: {pattern!r}"


def _bad_selector(selector):
    try:
        soupsieve.compile(selector)
        return None
    except Exception:
        return f"not a valid CSS selector: {selector!r}"


def validate_rules(rules):
    problems = []
    if not isinstance(rules, dict) or not rules:
        return ["rules must be a non-empty object"]

    for key, site in rules.items():
        def bad(message, key=key):
            problems.append(f"{key}: {message}")

        if not isinstance(site, dict):
            bad("site rules must be an object")
            continue
        mode = site.get("mode")
        if mode not in KNOWN_MODES:
            bad(f"unknown mode {mode!r}")
            continue
        if mode == "json":
            continue  # mangadex: not implemented yet, nothing further to check

        if not isinstance(site.get("display_name"), str) or not site["display_name"].strip():
            bad("display_name is missing")
        base_url = site.get("base_url")
        if not isinstance(base_url, str) or urlparse(base_url).scheme not in ("http", "https") \
                or not urlparse(base_url).hostname:
            bad("base_url must be an http(s) URL")

        for pattern_key in ("chapter_regex", "slug_hash_strip_regex"):
            if pattern_key in site and (problem := _bad_regex(site[pattern_key])):
                bad(f"{pattern_key}: {problem}")
        if "chapter_regex" not in site:
            bad("chapter_regex is missing")
        elif _bad_regex(site["chapter_regex"]) is None and re.compile(site["chapter_regex"]).groups < 1:
            bad("chapter_regex needs a capture group for the chapter number")

        patterns = site.get("novel_link_patterns")
        if not isinstance(patterns, list) or not patterns:
            bad("novel_link_patterns is missing")
        else:
            for pattern in patterns:
                if problem := _bad_regex(pattern):
                    bad(f"novel_link_patterns: {problem}")
                elif "slug" not in re.compile(pattern).groupindex:
                    bad(f"novel_link_patterns: pattern has no named group 'slug': {pattern!r}")

        lock_detection, lock_badge = site.get("lock_detection"), site.get("lock_badge")
        if lock_detection and lock_badge:
            bad("lock_detection and lock_badge must not both be set")

        if mode == "html":
            if not isinstance(site.get("segment_count"), int) or site["segment_count"] < 2:
                bad("segment_count must be an integer of at least 2")
        else:  # html_li_block
            for field in ("item_selector", "identity_selector", "identity_attr", "chapter_text_selector"):
                if not site.get(field):
                    bad(f"{field} is missing")
                elif field.endswith("_selector") and (problem := _bad_selector(site[field])):
                    bad(f"{field}: {problem}")
            template = site.get("chapter_url_template")
            if not isinstance(template, str) or "{slug}" not in template or "{chapter}" not in template:
                bad("chapter_url_template must contain {slug} and {chapter}")

        # A title source is required on every site.
        island_title = isinstance(lock_detection, dict) and (lock_detection.get("fields") or {}).get("title")
        if not (site.get("title_selector") or site.get("title_field") or island_title):
            bad("no title source (title_selector, title_field or lock_detection.fields.title)")
        if site.get("title_selector") and (problem := _bad_selector(site["title_selector"])):
            bad(f"title_selector: {problem}")

        if lock_detection:
            if not isinstance(lock_detection, dict):
                bad("lock_detection must be an object")
            else:
                if lock_detection.get("type") != "astro_island":
                    bad("lock_detection.type must be 'astro_island'")
                if not lock_detection.get("list_key"):
                    bad("lock_detection.list_key is missing")
                fields = lock_detection.get("fields") or {}
                for needed in ("chapter", "is_locked"):
                    if not fields.get(needed):
                        bad(f"lock_detection.fields.{needed} is missing")
                if not (fields.get("public_url") or fields.get("slug")):
                    bad("lock_detection.fields needs public_url or slug")
            labels = site.get("tier_labels")
            if not isinstance(labels, dict) or any(not labels.get(t) for t in TIERS):
                bad("a lock_detection site needs tier_labels for 'standard' and 'member'")

        if lock_badge:
            if not isinstance(lock_badge, dict):
                bad("lock_badge must be an object")
            else:
                if not lock_badge.get("locked_class"):
                    bad("lock_badge.locked_class is missing")
                marker = lock_badge.get("marker_selector")
                if not marker:
                    bad("lock_badge.marker_selector is missing")
                elif problem := _bad_selector(marker):
                    bad(f"lock_badge.marker_selector: {problem}")

    return problems



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