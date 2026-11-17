# V1 burn-in runbook (Phase 9)

How to take the finished V1 code live safely, what to look at in the first runs, and what to measure over the month. Everything in the repository is built and tested (1,042 tests, the SQL functions, the migration). What is left can only be verified against the real services, in this order:

1. a read-only **preview** of what the first run would do (nothing is written or sent)
2. one **local run** with your real `.env`, then a second one that must find nothing
3. **merge to `main`** and watch the first three scheduled runs
4. one deliberate **fault test**
5. the month of measurements

Keep each step's output; if something looks off, send it over before moving on. Related: [[deployment]] (the rollout order and rollback), [[main]] (what each run does), [[admin]] (alerts and digest), [[schema]].

> **Who can receive a message?** Before anything real runs, check that only accounts you control are subscribed (query 1 below). The first run sends real Telegram messages to every active user who follows a novel that has something new. With only your test accounts that is harmless; with anyone else it is not.

---

## 0. Useful queries (Supabase SQL editor, read-only)

**1. Who could be messaged, and about what**
```sql
select u.username, u.telegram_user_id, u.status, count(*) as novels_followed
  from public.user_subscriptions s
  join public.users u on u.id = s.user_id
 where s.reading_status = 'reading'
 group by u.username, u.telegram_user_id, u.status
 order by novels_followed desc;
```

**2. The queue at a glance (last 24 hours)**
```sql
select status, count(*) from public.pending_notifications
 where created_at > now() - interval '24 hours' group by status order by status;
select * from public.queue_stats_24h();
```

**3. Site health**
```sql
select w.web_domain_name, h.last_status, h.consecutive_failures, h.failing_since, h.last_error, h.last_ok_at
  from public.site_health h join public.websites w on w.id = h.web_id order by w.web_domain_name;
```

**4. Recent runs**
```sql
select last_run_timestamp, run_status from public.scraper_logs order by last_run_timestamp desc limit 10;
```

**5. One novel's queue history** (replace the slug)
```sql
select u.username, q.chapter, q.access_state, q.unlock_at, q.due_at, q.status, q.attempts, q.sent_at
  from public.pending_notifications q
  join public.users u on u.id = q.user_id
  join public.novels n on n.id = q.novel_id
 where n.novel_name = 'killer-pietro' order by q.created_at desc;
```

---

## 1. Before you merge

### 1.1 Tests and environment
```
python -m pytest -q                     # 1042 passed, 9 skipped (the 9 are the manual smoke tests)
```
Your `.env` needs `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `SUPABASE_PUBLISHABLE_KEY` (any non-empty value), `TELEGRAM_BOT_TOKEN` and `ADMIN_TELEGRAM_USER_IDS`. The GitHub repository secrets need the same (`ADMIN_TELEGRAM_USER_IDS` was added). Optional but worth doing once: the two manual smoke tests (`docs/pytest.md`).

### 1.2 Preview: what would the first run do? (read-only)
```
python -m scripts.preview_run                 # all sites
python -m scripts.preview_run --site hive     # one site
```
It fetches the live pages, compares them with your database, finds the recipients and prints what a run **would** queue. It never writes and never sends (a test enforces that; it fails if any write or send function is called). It prints titles, so keep the output private.

Expected shape:
```
SITES
  ok        https://asurascans.com/: 6 new chapter(s), 12 title(s) to store
  ok        https://hivetoons.org/: 0 new chapter(s), 4 title(s) to store
  ...

CHAPTERS THAT WOULD BE QUEUED (newest chapter per novel)
  Killer Pietro Ch. 156 [locked until 2026-11-14T08:41:27+00:00] -> 1 now, 1 later
  Lookism Ch. 632 [free] -> 2 now   <-- JUMP +32
  Some Novel Ch. 12 [free] -> 2 now   <-- FIRST SIGHT

WOULD QUEUE 6 record(s) for 2 user(s); 0 novel(s) held back.
Nothing was written and nothing was sent.
```
How to read it:
- **FIRST SIGHT** is a novel the database has never scraped (`latest_chap = -1`): it is alerted **once**, with its current chapter. Expected for anything you added recently and has not been scraped yet.
- **JUMP +N** is a novel that moved more than 5 chapters since the database last saw it (it was not scraped for a while). Only the **newest** chapter is ever queued, never the old ones, so a flood of old chapters cannot happen; the flag is there so nothing surprises you.
- **`1 now, 1 later`** on a locked asura chapter is the member-versus-standard behaviour: the member is told now, the standard reader at the unlock time.
- **`nobody reads it`**: nobody active follows that novel; it will still be advanced.
- **`[HELD BACK ...]`**: the recipients could not be read; the novel is not advanced and is detected again next run.
- A site that is `failed` or `degraded` shows the reason in brackets.

**Acceptance for this step:** every site `ok` (mangadex does not appear: it is skipped), the number of records matches what you expect from your test accounts, no unexplained FIRST SIGHT or JUMP lines. A `degraded` site is acceptable if you understand why (for example `no titles found`); a `failed` one means fix that first (query 3 and the troubleshooting table below).

### 1.3 One real run, locally
Pick a quiet moment: the scheduled runs start at minute 5 (cron-job.org) and minute 35 (GitHub's backup), so run locally around minute 15 to 25 or 45 to 55. The scheduler is still running the **old** code from `main` against the same database, and while they are compatible, two runs at the same moment could both notify.
```
python main.py
```
Expected output (counts only; the order is the order of the steps):
```
queue maintenance: recovered=0 cancelled=0 expired=0
sites: ok=5 degraded=0 failed=0 skipped=1 (health not recorded for 0)
titles: 12 of 12 updated
queue: 6 records, 6 new, 0 failed to insert
novels: 3 of 3 updated
telegram: sent=6 blocked=0 bad_request=0 failed=0 retried=0
admin: 0 site alert(s), 0 delivered
admin: digest sent to 1 admin(s)
```
Notes:
- `skipped=1` is mangadex (no rules for its mode yet).
- **The very first run sends the daily digest immediately**, because there is no "last sent" marker yet. After that it comes once a day after 23:35 IST.
- Check your Telegram test accounts: one message per user, titles as links, the lock badge on asura chapters for members, the Hive/Asura sections named properly, only the first chunk making a sound.
- Check the admin message: sites listed, queue numbers.
- Then in Supabase: query 2 (rows `sent`), query 3 (a row per site), query 4 (this run `success`).

### 1.4 The second run must send nothing
Immediately run it again:
```
python main.py
```
Expected: `queue: …` and `novels: …` lines absent (or `0 records`), `telegram: sent=0 …`, `admin: 0 site alert(s)`, and **no digest** (the marker is stored). If it sends anything, stop and send me the output.

---

## 2. Merge and watch the first three runs

Merging to `main` starts two things: Render redeploys the bot (once CI is green) and the next scheduled scraper run uses the new code. Open the Actions tab and watch the first three runs (the minute-5 run from cron-job.org, the minute-35 backup, the next minute-5).

| Run | Look for |
|---|---|
| 1st | `scraper (workflow_dispatch)` is green. The log shows the lines from 1.3, mostly zeros (the local run already advanced everything). No `Traceback`. If you skipped the local run, this is where the first-run lines appear. |
| 2nd | The minute-35 backup also green and quiet. It is allowed to find nothing. |
| 3rd | Still green; the digest arrives once the day's 23:35 IST has passed. |

Also check the bot: `/start`, `/list` (hub → site → novel), `/settings`, follow an asura novel and answer the account-type question with each test account.

**Red run?** `notify_failed` runs exit 1 for a rejected or missing Telegram token or an unexpected send error; the log line says which. A failed `scraper_logs` insert exits 1 too. Everything else (one site down, some users unreachable) stays green and shows up in the health table and the digest instead.

---

## 3. The deliberate fault test (spec 16.7)

Proves that a run that died mid-send is recovered. Use your own test user and any novel they follow.
```sql
insert into public.pending_notifications
  (user_id, novel_id, chapter, chapter_url, status, attempts, last_attempt_at)
values ('<USER_ID>', '<NOVEL_ID>', 99998, 'https://example.com/fault-test', 'sending', 1, now() - interval '1 hour')
returning id;
```
Run `python main.py` (or wait for the next run). Expected: `queue maintenance: recovered=1 …`, one message to that user for "Ch. 99998", and:
```sql
select status, attempts, sent_at from public.pending_notifications where chapter = 99998;   -- sent, attempts 2
delete from public.pending_notifications where chapter = 99998;
```

---

## 4. The month: what to measure (spec section 16)

| What | How |
|---|---|
| **Delivery health** | the daily digest: delivered, retried, dead, cancelled, waiting. `dead` should stay near zero; look into any non-zero day (query 2, query 5) |
| **Site health** | query 3 and the digest: how often each site is degraded or failed, and for how long |
| **Missed detections** | pick 5 to 10 novels across sites and compare the real release time on the site with the alert time. A large gap means chapters fall off the listing between runs |
| **Asura early vs free** | with one member and one standard account: the member is alerted at once with "free in ~Xh"; the standard account within about an hour after the unlock, one alert each (query 5 shows `due_at` and `sent_at`) |
| **Check B** | once, watch a real asura unlock: re-run the preview after the unlock time and confirm the chapter now shows `[free]`, i.e. the site flips its premium flag at the unlock time |
| **Resources** | Actions minutes (watch run time), Render hours, Supabase size (`pending_notifications` rows and the 30/60-day cleanup) |
| **Run duration** | the Actions run time as subscriptions grow from 20 towards 150 |

Accurate wording for the README and a resume, once you have the numbers: "Designed for a few hundred users; load-tested at 150 subscriptions over a month on free tiers."

---

## 5. Troubleshooting

| You see | It means | Do |
|---|---|---|
| `sites: … failed=1` and a health row with `fetch failed` | the site is down, blocked, or returned nothing | wait; after 3 runs in a row the admin gets an alert. If it persists, capture the page from a runner and compare with `tests/fixtures` |
| a site `degraded` with `no titles found` | the title selector stopped matching | alerts still work (titles are guessed from slugs). Update `title_selector` in `site_parse_rules.json` |
| `degraded` with `lock detection missing` | hive/vortex renamed the lock class, or asura's page island changed | alerts still work, badges are missing. Update `lock_badge` / `lock_detection` |
| `… novel(s) held back this run` | recipients or queue rows could not be written | harmless for one run; if every run, check the Supabase status |
| `novels: 0 of N updated` but alerts arrived | the batch update failed | nothing is lost (the queue has the alerts); the next run detects them again and the unique key prevents duplicates |
| `queue maintenance: recovered=N` | a previous run died or stalled while sending | expected after a cancelled or crashed run; the rows are retried |
| `telegram: … failed=N` | Telegram or the network kept failing | the rows go back to the queue and are retried on later runs (one attempt per run, up to 24 attempts or 24 hours; the run stops claiming as soon as rows were given back, so an outage never burns the attempts in one run) |
| `telegram: … bad_request=N` | Telegram refused those messages even as plain text | investigate the row (query 5); only that row is dead |
| `telegram: … blocked=N` | users blocked the bot | they are paused automatically; `/start` brings them back |
| `admin: ADMIN_TELEGRAM_USER_IDS is not set` | the secret is missing or empty | add it (alerts and the digest are skipped meanwhile) |
| run exits red: `bot token was rejected` | the token is wrong or revoked | fix `TELEGRAM_BOT_TOKEN` in the repository secrets; queued rows are recovered on the next run |

## 6. Rolling back
Revert the merge on `main`; the old code works with the new schema (the migration only added things). Rows already in the queue are then no longer delivered or recovered (the old code does not know the queue), and the novels they refer to were already advanced, so those few alerts would be lost. This is why the local run in 1.3 comes first: it shows the behaviour before the scheduler depends on it.
