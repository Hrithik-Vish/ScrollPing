# admin.py

## Purpose
The pure helpers for the admin side of the scraper: who the admins are, when the daily digest is due, and the text of the alerts and the digest. No network, no database, no clock (`now` is passed in). [[main]]'s `run_admin_tasks` does the reading, sending and acknowledging; [[telegramMessenger]].`send_admin` does the sending.

All times shown to an admin are **Indian Standard Time** (UTC+05:30), written like `09:35 AM`, with the date added when it is not today (`10 Nov, 09:35 AM`). All text is Telegram HTML and every value that is not literal text is escaped.

## Admin ids
`get_admin_chat_ids(env=None)` reads `ADMIN_TELEGRAM_USER_IDS` (`123,456`). Anything that is not a whole number is ignored and duplicates are dropped; unset or empty gives `[]`, and then alerts and the digest are skipped with one printed line. A strict pattern is used because `"--5".lstrip("-").isdigit()` is true but `int("--5")` raises. Negative ids (group chats) are fine. `tests/conftest.py` forces the variable to empty during tests, so a real id in a developer's `.env` can never make a test send an admin message.

## Immediate alerts
A site that has failed **three or more runs in a row** and has not been reported yet triggers `build_alert`:
```
🚨 ScrollPing alert
Asura Scans has failed 4 runs in a row.
Since: 10 Nov, 07:35 PM IST
Error: fetch failed
```
(the error is in a monospaced box and cut to 300 characters). The alert goes to every admin; the site is marked reported only if **at least one admin received it**, so an undelivered alert is retried on the next run. When the site recovers, its counters reset and a later failure streak alerts again.

## The daily digest
- **When.** Once per day, after **23:35 IST**. `latest_digest_target(now)` is the most recent 23:35 IST that is not in the future; `digest_due(now, last_sent)` is true when nothing has been sent since that target. A run that starts late (after midnight) still sends that day's digest, a failed send leaves the marker alone so the next run retries, and an unreadable marker counts as "never sent". The marker is `app_state.last_digest_sent_at`, written only after at least one admin received the digest.
- **Content.** A header with the IST time. If every site is healthy, one line (`✅ All 5 sites healthy`); otherwise one line per site, the first matching case winning:
  1. currently failing: `❌ Currently failing since 07:35 PM (5 runs) · error`
  2. failed within the last 24 hours and now ok or degraded: `⚠️ Issue resolved · failed 08:35 AM, resolved 09:35 AM`
  3. degraded: `🟡 Degraded · reason`
  4. otherwise `✅ Healthy`
  Then the queue numbers: delivered, retried, dead and cancelled in the last 24 hours, and how many are waiting or in flight. If the numbers cannot be read, a line says so.
- **Left out on purpose:** an outage *duration*. After a recovery the database keeps only the last failure time and the resolve time, so a figure would understate long outages; the two timestamps are shown instead. Keeping the outage start until recovery is a small SQL change, listed on the roadmap.

## Testing
`tests/test_admin.py` (pure): id parsing cases, the schedule including a **simulated week** with cron runs at :05 and :35 of every hour (exactly one digest per day, at the first run after 23:35 IST), IST formatting edge cases (12 AM, 12 PM), every digest case and precedence, escaping, and length limits. `tests/test_admin_step.py` covers `run_admin_tasks` with the database and Telegram faked: alerts are acknowledged only when delivered, the digest marker is written only when delivered, an unreadable marker skips the digest, and alerts still go out first. `tests/test_run.py` checks step 11's position and that a problem in it never fails a run.

## Related notes
[[main]] · [[telegramMessenger]] · [[database]] · [[schema]] · [[scheduling]] · [[deployment]]
