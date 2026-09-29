"""Several API keys for one provider, and which of them has room right now.

Groq's free tier caps a key at 8,000 tokens a minute and 200,000 a day. The
letter prompt is about 6,000 tokens, so one key writes roughly one letter a
minute and runs out after about twenty. Three keys is three times both numbers,
and it is the difference between a hundred letters taking a week and taking a
night.

The interesting part is not holding three keys, it is **choosing**. Round-robin
would send a call to a key that has 200 tokens left while another has 90,000,
and the daily ceiling is the one that actually stops a batch. So each key carries
its own usage and the pool hands out whichever has the most headroom.

Two kinds of refusal, and they need opposite treatment:

* **a minute limit** — park the key for the seconds Groq asks for and use another
  one meanwhile. Before this existed the router simply slept, so three keys would
  have waited in turn instead of working.
* **a day limit** — park the key for as long as the window needs. The router used
  to treat this as the whole provider being dead, which with one key was true and
  with three is wrong.

Usage is written to `run/key_usage.json` so it survives a restart and so parallel
shards see roughly the same picture. Roughly is enough: a stale count can only
lead to a worse *choice*, never to a wrong answer, because the provider itself
remains the authority on whether a call is allowed.

**A key is never logged or written out.** Only a fingerprint — the last four
characters — appears anywhere, which is enough to tell three keys apart in a
report and useless to anyone who reads it.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

__all__ = ["KeyState", "KeyPool", "USAGE_PATH", "fingerprint", "shared_pool", "reset_pools"]

USAGE_PATH = pathlib.Path("run") / "key_usage.json"

#: Groq free tier, per key. Overridable because a paid tier or another provider
#: has different numbers, and because the tests need small ones.
DAILY_TOKENS = 200_000
MINUTE_TOKENS = 8_000

#: How long a key is parked when the provider reports a daily ceiling and gives
#: no time. The window is rolling, so a short park lets it be retried as room
#: frees up rather than writing the key off for the rest of the day.
DAY_PARK_SECONDS = 900.0


def fingerprint(key: str) -> str:
    """`...a1b2` — enough to tell keys apart, useless to a reader."""
    cleaned = (key or "").strip()
    return f"...{cleaned[-4:]}" if len(cleaned) >= 4 else "...????"


@dataclass
class KeyState:
    """One key's recent history. `spend` is (unix seconds, tokens) pairs."""

    fingerprint: str
    spend: list[tuple[float, int]] = field(default_factory=list)
    #: Unix time this key may be used again. 0 means now.
    parked_until: float = 0.0
    parked_reason: str = ""

    def prune(self, now: float) -> None:
        """Drop anything outside the daily window; it no longer counts."""
        self.spend = [(when, n) for when, n in self.spend if now - when < 86_400]

    def used_in(self, now: float, seconds: float) -> int:
        return sum(n for when, n in self.spend if now - when < seconds)

    def day_used(self, now: float) -> int:
        return self.used_in(now, 86_400)

    def minute_used(self, now: float) -> int:
        return self.used_in(now, 60)

    def headroom(self, now: float, *, daily: int, minute: int) -> int:
        """Tokens this key could spend right now, the tighter of the two limits."""
        if now < self.parked_until:
            return 0
        return max(0, min(daily - self.day_used(now), minute - self.minute_used(now)))


class KeyPool:
    """The keys for one provider, and which to use next.

    Thread-safe, because the Studio runs a batch on a worker thread and a single
    process using all three keys is the whole point of the Streamlit path — it
    means Simon clicks one button rather than launching three shells.
    """

    def __init__(
        self,
        keys: list[str],
        *,
        daily: int = DAILY_TOKENS,
        minute: int = MINUTE_TOKENS,
        usage_path: pathlib.Path | None = USAGE_PATH,
        provider: str = "groq",
    ) -> None:
        # Order preserved, blanks and duplicates dropped: a .env with the same
        # key pasted twice should not look like twice the quota.
        seen: list[str] = []
        for key in keys:
            cleaned = (key or "").strip()
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
        self.keys = seen
        self.daily, self.minute = daily, minute
        self.provider = provider
        self.usage_path = usage_path
        self._lock = threading.Lock()
        self._state: dict[str, KeyState] = {
            key: KeyState(fingerprint=fingerprint(key)) for key in self.keys
        }
        self._load()

    def __len__(self) -> int:
        return len(self.keys)

    # ── choosing ──

    def choose(self, need: int = 0) -> str | None:
        """The key with the most room, or None when every key is parked.

        `need` is what the call is expected to cost. A key with less headroom
        than that is skipped rather than used and immediately refused, which is
        what turns a 429 into a free key switch instead of a wasted round trip.
        """
        with self._lock:
            now = time.time()
            ranked = sorted(
                self.keys,
                key=lambda k: self._state[k].headroom(now, daily=self.daily, minute=self.minute),
                reverse=True,
            )
            for key in ranked:
                room = self._state[key].headroom(now, daily=self.daily, minute=self.minute)
                if room > 0 and (need <= 0 or room >= need):
                    return key
            # Nothing has room for `need`. Offer the roomiest unparked key
            # anyway: our accounting may be behind the provider's, and being
            # told no by Groq is cheaper than refusing to try.
            for key in ranked:
                if now >= self._state[key].parked_until:
                    return key
            return None

    def wait_seconds(self) -> float:
        """How long until the earliest key frees, for when all are parked."""
        with self._lock:
            now = time.time()
            waits = [s.parked_until - now for s in self._state.values() if s.parked_until > now]
            return max(0.0, min(waits)) if waits else 0.0

    # ── recording ──

    def record(self, key: str, tokens: int) -> None:
        with self._lock:
            state = self._state.get(key)
            if state is None:
                return
            now = time.time()
            state.spend.append((now, max(0, int(tokens))))
            state.prune(now)
        self._save()

    def park(self, key: str, seconds: float, reason: str = "") -> None:
        """Take a key out of rotation for a while."""
        with self._lock:
            state = self._state.get(key)
            if state is None:
                return
            state.parked_until = max(state.parked_until, time.time() + max(0.0, seconds))
            state.parked_reason = reason
        log.info("%s key %s parked %.0fs (%s)", self.provider,
                 fingerprint(key), seconds, reason or "no reason given")
        self._save()

    def note_refusal(self, key: str, kind: str, retry_after: float) -> None:
        """Park a key according to which ceiling it hit.

        `kind` is `classify_error`'s verdict. A minute limit is a short park; a
        day limit is a long one, but not the rest of the day — the window rolls,
        so the key becomes usable again gradually.
        """
        if kind == "quota":
            self.park(key, max(retry_after, DAY_PARK_SECONDS), "daily ceiling")
        else:
            self.park(key, max(retry_after, 1.0), "minute ceiling")

    # ── reporting ──

    def status(self) -> list[dict]:
        """One row per key, for the batch report and the Studio. No secrets."""
        with self._lock:
            now = time.time()
            out = []
            for key in self.keys:
                state = self._state[key]
                out.append({
                    "key": state.fingerprint,
                    "day_used": state.day_used(now),
                    "day_limit": self.daily,
                    "minute_used": state.minute_used(now),
                    "headroom": state.headroom(now, daily=self.daily, minute=self.minute),
                    "parked_for": max(0.0, round(state.parked_until - now, 1)),
                    "reason": state.parked_reason if state.parked_until > now else "",
                })
            return out

    # ── persistence ──

    def _load(self) -> None:
        if not self.usage_path or not self.usage_path.exists():
            return
        try:
            saved = json.loads(self.usage_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        now = time.time()
        # Keyed by fingerprint, because the key itself is never written down.
        by_print = {state.fingerprint: state for state in self._state.values()}
        for row in saved.get(self.provider, []):
            state = by_print.get(str(row.get("key", "")))
            if state is None:
                continue
            state.spend = [
                (float(w), int(n))
                for w, n in row.get("spend", [])
                if isinstance(w, (int, float)) and now - float(w) < 86_400
            ]
            state.parked_until = float(row.get("parked_until") or 0.0)
            state.parked_reason = str(row.get("parked_reason") or "")

    def _save(self) -> None:
        if not self.usage_path:
            return
        payload = {}
        if self.usage_path.exists():
            try:
                payload = json.loads(self.usage_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
        with self._lock:
            payload[self.provider] = [
                {
                    "key": state.fingerprint,
                    "spend": [[round(w, 1), n] for w, n in state.spend],
                    "parked_until": round(state.parked_until, 1),
                    "parked_reason": state.parked_reason,
                }
                for state in self._state.values()
            ]
        try:
            self.usage_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.usage_path.with_name(f"{self.usage_path.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.usage_path)
        except OSError as exc:      # never fail a run over bookkeeping
            log.info("could not write key usage: %s", exc)


# ── one pool per provider, per process ──
#
# `generate_application` builds its own router, so a batch running eight jobs at
# once would otherwise have eight routers, eight pools and eight independent
# ideas of how much quota is left — each one happily spending the same key up to
# its full ceiling. The pool has to be shared for the accounting to mean
# anything, and it is thread-safe precisely so it can be.

_POOLS: dict[str, KeyPool] = {}
_POOLS_LOCK = threading.Lock()


def shared_pool(provider: str, keys: list[str], **options) -> KeyPool | None:
    """The one pool for this provider in this process, or None for a single key.

    A single key needs no pool: there is nothing to choose between, and the
    provider's own limits are handled the way they always were.
    """
    if len(keys) <= 1:
        return None
    with _POOLS_LOCK:
        pool = _POOLS.get(provider)
        # Rebuilt if the configured keys changed, which happens in tests and
        # when a key is added to .env and the page is reloaded.
        if pool is None or pool.keys != [k.strip() for k in keys if k and k.strip()]:
            pool = KeyPool(keys, provider=provider, **options)
            _POOLS[provider] = pool
        return pool


def reset_pools() -> None:
    """Forget every pool. For tests, and for picking up an edited .env."""
    with _POOLS_LOCK:
        _POOLS.clear()
