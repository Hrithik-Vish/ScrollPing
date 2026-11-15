"""Preview what the next scraper run WOULD do, without doing any of it. Meant for your own terminal.

    python -m scripts.preview_run                 # every supported site
    docker exec -it scrollping-dev python -m scripts.preview_run     # for those who use docker 
    python -m scripts.preview_run --site hive     # only sites whose URL contains "hive"
    docker exec -it scrollping-dev python -m scripts.preview_run --site hive    # for those who use docker 

It fetches the live pages (like a real run), compares them with your database, finds the recipients and
works out the queue records and their due times, then prints a report. It NEVER writes: no queue rows,
no novel updates, no site health, no titles, no Telegram message, no admin message. Reading only.

It prints novel titles and counts, so keep its output to yourself (never run it in CI, where the logs are
public). A real run prints counts only.

Use it before the first live run (docs/burnin.md): the line to check is "WOULD QUEUE ... for ... user(s)".
A novel the database has never scraped (latest chapter -1) is flagged FIRST SIGHT: it will be alerted
once, with its current chapter. Only a novel's newest chapter is ever queued, never its old ones, so a
"flood of old chapters" is impossible by design; a big jump is flagged only to catch surprises.
"""

import argparse
import json
import sys

import main
from src import database, parsing, scheduling
from src.novel_links import title_for

BIG_JUMP = 5          # chapters; larger jumps are flagged so you can look at them


def _stored_chapters(site_id):
    """{slug: latest_chap} as the database has it now (read-only)."""
    try:
        return {n["novel_name"]: n["latest_chap"] for n in database.fetch_novels(site_id) or []}
    except Exception:
        return {}


def _flag(old, new):
    try:
        old, new = float(old), float(new)
    except (TypeError, ValueError):
        return ""
    if old < 0:
        return "FIRST SIGHT"
    if new - old > BIG_JUMP:
        return f"JUMP +{new - old:g}"
    return ""


def build_report(sites, rules, now, only=None):
    """Run the read-only half of a scrape and return (lines, summary). Separate from main() for testing."""
    lines, summary = [], {"records": 0, "users": set(), "rows": 0, "sites_failed": 0}
    results = []
    stored = {}
    for site in sites:
        if only and only.lower() not in site["web_domain_name"].lower():
            continue
        stored[site["id"]] = _stored_chapters(site["id"])
        results.extend(main.finding_updated_novels([site["id"]], [site["web_domain_name"]], rules))

    lines.append("SITES")
    for r in results:
        note = f" ({r['error']})" if r["error"] else ""
        lines.append(f"  {r['status']:<9} {r['web_url']}{note}: {len(r['rows'])} new chapter(s), {len(r['title_updates'])} title(s) to store")
        summary["sites_failed"] += r["status"] == "failed"

    rows = [row for r in results for row in r["rows"]]
    summary["rows"] = len(rows)
    if not rows:
        lines += ["", "WOULD QUEUE 0 records: nothing new, nothing would be sent."]
        return lines, summary

    web_id_by_novel = {row["id"]: row.get("web_id") for row in rows}
    fetched = database.fetch_subscribers_with_preferences(list(web_id_by_novel), web_id_by_novel)
    held = set(fetched.failed_novel_ids)
    records = scheduling.build_queue_records([row for row in rows if row["id"] not in held], fetched.recipients, fetched.tiers, now)
    by_novel = {}
    for rec in records:
        by_novel.setdefault(rec["novel_id"], []).append(rec)

    lines += ["", "CHAPTERS THAT WOULD BE QUEUED (newest chapter per novel)"]
    for r in results:
        for row in r["rows"]:
            recs = by_novel.get(row["id"], [])
            now_n = sum(1 for x in recs if scheduling.parse_utc(x["due_at"]) <= now)
            later_n = len(recs) - now_n
            flag = _flag(stored.get(r["site_id"], {}).get(row["novel_name"]), row["latest_chap"])
            state = row["access_state"] + (f" until {row['unlock_at']}" if row["access_state"] == "locked" and row["unlock_at"] else "")
            who = f"{now_n} now" + (f", {later_n} later" if later_n else "") if recs else "nobody reads it"
            held_note = "  [HELD BACK: recipients unreadable]" if row["id"] in held else ""
            lines.append(f"  {title_for(row)} Ch. {row['latest_chap']:g} [{state}] -> {who}" + (f"   <-- {flag}" if flag else "") + held_note)

    users = {rec["user_id"] for rec in records}
    summary["records"], summary["users"] = len(records), users
    lines += ["", f"WOULD QUEUE {len(records)} record(s) for {len(users)} user(s); {len(held)} novel(s) held back.",
              "Nothing was written and nothing was sent."]
    return lines, summary


def main_cli(argv=None):
    parser = argparse.ArgumentParser(description="Read-only preview of the next scraper run.")
    parser.add_argument("--site", help="only sites whose URL contains this text")
    args = parser.parse_args(argv)

    with open("site_parse_rules.json", encoding="utf-8") as f:
        rules = json.load(f)
    for problem in parsing.validate_rules(rules):
        print(f"site_parse_rules.json: {problem}")

    lines, _ = build_report(database.fetch_websites(), rules, main.utcnow(), args.site)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main_cli())
