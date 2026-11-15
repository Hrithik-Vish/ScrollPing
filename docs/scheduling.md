# scheduling.py

## Purpose
The pure decisions about **who is notified, when, and with what badge** (no network, no database, and it never reads the clock: `now` is always passed in). [[main]] feeds it and acts on its answers; [[telegramMessenger]] uses `badge_for` at the moment of sending.

## Vocabulary
- `access_state`: `free`, `locked` or `unknown` (what the site told us about a chapter; `unknown` when lock parsing failed).
- `unlock_at`: when a locked chapter becomes free, only when the site says (asurascans).
- `tier`: `standard` or `member`, the user's account type on a site ([[bot]] `/settings`).
- `alert_timing`: `early`, `when_free` or none, a per-novel override.

## `resolve_due_at(access_state, unlock_at, alert_timing, tier, now)` → aware UTC datetime
| Situation | Due |
|---|---|
| not locked (free, unknown, anything else) | now |
| locked, unlock time unknown or already past | now (the badge says "early access") |
| locked, unlock in the future, timing `early` | now |
| locked, unlock in the future, timing `when_free` | the unlock time |
| locked, unlock in the future, **no override** | members: now; everyone else: the unlock time |

An unrecognised timing value is treated as no override. Times may be datetimes or ISO text (a trailing `Z` is fine); naive datetimes are taken as UTC.

## `build_queue_records(rows, recipients, tiers, now)`
One record per (updated chapter, recipient): `user_id`, `novel_id`, `chapter` (float), `chapter_url`, `access_state`, `unlock_at`, `due_at`, `status = pending`.
- Every record has **exactly the same keys** (a PostgREST batch insert requires that).
- The recipient's tier is looked up as `tiers[(user_id, row["web_id"])]`, default standard, so a member of one site is not treated as a member on another.
- `unlock_at` is stored only for locked rows (free rows can carry an old timestamp from the site that means nothing).
- Rows without a URL or chapter are ignored; a recipient listed twice for one novel yields one record; inputs are not mutated. Order is by row, then recipient.

## `badge_for(row, now)` → text appended to a chapter line
| Row | Badge |
|---|---|
| locked, unlock in the future | ` 🔒 free in ~{remaining}` |
| locked, unlock unknown | ` 🔒 early access` |
| locked, unlock already passed | none (it is free by now) |
| free or unknown | none |

`format_remaining(seconds)`: under one hour `<1h`; under 48 hours `Nh` rounded up; otherwise `Nd` in whole days. Because the badge is computed when the message is **sent**, a row queued at noon and sent at 3 pm says "free in ~2h", not "~5h".

## Why a separate module
These rules decide what people experience, so they should be readable, unit-tested with plain data (`tests/test_scheduling.py`), and impossible to break by touching SQL. The database functions only do what must be atomic. A test mutating the member default fails six tests, which is the point.

## Related notes
[[main]] · [[telegramMessenger]] · [[admin]] · [[database]] · [[parsing]]
