# ratelimiter.py — per-user token bucket

**Status:** done, covered by `tests/test_ratelimiter.py` (9 tests) plus the gate tests in `tests/test_bot.py`.

## Purpose
Stops one person from flooding [[bot]]. A bot is public: anyone who finds it can send it messages as fast as they like, and each message can cost a database query.

## How it works
`TokenBucketLimiter(capacity=5, refill_per_second=0.5, warn_cooldown=10, clock=time.monotonic, max_tracked=10000)`. Each user has a bucket holding up to `capacity` tokens that refills continuously; every message costs one token.

`check(key)` returns one of:
- `"allow"`: spend a token, handle the message;
- `"warn"`: bucket empty, drop the message **and** tell the user once ("slow down");
- `"drop"`: bucket empty and the user was already warned within `warn_cooldown` seconds, drop silently.

The warning is itself throttled. Otherwise answering every dropped message would let a flooder make the bot send a flood back.

With the bot's settings (5, 0.5/s) a burst of 5 commands is fine, then roughly one per 2 seconds.

## Details
- **Injectable clock**, so tests advance time instead of sleeping.
- **Memory is bounded.** When more than `max_tracked` users are tracked, users whose bucket would be full again are forgotten (they are indistinguishable from new ones); users who are still limited are kept.
- State is in memory: it resets when the bot restarts, which is fine for one process.
- Not to be confused with python-telegram-bot's `AIORateLimiter`, which throttles *outgoing* messages. This module limits *incoming* ones.

## Where it runs
`bot.rate_gate` (handler group -1) calls `check(update.effective_user.id)` before every other handler, and raises `ApplicationHandlerStop` for anything not allowed, so over-limit updates never reach a handler or the database.

## Related notes
[[bot]] · [[pytest]]
