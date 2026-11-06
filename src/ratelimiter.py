"""ratelimiter.py - per-user token bucket for incoming Telegram messages.

Each user owns a bucket that holds up to `capacity` tokens and refills at
`refill_per_second`. Every message costs one token. A user who empties the bucket
is dropped until it refills, so a burst of 5 is fine but a flood is not.

check(key) returns:
    "allow"   message is fine, one token spent
    "warn"    over the limit: drop it AND tell the user once ("slow down")
    "drop"    over the limit and already warned recently: drop it silently

The warning is itself rate limited (warn_cooldown seconds), otherwise replying to every
dropped message would let a flooder make the bot send a flood back.

Pure logic: the clock is injectable so tests never sleep, and nothing here touches Telegram.
"""

import time


class TokenBucketLimiter:
    def __init__(self, capacity=5, refill_per_second=0.5, warn_cooldown=10.0,
                 clock=time.monotonic, max_tracked=10000):
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.warn_cooldown = warn_cooldown
        self.clock = clock
        self.max_tracked = max_tracked
        self._state = {}  # key -> {"tokens", "stamp", "warned_at"}

    def check(self, key):
        now = self.clock()

        if len(self._state) >= self.max_tracked and key not in self._state:
            self._purge(now)

        state = self._state.get(key)
        if state is None:
            state = {"tokens": float(self.capacity), "stamp": now, "warned_at": None}
            self._state[key] = state
        else:
            elapsed = max(0.0, now - state["stamp"])
            state["tokens"] = min(self.capacity, state["tokens"] + elapsed * self.refill_per_second)
            state["stamp"] = now

        if state["tokens"] >= 1:
            state["tokens"] -= 1
            return "allow"

        warned_at = state["warned_at"]
        if warned_at is None or now - warned_at >= self.warn_cooldown:
            state["warned_at"] = now
            return "warn"
        return "drop"

    def _purge(self, now):
        """Forget users whose bucket would be full again (they are indistinguishable from new ones)."""
        for key in list(self._state):
            state = self._state[key]
            refilled = state["tokens"] + max(0.0, now - state["stamp"]) * self.refill_per_second
            if refilled >= self.capacity:
                del self._state[key]
