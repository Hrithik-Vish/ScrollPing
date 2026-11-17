import time
import re
import json
import sys
import traceback
from datetime import datetime, timezone
from src import admin
from src import database
from src import fetching
from src import parsing
from src import scheduling
from src import telegramMessenger
from src.novel_links import clean_title
from telegram.error import InvalidToken
from urllib.parse import urljoin

# Modes that process_website can handle today.
# json (mangadex) gets added here once it is implemented.
SUPPORTED_MODES = ("html", "html_li_block")

CLAIM_BATCH = 500           # rows taken from the queue per dispatch pass
MAX_DISPATCH_PASSES = 10    # so one run sends at most 5000 rows; the rest waits for the next run
MAX_PRINTED_SKIPPED = 25    # unrecognised links printed per site (the rest are only counted)


def utcnow():
    """The one place the run reads the clock (tests replace it)."""
    return datetime.now(timezone.utc)


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


# =====================================================================
# Compilation: turn parse_page items into "this novel has a newer chapter" rows (spec 7.2).
# Pure: nothing is mutated, nothing is fetched.
#
# row = {"id", "novel_name" (slug), "latest_chap", "chapter_url", "display_title", "access_state",
#        "unlock_at", "web_id"}
# access_state is "locked" / "free" for a known lock state and "unknown" when lock parsing failed.
# web_id is not in the spec's row shape; it is carried so the tier of the recipient on THIS site
# can be looked up when the queue records are built.
# =====================================================================

def _access_state(locked):
    if locked is None:
        return "unknown"
    return "locked" if locked else "free"


def _describe_skipped(item):
    if item.get("chapter_text") is not None:
        return f"{item.get('href')} | {item.get('chapter_text')}"
    return item.get("href")


def _pick_winners(items, fetched_novels):
    """Highest chapter per tracked slug (the first one seen wins a tie), plus the items we ignored."""
    winners, skipped = {}, []
    for item in items:
        slug, chapter = item.get("slug"), item.get("chapter")
        if not slug or chapter is None or slug not in fetched_novels:
            skipped.append(_describe_skipped(item))
            continue
        best = winners.get(slug)
        if best is None or chapter > best["chapter"]:
            winners[slug] = item
    return winners, skipped


def _collect_rows(winners, fetched_novels, chapter_url_for):
    """Rows for the winners that are newer than what the database has, in the database's order."""
    rows = []
    for name, info in fetched_novels.items():
        item = winners.get(name)
        if item is None or float(item["chapter"]) <= float(info["latest_chap"]):
            continue
        rows.append({
            "id": info["id"],
            "novel_name": name,
            "latest_chap": item["chapter"],
            "chapter_url": chapter_url_for(item),
            "display_title": clean_title(item.get("title")),
            "access_state": _access_state(item.get("locked")),
            "unlock_at": item.get("unlock_at"),
            "web_id": info.get("web_id"),
        })
    return rows


def compilation_fnct(items, fetched_novels, rules):
    """html sites. items come from parsing.parse_page. Returns (rows, skipped)."""
    winners, skipped = _pick_winners(items, fetched_novels)
    base_url = rules["base_url"]
    return _collect_rows(winners, fetched_novels, lambda item: urljoin(base_url, item["href"])), skipped


def compilation_fnct_li_block(items, fetched_novels, rules):
    """html_li_block sites (mgeko): the chapter URL comes from a template, not from a link."""
    winners, skipped = _pick_winners(items, fetched_novels)
    chapter_re = re.compile(rules["chapter_regex"])

    def chapter_url_for(item):
        token = chapter_re.search(item["chapter_text"] or "").group(1)      # "1-1" stays "1-1" in the URL
        return urljoin(rules["base_url"], rules["chapter_url_template"].format(slug=item["slug"], chapter=token))

    return _collect_rows(winners, fetched_novels, chapter_url_for), skipped


def find_title_updates(titles, fetched_novels, skip_ids=()):
    """Tracked novels whose scraped title differs from the stored one. A novel that already has a
    chapter row is left out: the batch update writes its title (spec 7.6)."""
    updates = []
    for slug, info in fetched_novels.items():
        title = clean_title(titles.get(slug))
        if title and title != clean_title(info.get("display_title")) and info["id"] not in skip_ids:
            updates.append({"id": info["id"], "display_title": title})
    return updates


# =====================================================================
# One site -> one result object (spec 7.8)
# =====================================================================

def _site_result(site_id, web_url, status="ok", error=None):
    return {
        "site_id": site_id, "web_url": web_url, "status": status, "error": error,
        "rows": [], "title_updates": [], "skipped": [],
    }


def _status_error(status, diagnostics):
    """A short reason for the health table: no URLs, no names."""
    if status == "failed":
        return "no chapter items found"
    reasons = []
    if diagnostics.get("titles", 0) == 0:
        reasons.append("no titles found")
    if diagnostics.get("lock") == "missing":
        reasons.append("lock detection missing")
    return "; ".join(reasons) or None


def process_website(site_id, web_url, site_parse_rules):
    max_attempts = 3
    result = _site_result(site_id, web_url)

    site_rules = site_parse_rules.get(web_url)
    if site_rules is None:
        print(f"{site_id}: {web_url}: no parse rules found, skipping")
        result["status"] = "skipped"
        return result
    if site_rules.get("mode") not in SUPPORTED_MODES:
        print(f"{site_id}: {web_url}: mode '{site_rules.get('mode')}' is not supported yet, skipping")
        result["status"] = "skipped"
        return result

    html_structure = retry(fetching.fetch_html, max_attempts, web_url)
    if not html_structure:
        result["status"], result["error"] = "failed", "fetch failed"
        return result

    page = parsing.parse_page(html_structure, site_rules)
    diagnostics = page["diagnostics"]
    result["status"] = parsing.classify_site(diagnostics, True, None)
    result["error"] = _status_error(result["status"], diagnostics)
    if result["status"] == "failed":
        return result

    novels_list = retry(database.fetch_novels, max_attempts, site_id)
    if not novels_list:
        return result                       # nothing tracked on this site: nothing to compare

    fetched_novels = {
        novel['novel_name']: {k: v for k, v in novel.items() if k != 'novel_name'}
        for novel in novels_list
    }

    compile_fnct = compilation_fnct_li_block if site_rules["mode"] == "html_li_block" else compilation_fnct
    rows, skipped = compile_fnct(page["items"], fetched_novels, site_rules)

    result["rows"] = rows
    result["skipped"] = skipped
    result["title_updates"] = find_title_updates(page["titles"], fetched_novels, {row["id"] for row in rows})

    for data in skipped[:MAX_PRINTED_SKIPPED]:
        print(f"{site_id}: {web_url}: {data}")
    if len(skipped) > MAX_PRINTED_SKIPPED:
        print(f"{site_id}: {web_url}: ... and {len(skipped) - MAX_PRINTED_SKIPPED} more links not tracked")

    return result


def finding_updated_novels(site_id_list, web_url_list, site_parse_rules):
    """One result object per site. A site that raises becomes a 'failed' result carrying only the
    error class name (never a traceback or a message) so the other sites still run."""
    results = []
    for site_id, web_url in zip(site_id_list, web_url_list):
        try:
            results.append(process_website(site_id, web_url, site_parse_rules))
        except Exception as exc:
            print(f"{site_id}: {web_url}: unexpected error, skipping this site for this run")
            traceback.print_exc()
            results.append(_site_result(site_id, web_url, "failed", type(exc).__name__))
    return results


# =====================================================================
# Delivery: claim from the queue -> send -> acknowledge (spec section 10)
# =====================================================================

class QueueDelivery:
    """What the messenger calls after each send. It holds no logic: every method is one database call."""

    def ack_sent(self, ids):
        return database.ack_sent(ids)

    def release(self, ids, error_text):
        return database.release(ids, error_text)

    def mark_dead(self, ids, error_text):
        return database.mark_dead(ids, error_text)

    def blocked(self, chat_id, ids):
        # also cancels the user's rows that are still 'sending', so the ids are not needed here
        return database.handle_blocked_users(chat_id)


def group_claimed(claimed):
    """{telegram_user_id: [claimed row, ...]} keeping the order the queue returned them in."""
    batches = {}
    for row in claimed:
        telegram_id = row.get("telegram_user_id")
        if telegram_id is None:
            continue
        batches.setdefault(telegram_id, []).append(row)
    return batches


def _count(value):
    return len(value) if isinstance(value, (list, tuple, set, dict)) else int(value or 0)


SUMMARY_KEYS = ("sent", "blocked", "bad_request", "failed", "retried")


def merge_summaries(total, summary):
    for key in SUMMARY_KEYS:
        total[key] = total.get(key, 0) + _count(summary.get(key))
    return total


def dispatch_queue(delivery=None, rules=None):
    """Claim due rows and send them, repeating while there is more (at most MAX_DISPATCH_PASSES).
    Runs every time, even when nothing new was detected: the queue also holds retries and delayed
    unlocks from earlier runs. Rows left 'sending' if the process dies are recovered by the next run.

    The loop STOPS after a pass in which any row was given back (a transient Telegram or network
    failure). A released row is 'pending' and due again at once, so the next pass of the same run
    would claim it a second time, and every claim counts an attempt: an outage of a minute or two
    used to burn through the attempt cap and mark the alerts dead. Now a failing row is claimed once
    per run, and its remaining attempts stretch over the following hourly runs and the 24 hours it
    is allowed to live. If more rows were still due than one pass can claim, they wait for the next run."""
    delivery = delivery or QueueDelivery()
    total = {key: 0 for key in SUMMARY_KEYS}
    for _ in range(MAX_DISPATCH_PASSES):
        claimed = database.claim_due(CLAIM_BATCH)
        if not claimed:
            break
        summary = telegramMessenger.send_all(group_claimed(claimed), delivery, rules=rules, now=utcnow())
        merge_summaries(total, summary)
        given_back = _count(summary.get("failed"))
        if given_back:
            print(f"dispatch: {given_back} row(s) were given back to the queue, stopping early; "
                  "they are retried on the next run")
            break
    return total


def run_admin_tasks(rules, now):
    """Step 11: tell the admins about sites that keep failing, and send the daily digest when it is due.
    Never raises into run(): a problem here must not fail a scrape that otherwise worked."""
    admin_ids = admin.get_admin_chat_ids()
    if not admin_ids:
        print("admin: ADMIN_TELEGRAM_USER_IDS is not set, alerts and the digest are skipped")
        return

    # 12.2  immediate alerts for sites that failed 3+ runs in a row and were not reported yet
    alerts = database.fetch_site_alerts()
    reported = []
    for site in alerts:
        result = telegramMessenger.send_admin(admin_ids, admin.build_alert(site, rules))
        if result["delivered"] > 0:
            reported.append(site["web_id"])           # only a delivered alert may be marked as reported
    if reported:
        database.ack_site_alerts(reported)
    print(f"admin: {len(alerts)} site alert(s), {len(reported)} delivered")

    # 12.4  the digest is due once per day after 23:35 IST
    try:
        last_sent = database.get_app_state(admin.DIGEST_STATE_KEY)
    except Exception:
        print("admin: could not read the digest state, digest skipped this run")
        return
    if not admin.digest_due(now, last_sent):
        return
    text = admin.build_digest(database.fetch_site_health_rows(), database.queue_stats(), now, rules)
    result = telegramMessenger.send_admin(admin_ids, text)
    if result["delivered"] > 0:
        stored = database.set_app_state(admin.DIGEST_STATE_KEY, now.isoformat())
        print(f"admin: digest sent to {result['delivered']} admin(s)" + ("" if stored else " (could not store the marker)"))
    else:
        print("admin: digest could not be delivered, it will be tried again next run")


def print_send_summary(summary):
    line = (
        f"telegram: sent={_count(summary.get('sent'))} blocked={_count(summary.get('blocked'))} "
        f"bad_request={_count(summary.get('bad_request'))} failed={_count(summary.get('failed'))}"
    )
    if "retried" in summary:
        line += f" retried={_count(summary.get('retried'))}"
    print(line)


# =====================================================================
# The run (spec section 9)
# =====================================================================

def run():
    # 1
    status, row_id = database.insert_scraper_logs()
    if status != True:
        print("could not insert into scraper_logs, stopping the run")
        sys.exit(1)

    # 2  housekeeping first: recover rows a dead run left 'sending', cancel ended subscriptions, expire old rows
    maintained = database.maintain_queue()
    if maintained["ok"]:
        print(f"queue maintenance: recovered={maintained['recovered']} cancelled={maintained['cancelled']} expired={maintained['expired']}")
    else:
        print("queue maintenance failed, continuing")

    # 3
    websites_list = database.fetch_websites()
    site_id = [item['id'] for item in websites_list]
    web_url = [item['web_domain_name'] for item in websites_list]

    with open('site_parse_rules.json', 'r') as file:
        site_parse_rules = json.load(file)
    problems = parsing.validate_rules(site_parse_rules)
    for problem in problems:
        print(f"site_parse_rules.json: {problem}")

    # 4
    results = finding_updated_novels(site_id, web_url, site_parse_rules)

    # 5  site health (fail-soft)
    counts = {"ok": 0, "degraded": 0, "failed": 0, "skipped": 0}
    record_failures = 0
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        if result["status"] == "skipped":
            continue
        if database.record_site_result(result["site_id"], result["status"], result["error"]) is None:
            record_failures += 1
    print(f"sites: ok={counts['ok']} degraded={counts['degraded']} failed={counts['failed']} "
          f"skipped={counts['skipped']} (health not recorded for {record_failures})")

    # 6  titles never touch updated_at (D4); fail-soft
    title_updates = [update for result in results for update in result["title_updates"]]
    if title_updates:
        changed = database.sync_novel_titles(title_updates)
        print(f"titles: {len(changed)} of {len(title_updates)} updated")

    rows = [row for result in results for row in result["rows"]]
    if rows:
        # 7  who reads these novels
        web_id_by_novel = {row["id"]: row.get("web_id") for row in rows}
        fetched = database.fetch_subscribers_with_preferences(list(web_id_by_novel), web_id_by_novel)
        held_back = set(fetched.failed_novel_ids)

        # 8  queue first, novels second (D1): a crash between the two only causes re-detection,
        #    and the unique key makes the re-insert a no-op
        records = scheduling.build_queue_records(
            [row for row in rows if row["id"] not in held_back],
            fetched.recipients, fetched.tiers, utcnow(),
        )
        if records:
            inserted = database.insert_queue_records(records)
            held_back |= inserted["failed_novel_ids"]
            print(f"queue: {len(records)} records, {inserted['inserted']} new, {inserted['failed']} failed to insert")
        if held_back:
            print(f"{len(held_back)} novel(s) held back this run (queue or recipients could not be written); they are detected again next run")

        # 9  a failed id only means the write did not happen; it never blocks an alert
        failed_ids = database.update_novels_table([row for row in rows if row["id"] not in held_back])
        print(f"novels: {len(rows) - len(held_back) - len(failed_ids or [])} of {len(rows)} updated")

    # 10
    notify_failed = False
    try:
        send_summary = dispatch_queue(rules=site_parse_rules)
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

    # 11  admin alerts and the daily digest (a failure here never fails the run)
    try:
        run_admin_tasks(site_parse_rules, utcnow())
    except Exception as exc:
        print(f"admin: step failed ({type(exc).__name__}), continuing")
        traceback.print_exc()

    # 12
    database.update_scraper_logs(row_id)

    # 13
    if notify_failed:
        sys.exit(1)


if __name__ == "__main__":
   run()
