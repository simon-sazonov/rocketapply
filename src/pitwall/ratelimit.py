"""Shared token-bucket rate limiter — §7.1 of PITWALL_PLAN.md.

Defaults: `ddgs` -> 0.25 req/s (one every 4 s), `serper` -> 5 req/s.

**Do not parallelize the free provider.** Threads get you rate-limited within a
minute; serial + cached is faster in wall-clock terms than parallel + blocked.
"""

from __future__ import annotations

import random
import threading
import time


class RateLimiter:
    """Process-wide minimum interval between calls, with jitter."""

    def __init__(self, per_second: float, jitter: float = 0.4) -> None:
        if per_second <= 0:
            raise ValueError("per_second must be > 0")
        self.per_second = per_second
        self.min_interval = 1.0 / per_second
        self.jitter = max(0.0, jitter)
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self) -> float:
        """Block until the next call is allowed. Returns the seconds slept."""
        with self._lock:
            now = time.monotonic()
            wait = self.min_interval - (now - self._last)
            slept = 0.0
            if wait > 0:
                slept = wait + random.uniform(0, self.jitter)
                time.sleep(slept)
            self._last = time.monotonic()
            return slept

    def __enter__(self) -> RateLimiter:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        return None


_limiters: dict[str, RateLimiter] = {}
_registry_lock = threading.Lock()


def get_limiter(name: str, per_second: float, jitter: float = 0.4) -> RateLimiter:
    """One limiter per provider name, shared across the process."""
    with _registry_lock:
        limiter = _limiters.get(name)
        if limiter is None or abs(limiter.per_second - per_second) > 1e-9:
            limiter = RateLimiter(per_second, jitter)
            _limiters[name] = limiter
        return limiter


def reset_limiters() -> None:
    """Test helper — drop every registered limiter."""
    with _registry_lock:
        _limiters.clear()
