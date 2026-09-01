"""Shared token-bucket rate limiter and adaptive concurrency — §7.1, §1.1.

Defaults: `ddgs` -> 0.25 req/s (one every 4 s), `serper` -> 5 req/s,
`searxng` -> 1 req/s.

**The limiter is process-wide, and that is the point.** Phase 1 puts a
`ThreadPoolExecutor` in front of the search and LLM stages; a per-thread limiter
would not be a rate limit at all, it would be N of them. `get_limiter` returns
one object per provider name and `acquire()` holds the lock across the sleep, so
four workers on a 1 q/s bucket issue one query per second between them, not four.

`AdaptiveConcurrency` is the other half: a 429 means the ceiling was wrong, so
the run lowers it for good rather than hammering the same wall at the same rate.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

log = logging.getLogger(__name__)


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


class AdaptiveConcurrency:
    """A worker-slot semaphore whose ceiling can only go down.

    §1.1: "On HTTP 429, back off exponentially and reduce the effective
    concurrency for the rest of the run rather than hammering and failing."

    Shrinking is one-way and permanent for the life of the object. A limit that
    recovered would just walk back into the same 429, and the run is short
    enough that the lost throughput costs less than the retries would.
    """

    def __init__(self, limit: int, *, name: str = "workers", floor: int = 1) -> None:
        self.name = name
        self.floor = max(1, int(floor))
        self.limit = max(self.floor, int(limit))
        self.initial = self.limit
        self.reductions = 0
        self._cond = threading.Condition()
        self._in_flight = 0

    @contextmanager
    def slot(self) -> Iterator[None]:
        with self._cond:
            while self._in_flight >= self.limit:
                self._cond.wait()
            self._in_flight += 1
        try:
            yield
        finally:
            with self._cond:
                self._in_flight -= 1
                self._cond.notify()

    def reduce(self, reason: str = "429") -> int:
        """Halve the ceiling, never below `floor`. Returns the new limit."""
        with self._cond:
            if self.limit > self.floor:
                self.limit = max(self.floor, self.limit // 2)
                self.reductions += 1
                log.warning(
                    "%s: reducing concurrency to %d for the rest of the run (%s)",
                    self.name, self.limit, reason,
                )
                self._cond.notify_all()
            return self.limit

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AdaptiveConcurrency {self.name} {self.limit}/{self.initial}>"
