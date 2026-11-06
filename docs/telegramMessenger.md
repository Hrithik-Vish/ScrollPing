# telegramMessenger.py

## Purpose
Sends each subscriber **one compiled message** listing the novels that updated this run, through the ScrollPing bot (`@ScrollPingBot`). It only *sends*: it does not read the DB and does not listen for commands (that is the separate always-on [[bot]] process, which reuses `get_token()` from this module). Called from [[main|run()]] after the DB update.

**Public entry point:** `send_all(user_tele_dict) -> summary`

## Data shapes
- **Input:** `{telegram_id: [row, ...]}` from `group_subscribers`. A row is `{"novel_name", "latest_chap", "chapter_url", ...}`; `chapter_url` must already be absolute ([[main|compilation_fnct]] builds it with `base_url`).
- **Send result** (one attempt): `(status, detail)`; status is one of `sent`, `blocked`, `bad_request`, `rate_limited` (detail = seconds to wait), `network`, `unknown`.
- **Return value (summary):** `{"sent": int, "blocked": [chat_id, ...], "bad_request": [(chat_id, detail), ...], "failed": [(chat_id, category, detail), ...]}`.

## What the message looks like
Bold header `New chapters`, then one line per novel: a tappable title plus the chapter, e.g. `the-hero's-ballad — Ch. 12.5` (whole numbers print without `.0`). HTML parse mode, link previews off. Titles are the **slugs** for V1 (a display title is V2). If a user's list is too long it is split on whole-line boundaries into chunks headed `New chapters (1/N)` … `(N/N)`.

## Functions (in dependency order)
- **`get_token()`** — `load_dotenv()` (does not override real environment variables), reads `TELEGRAM_BOT_TOKEN`, raises `RuntimeError` if missing or blank. Nothing is read at import time.
- **`build_message(rows) -> list[str]`** (pure) — escapes names with `html.escape` and URLs with `quote=True`, truncates names over `MAX_NAME_LEN`, chunks under `MAX_MESSAGE_LEN`, numbers chunks only when there is more than one. `[]` for no rows.
- **`send_one(bot, chat_id, text)`** — one attempt; classifies, never sleeps or retries. Re-raises `InvalidToken`.
- **`send_with_flood_wait(...)`** — on `rate_limited`, sleeps the requested time and retries inline (up to `MAX_FLOOD_RETRIES`, never sleeping longer than `MAX_FLOOD_WAIT`).
- **`new_summary()` / `record_outcome(...)`** — pure bookkeeping that routes each result to the summary or the retry queue.
- **`retry_failed(bot, retry_queue, summary)`** — bounded rounds over the queue, then leftovers move to `summary["failed"]`.
- **`_preflight(bot)`** — `get_me()` so a bad token fails before any send; other errors only print a warning.
- **`_send_all_async(user_tele_dict, token)`** — one `Bot`, one event loop: preflight → per user, per chunk → retry queue → summary.
- **`send_all(user_tele_dict)`** — sync wrapper: `get_token()` first, then `asyncio.run(...)`.

## Error handling
| Exception | Status | What happens |
|---|---|---|
| `InvalidToken` | — | re-raised; aborts the run; [[main|run()]] prints a fixed message, still flips the log, exits 1 |
| `RetryAfter` | `rate_limited` | waited out inline; if still limited, queued |
| `Forbidden` | `blocked` | counted, never retried, that user's remaining chunks are skipped |
| `BadRequest` | `bad_request` | counted, never retried (chat not found, bad HTML, too long) |
| `TimedOut` / `NetworkError` | `network` | queued, retried in later rounds |
| other `TelegramError` | `unknown` | reported in `failed`, not retried |

**Clause order matters.** In python-telegram-bot 22.x `BadRequest` and `TimedOut` are subclasses of `NetworkError`; `BadRequest` must be caught first or a malformed message would be retried forever. A canary test asserts the subclass relationship so a library change is noticed.

## Constants (top of the file; tests patch them)
`MAX_MESSAGE_LEN = 4000` (Telegram's limit is 4096), `MAX_NAME_LEN = 200`, `BATCH_SIZE = 25` / `BATCH_PAUSE = 5` (pause after every 25 sends), `CHUNK_PAUSE = 1` (about one message per second to the same chat), `MAX_FLOOD_RETRIES = 2`, `MAX_FLOOD_WAIT = 60`, `MAX_RETRY_ROUNDS = 3`, `ROUND_DELAY = 5`.

## Design decisions
- **HTML parse mode, not MarkdownV2**, because titles are full of characters MarkdownV2 would need escaped.
- **One message per user**, not per novel — fewer messages, friendlier to rate limits.
- **`InvalidToken` is a run-level error**, so it is never recorded as a per-message status.
- **Rate limits are bot-wide**, so they are handled inline; the deferred queue is for network failures.
- **No idempotency key exists in Telegram.** A timeout can mean the message arrived, so a retry can occasionally duplicate. For alerts, a duplicate beats a silent miss.
- **Failed sends are lost across runs** once the novels table is updated (V2: notified flag/table). Blocked users are only counted (V2: mark the subscription inactive).
- **Logs print counts only** — the repo and its Actions logs are public.
- **The token is read inside `get_token()`**, not at import, so tests and tools can import the module freely.

## Tests ([[pytest]])
- `tests/test_telegram_messenger.py` — `build_message` (escaping, chunking, numbering, order), `send_one` classification for every exception, flood wait, retry rounds, `send_all` flows, all with a `FakeBot` and a patched `asyncio.sleep`.
- `tests/test_run.py` — how `run()` wires the send in (order, exit codes, no token in output).
- `tests/test_smokeTest.py` — manual, sends **real** messages to `TELEGRAM_CHAT_ID`; skipped unless `RUN_SMOKE_TESTS=1`.

## Related notes
[[main]] · [[database]] · [[pytest]]
