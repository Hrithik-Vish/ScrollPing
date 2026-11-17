# telegramMessenger.py

**Status:** Working: tested offline (`tests/test_telegram_messenger.py`) and with real messages through the manual smoke tests in `tests/test_smokeTest.py`. Used by [[main]] through `send_all` (users) and `send_admin` (alerts and the digest). Covered by [[pytest]].

## Purpose
Sends the Telegram messages for the scraper: chapter alerts to users, and alerts and the digest to the admins. It decides **how a message looks and how a send is retried**; it does not decide who is told or when ([[scheduling]]) and it knows nothing about the database.

## Rules this file follows
- **No database access, no import from `main`, no work at import time.** The token is read inside `get_token()`, never at module level, so importing the module is harmless (tests, the bot).
- **Outcomes go out through a `delivery` object,** one acknowledgement per outcome, right after that outcome (not at the end), so a crash mid-run loses at most the message in flight. In production it is [[main]]'s `QueueDelivery`; tests pass a recorder.
- **`InvalidToken` is never swallowed.** It is a run-level problem: every later send would fail too, so it propagates to `run()`. Rows stay `sending` and the next run's maintenance recovers them.
- **`send_one` only classifies.** It makes one attempt, never sleeps and never retries. All retry policy lives in `send_with_flood_wait` and `_Run`.
- **Logs and summaries are counts only.** The repository is public, and so are its Actions logs.

## `send_all(batches, delivery, rules=None, now=None)` → summary
```
batches  {telegram_id: [claimed row, ...]}      (main.group_claimed)
row      {"notification_id", "novel_name", "display_title", "web_domain_name", "chapter",
          "chapter_url", "access_state", "unlock_at", ...}
delivery ack_sent(ids) · release(ids, error_text) · mark_dead(ids, error_text) · blocked(chat_id, ids)
rules    site_parse_rules.json (only for each site's display_name)
now      for the "free in ~5h" badge (main passes the run's clock; defaults to the current time)
summary  {"sent", "blocked", "bad_request", "failed", "retried"}   all integers
```
`sent` counts **rows** delivered, `blocked` counts **users** who blocked the bot, `bad_request` rows given up on, `failed` rows released after the retries ran out, `retried` rows delivered on a second or later attempt. `rules` and `now` are two optional arguments the original design did not list: the messenger cannot read the rules file itself (no I/O), and the badge must use the same clock as the rest of the run.

Raises `RuntimeError` when no token is configured and `InvalidToken` when Telegram rejects it; with nothing to send it returns a zero summary without creating a bot.

### Flow
```
send_all → get_token → asyncio.run(_send_all_async)
   Bot + _preflight (get_me: a bad token fails before any send)
   per user: build_message → per chunk: send → settle
       sent        → delivery.ack_sent(ids)
       blocked     → delivery.blocked(chat_id, every unsettled id of that user); stop that user
       bad_request → one message per row (HTML) → plain text → delivery.mark_dead
       transient   → retry rounds → delivery.release(ids, category)
   retry rounds, then return the summary
```

## `build_message(rows, rules, now)` → `[{"text", "row_ids"}]` (pure)
One user's rows become one or more messages:
```
<b>📚 New chapters</b>

<b>Asura Scans</b>
• <a href="https://asurascans.com/…/chapter/156">Killer Pietro</a> — Ch. 156 🔒 free in ~5h

<b>Hive Toons</b>
• <a href="…">Lookism</a> — Ch. 631
```
- Rows are grouped by site (sites sorted by display name, then by host); novels sort by title (case-insensitive) then chapter. The site name comes from `rules[domain]["display_name"]`, else the host, else "Unknown site".
- The title is the scraped title, else a readable guess from the slug ([[novel_links]].`title_for`); more than 200 characters is cut with an ellipsis. Chapter numbers show as `631`, `12.5`, or `?`.
- The badge comes from [[scheduling]].`badge_for(row, now)`, so it is computed at the moment of sending and is never stale.
- **Escaping.** Titles and site names escape `& < >` only (no numeric entities like `&#x27;` for every apostrophe); the URL in `href` also escapes quotes, so it cannot break out of the attribute. Only `http` and `https` URLs become links (`javascript:`, `data:`, relative and empty ones are shown as plain text), and a URL over 1000 characters is not linked.
- **Splitting.** Telegram's limit is 4096 characters; the budget here is 4000, minus room for the longest numbered header. Chunks split only on whole-line boundaries so a tag is never cut, repeat the site heading when a site's list continues, always hold at least one line, and are numbered `(1/3)` only when there is more than one. `row_ids` say which queue rows each chunk carries.

## Sending
- **`send_one(bot, chat_id, text, parse_mode=HTML, disable_notification=False)`** makes one attempt and returns `(status, detail)`: `sent`, `blocked` (Forbidden: the user blocked the bot or deleted the chat), `bad_request`, `rate_limited` (detail = seconds), `network`, `unknown`. **The order of the `except` clauses matters:** in python-telegram-bot, `BadRequest` and `TimedOut` are subclasses of `NetworkError`, so the specific ones must come first or a malformed message would be retried as if the connection were flaky. Link previews are always disabled.
- **`send_with_flood_wait(...)`** waits out a rate-limit answer inline (at most 2 extra attempts, never longer than 60 seconds), because Telegram's flood control is bot-wide and everything else should pause too. If the limit is still on afterwards, `rate_limited` is returned and the job is deferred.
- **Pacing.** At least 0.05 seconds between **any** two sends (about 20 a second, under Telegram's roughly 30 a second bot-wide limit), plus a 1-second pause between chunks to the same user (about one message a second per chat). This replaces the old "pause every 25 messages" rule.
- **Chunk 1 notifies, chunks 2 and onward are silent** (`disable_notification`).

### Outcomes, per message
| Outcome | What happens |
|---|---|
| `sent` | `delivery.ack_sent(row_ids)` at once |
| `blocked` | `delivery.blocked(chat_id, ids)` with **every** row of that user not yet settled (this chunk, later chunks, queued retries); the rest of that user's chunks are skipped and their queued retries dropped |
| `bad_request` | three steps: **(a)** resend the chunk as **one message per row** (still escaped HTML), acknowledging each row on its own; **(b)** a row still refused is sent as **plain text** (no markup, URL on its own line); **(c)** refused even then → `delivery.mark_dead([id], "bad_request")`. A single-row chunk skips (a). Telegram's error text is never stored or returned |
| `network`, `rate_limited` (still active), `unknown` | queued for the **retry rounds**: up to 3 rounds, 5 seconds apart. Still failing afterwards → `delivery.release(ids, category)`, and the queue gives the rows back for the next run (or marks them dead at the 24-attempt cap or after 24 hours). The retry rounds are *inside* one claim and do not count as attempts: the queue counts one attempt per claim, and [[main]]'s dispatch loop claims a failing row only once per run |

`unknown` (any other Telegram error) is retried, as the spec says; before the queue existed it was final. A blocked user mid-fallback stops the remaining rows of that chunk.

## `send_admin(chat_ids, text)` → `{"delivered", "failed"}`
Sends one HTML text to each admin chat (alerts and the daily digest, see [[admin]]) with the same classification and retry rounds, but no queue and no acknowledgements. A refused message gets one more try as plain text (tags stripped). Returns counts of admins reached and not reached; [[main]] acts only on `delivered > 0`. Raises like `send_all` for a missing or rejected token.

## Testing
- `tests/test_telegram_messenger.py` drives the real code with a scripted `FakeBot` (per-chat outcomes: success or an exception) and a recording `FakeDelivery`; `asyncio.sleep` is replaced so nothing waits and the spacing clock is frozen. Async code is driven with `asyncio.run`, so no pytest plugin is needed.
- The tests pin things that are easy to break unnoticed: acknowledgement **timing** (send, ack, send, ack), which ids `blocked` receives, the three-step fallback including the exact plain text, the retried count, and that nothing in the module imports the database.
- `tests/test_pipeline_flow.py` runs the real messenger through `main.py` and `QueueDelivery` with a fake Telegram and fake queue.
- `tests/test_smokeTest.py` (manual, `RUN_SMOKE_TESTS=1`) sends real messages to your own chat: the layout with badges and awkward characters, a long numbered message, and a sample admin alert and digest.

## Not done on purpose
- No MarkdownV2 (its escaping rules are easy to get wrong); HTML is simpler and well tested.
- No per-message "edit" or deletion of old alerts.
- Telegram's 4096 limit is counted on the raw HTML here with a safety margin, not on the parsed text.

## Related notes
[[main]] · [[scheduling]] · [[admin]] · [[database]] · [[bot]] · [[pytest]]
