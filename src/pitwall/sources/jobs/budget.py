"""How many requests a quota-limited source may make this run.

A free API key usually carries more than one limit. Adzuna's are 250 a day,
1,000 a week and 2,500 a month, and the tightest one decides. Each window is
counted as a ROLLING window over the `quota` table (today plus the previous
N-1 days), because a provider rarely says which day its week starts on. A
rolling count cannot be over the limit whichever start day the provider uses.

Pacing is a separate question from the limits:

* **burst** spends up to the limits every run. The weekly limit means at most
  1,000 in the first four days, and then the source pauses until the window
  rolls.
* **even** spends about `per_month / 31` a day, which is the rate the monthly
  limit can sustain indefinitely.
* **auto** bursts while a backfill is pending (some search has never been read
  back its full window), then runs evenly. Everything that is open today is
  found in the first days, and new postings are found daily after that.

Pure functions: the caller reads the ledger on the thread that owns the
database connection and passes the numbers in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Pace = Literal["auto", "burst", "even"]


@dataclass(frozen=True)
class Usage:
    day: int = 0
    week: int = 0
    month: int = 0


@dataclass(frozen=True)
class SourceBudget:
    per_day: int | None = None
    per_week: int | None = None
    per_month: int | None = None

    def remaining(self, used: Usage) -> int | None:
        """What the tightest limit still allows. None = no limit at all."""
        left = [
            limit - spent
            for limit, spent in ((self.per_day, used.day), (self.per_week, used.week),
                                 (self.per_month, used.month))
            if limit is not None
        ]
        return max(0, min(left)) if left else None

    def steady_per_day(self) -> int | None:
        """The daily rate every limit can sustain for ever."""
        rates = [
            limit // days
            for limit, days in ((self.per_day, 1), (self.per_week, 7), (self.per_month, 31))
            if limit is not None
        ]
        return min(rates) if rates else None


def allowance(budget: SourceBudget, used: Usage, *, pace: Pace = "auto",
              backfill_pending: bool = False) -> int | None:
    """Requests this run may make. None = unlimited (a source with no budget)."""
    hard = budget.remaining(used)
    if pace == "burst" or (pace == "auto" and backfill_pending):
        return hard
    steady = budget.steady_per_day()
    if steady is None:
        return hard
    today = max(0, steady - used.day)
    return today if hard is None else min(hard, today)


def usage_of(quota: object, provider: str) -> Usage:
    """Read the three windows from a `QuotaRepo`. Main thread only."""
    used_since = quota.used_since  # type: ignore[attr-defined]
    return Usage(day=used_since(provider, 1), week=used_since(provider, 7),
                 month=used_since(provider, 31))
