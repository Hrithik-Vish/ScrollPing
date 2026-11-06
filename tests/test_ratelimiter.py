"""TokenBucketLimiter with a fake clock, so nothing here sleeps."""

import pytest

from src.ratelimiter import TokenBucketLimiter


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


def make(clock, **kwargs):
    defaults = dict(capacity=3, refill_per_second=1.0, warn_cooldown=10.0, clock=clock)
    defaults.update(kwargs)
    return TokenBucketLimiter(**defaults)


def test_burst_up_to_capacity_is_allowed(clock):
    limiter = make(clock)
    assert [limiter.check("u") for _ in range(3)] == ["allow"] * 3


def test_over_capacity_warns_once_then_drops_silently(clock):
    limiter = make(clock)
    for _ in range(3):
        limiter.check("u")
    assert limiter.check("u") == "warn"
    assert limiter.check("u") == "drop"
    assert limiter.check("u") == "drop"


def test_warning_comes_back_after_the_cooldown(clock):
    limiter = make(clock, refill_per_second=0.0)   # never refills, isolates the cooldown
    for _ in range(3):
        limiter.check("u")
    assert limiter.check("u") == "warn"
    clock.advance(9)
    assert limiter.check("u") == "drop"
    clock.advance(1)
    assert limiter.check("u") == "warn"


def test_tokens_refill_over_time(clock):
    limiter = make(clock)
    for _ in range(3):
        limiter.check("u")
    assert limiter.check("u") != "allow"
    clock.advance(2)
    assert limiter.check("u") == "allow"
    assert limiter.check("u") == "allow"
    assert limiter.check("u") != "allow"


def test_refill_never_exceeds_capacity(clock):
    limiter = make(clock)
    limiter.check("u")
    clock.advance(10_000)
    assert [limiter.check("u") for _ in range(3)] == ["allow"] * 3
    assert limiter.check("u") != "allow"


def test_users_have_separate_buckets(clock):
    limiter = make(clock)
    for _ in range(3):
        limiter.check("a")
    assert limiter.check("a") != "allow"
    assert limiter.check("b") == "allow"


def test_fractional_refill_rate(clock):
    limiter = make(clock, capacity=1, refill_per_second=0.5)
    assert limiter.check("u") == "allow"
    clock.advance(1)            # only half a token
    assert limiter.check("u") != "allow"
    clock.advance(1)            # now a full token
    assert limiter.check("u") == "allow"


def test_idle_users_are_purged_when_the_table_is_full(clock):
    limiter = make(clock, max_tracked=3)
    for name in ("a", "b", "c"):
        limiter.check(name)
    clock.advance(100)          # everyone is back to a full bucket
    limiter.check("d")
    assert set(limiter._state) == {"d"}


def test_purge_keeps_users_who_are_still_limited(clock):
    limiter = make(clock, max_tracked=2, refill_per_second=0.0)
    for _ in range(3):
        limiter.check("flooder")
    limiter.check("quiet")
    limiter.check("new")        # table full: purge runs, but nobody is "full" with refill 0
    assert "flooder" in limiter._state
