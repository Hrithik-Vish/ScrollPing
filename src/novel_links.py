"""novel_links.py - turns a link a user pastes into (site, slug).

    parse_novel_link("https://asurascans.com/comics/nano-machine-ab39de10/chapter/1", rules)
    -> {"ok": True, "site_key": "https://asurascans.com/", "slug": "nano-machine"}

    parse_novel_link("https://example.com/x", rules)
    -> {"ok": False, "reason": "unsupported_site"}

Everything site-specific lives in site_parse_rules.json, never here:
  base_url             scheme + host of the site (its host is what a pasted link is matched on)
  novel_link_patterns  regexes (each with a named group "slug") run against the link's path
  slug_hash_strip_regex  optional; the same hash-stripping compilation_fnct applies, so the
                         slug stored for a user matches the slug the scraper finds

Pure functions: no network, no database. A pasted link is never fetched.

Failure reasons: empty, too_long, not_a_link, unsupported_site, not_a_novel_link, bad_slug
"""

import re
from urllib.parse import unquote, urlparse

MAX_LINK_LEN = 300
MAX_SLUG_LEN = 150
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def normalize_host(host):
    host = (host or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def supported_sites(rules):
    """Sites a user can add novels from: those with both a base_url and link patterns.
    Returns [{"key": <rules key>, "host": <display host>}] in rules order."""
    sites = []
    for key, site_rules in rules.items():
        base_url = site_rules.get("base_url")
        if base_url and site_rules.get("novel_link_patterns"):
            sites.append({"key": key, "host": normalize_host(urlparse(base_url).hostname)})
    return sites


def parse_novel_link(text, rules):
    text = (text or "").strip()

    if not text:
        return {"ok": False, "reason": "empty"}
    if len(text) > MAX_LINK_LEN:
        return {"ok": False, "reason": "too_long"}
    if re.search(r"\s", text):
        return {"ok": False, "reason": "not_a_link"}

    if "://" not in text:
        text = "https://" + text

    try:
        parsed = urlparse(text)
        port = parsed.port
    except ValueError:
        return {"ok": False, "reason": "not_a_link"}

    if parsed.scheme not in ("http", "https") or not parsed.hostname or port not in (None, 80, 443):
        return {"ok": False, "reason": "not_a_link"}

    host = normalize_host(parsed.hostname)
    site_key = None
    site_rules = None
    for site in supported_sites(rules):
        if site["host"] == host:
            site_key, site_rules = site["key"], rules[site["key"]]
            break

    if site_rules is None:
        return {"ok": False, "reason": "unsupported_site"}

    path = unquote(parsed.path)
    slug = None
    for pattern in site_rules["novel_link_patterns"]:
        match = re.match(pattern, path)
        if match:
            slug = match.group("slug").lower()
            break

    if slug is None:
        return {"ok": False, "reason": "not_a_novel_link"}

    hash_regex = site_rules.get("slug_hash_strip_regex")
    if hash_regex:
        slug = re.sub(hash_regex, "", slug)

    if not slug or len(slug) > MAX_SLUG_LEN or not SLUG_RE.match(slug):
        return {"ok": False, "reason": "bad_slug"}

    return {"ok": True, "site_key": site_key, "slug": slug}
