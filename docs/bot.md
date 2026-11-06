# bot.py — Telegram command handler

**Status:** built and live-tested (Nov 2026): `/start`, `/add`, `/list`, `/remove`, `/status`, `/stop`, `/help`, `/cancel`, duplicate adds, pausing, deleting and re-creating an account. Covered by [[pytest]] (`tests/test_bot.py`, 110 tests, and `tests/test_health.py`, 10). Deployment to Render is set up, see [[deployment]].

## Purpose
The people-facing half of ScrollPing. [[main]] runs on a schedule and only *sends* alerts ([[telegramMessenger]]); `bot.py` is a separate, always-running process that *receives* commands so people can sign up and manage the novels they follow without anyone touching the database by hand.

Run it with `python -m src.bot` (needs `TELEGRAM_BOT_TOKEN` and the three `SUPABASE_*` variables). It uses **long polling**: the process keeps asking Telegram for new messages, so it needs no public URL. It also serves a `/health` page (`src/health.py`) when `HEALTH_PORT` (or `PORT`, which Render sets) is present, for hosts that need a listening port and for the timer that keeps a free host awake (details in [[deployment]]); with neither set, no port is opened. Only one process may poll per bot token at a time. Telegram identifies every sender with a numeric `telegram_user_id` it vouches for, so `/start` registers someone with no password or phone step.

## Commands
| Command | What it does |
|---|---|
| `/start` | New user: creates the account and shows a welcome, the command list and the supported sites. Paused user: switches notifications back on. Existing user: "welcome back". |
| `/add` | `/add <link>` adds immediately; plain `/add` asks for the link and waits for the next message (retrying on a bad link until `/cancel`). |
| `/list` | The user's novels as numbered text (name, site, latest chapter, status) with one button per novel; tapping opens a menu: change status / remove. |
| `/remove` | Same list as buttons; tap one, confirm with "Yes, remove". |
| `/status` | Same list as buttons; tap one, pick `reading`, `completed`, `left` or `on hiatus`. Only `reading` sends notifications, and the picker says so. |
| `/stop` | Menu: **Pause notifications**, **Delete my account and data** (asks a second time), or Cancel. |
| `/help` | Command list and supported sites. |
| `/cancel` | Abandons a half-finished `/add`. |

Anything else gets a short hint: plain text without `/add` ("send /add first"), non-text messages ("I only understand text"), unknown commands.

## How adding a novel works
1. The link is parsed by [[novel_links]] into a site key and a slug (no fetch, nothing is requested from the site). `nano-machine-ab39de10/chapter/1` and `nano-machine/chapter/1` both become `nano-machine`.
2. `database.subscribe_user_to_novel` creates the website and novel rows if needed and links the user. A brand-new novel starts at `latest_chap = -1`.
3. The next scraper run that sees the novel on the site's listing page fills in the real chapter and sends the first notification ([[main]]). A real but quiet novel simply waits until it next updates, and `/list` shows "waiting for first update".

Replies: added, already following, limit reached (20), or one of several specific messages for a bad link (not a link, unsupported site plus the supported list, not a novel page, unreadable slug, too long).

## Layout
- **Plain helpers** (no Telegram objects, no I/O): `process_add`, `format_subscriptions`, `chapter_text`, `sites_text`, `display_name`, `parse_uuid`, keyboard builders. This is what most tests hit.
- **Handlers** are thin: they call [[database]] and [[novel_links]]. Blocking database calls run through `asyncio.to_thread` so one slow query cannot freeze the bot for everybody.
- **`MESSAGES`** at the top holds every user-facing text, so the wording can be rewritten in one place (final wording is planned once everything works).
- **`build_application(token, rules)`** wires everything and is what the tests inspect; `main()` configures logging, starts the health server when `HEALTH_PORT` or `PORT` is set (a bad value fails fast with a message naming the variable) and calls `run_polling`.
- **Lifecycle:** `post_init` publishes the command menu (a failure there is logged, not fatal) and marks the bot as running for `/health`; `post_shutdown` clears it.

## Inline buttons
All buttons go through one router, `on_callback`. Callback data (Telegram allows 64 bytes; the longest here is 41):

| Data | Meaning |
|---|---|
| `m:<id>` | open the menu for a subscription (from `/list`) |
| `st:<id>` | show the status choices |
| `ss:<id>:<n>` | set status number `n` (index into `reading, completed, left, on hiatus`) |
| `rm:<id>` / `ry:<id>` | ask to remove / confirmed remove |
| `stop:pause`, `stop:delete`, `stop:deleteyes` | the `/stop` menu |
| `no` | cancel, "nothing changed" |

`<id>` is a subscription id. The router checks it is a valid uuid **and** present in the caller's own list before doing anything, and the database functions filter on the user id again.

## Protection
- **Private chats only.** `rate_gate` runs before every handler (handler group -1) and drops anything from groups or without a user, silently.
- **Rate limiting.** One token bucket per user ([[ratelimiter]]): a burst of 5 is fine, then one message per 2 seconds. A user over the limit gets one "slow down" reply per 10 seconds, everything else is dropped before any database work. Button presses are answered with the warning as a pop-up instead of a message.
- **Input limits.** Text over 300 characters is rejected before parsing; links are length-capped and matched against an allow-list of hosts; slugs must match `[a-z0-9-]+`.
- **Identity and ownership.** The bot uses the Supabase **secret key**, which bypasses RLS. So identity comes only from `update.effective_user.id`, and every subscription change filters on both the subscription id and the user's id. At most 20 novels per user (`MAX_SUBSCRIPTIONS`).
- **No token in logs.** `httpx` logs every request URL at INFO and a Telegram URL contains the bot token, so `configure_logging()` keeps the `httpx` and `httpcore` loggers at WARNING. (Found in the first live run; the token was rotated.)
- **Errors.** Any exception inside a handler is logged and the user sees a generic "something went wrong"; details never reach the chat.

## Decisions
- Users who block the bot are left alone: the sender keeps trying, and they resume getting updates if they come back.
- No link verification on every paste: it would mean one fetch per `/add`. The checks are cheap ones (host allow-list, slug shape, cap, rate limit); a typo'd slug stays at `-1` and expires through the 97-day cleanup, because the bot stamps `updated_at` on creation. A one-time verification fetch with a per-user limit is a V2 option.
- `/stop` offers pause *and* delete: pause keeps the subscriptions (a `users.status` column, which the scraper's subscriber query respects); delete is the privacy option.
- Typing is kept to a minimum: only `/add` takes typed input, everything else is buttons.

## Known limitations
- The 20-novel cap is enforced in code, not by a database trigger (see [[database]]).
- Chapter notifications show slugs, not titles (V2).
- Per-user state for a pending `/add` lives in memory, so a restart forgets who was mid-flow (they just send `/add` again).
- Incoming-message throttling state is in memory too, which is fine for a single process.

## Related notes
[[novel_links]] · [[ratelimiter]] · [[database]] · [[telegramMessenger]] · [[main]] · [[schema]] · [[deployment]] · [[pytest]]