"""What every keyword-searching source shares: the rotation, the ledger and
the request count.

A keyword source runs searches: (query, country) pairs. Four things about them
are the same whichever board runs them:

* **The cursor.** A visit asks only for what is newer than the previous
  complete visit, plus a day of overlap. The first visit reads back the full
  `backfill_days`. A search skipped for a day, because the budget ran out,
  loses nothing: the next visit covers the gap.
* **The order.** Searches never read come first, then the longest unvisited.
* **The ledger.** Each visit is recorded (requests, postings, whether it
  finished) and written to `source_queries` by the caller on the thread that
  owns the database. A visit that did not finish leaves its cursor where it
  was, so it is redone rather than skipped.
* **The count.** A request counts when it went to the network, which the
  client reports per thread. A cache hit is free.

Sources with a quota set `provider` to their ledger name, and their
`requests_made` is written to `quota`. Keyless sources leave it None.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass
class Visit:
    """One (query, country) search in one run."""

    query: str
    country: str
    requests: int = 0
    found: int = 0
    complete: bool = False
    total: int | None = None
    backfill: bool = False


def clean_query(text: str) -> str:
    return " ".join((text or "").lower().split())


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9+#]+", (text or "").lower())


def title_matches(title: str, query: str) -> bool:
    """Every query word is in the title, as a word or the start of one
    ("scientist" matches "scientists"; "ml" does not match "html").

    A board's own title search does not guarantee this. EURES ORs and stems
    the words. Adzuna Italy drops "ai", an Italian preposition, so "ai
    engineer" came back as every engineer in Italy: 4% of 990 titles on the
    first live run, against 99-100% in every other country.
    """
    tokens = _tokens(title)
    return all(
        any(t == w or (len(w) >= 4 and t.startswith(w)) for t in tokens)
        for w in _tokens(query)
    )


#: What a search that has never found anything relevant still earns per day
#: waited, against up to 5 for one that finds a relevant job every request.
#: At 0.2, a barren search is revisited about a fifth as often, not never.
YIELD_FLOOR = 0.2

#: Below this share of titles matching the search, a page is the board's
#: search going wrong, not a thinly-matched result, and reading on is waste.
MIN_PAGE_PRECISION = 0.2


class SearchLedger:
    """Mixin for keyword sources. Call `_init_ledger` from `__init__`."""

    complete_listing = False
    terms_govern = False
    #: The quota ledger's name for this source. None = not counted.
    provider: str | None = None

    def _init_ledger(self, *, queries: list[str], countries: list[str],
                     cursors: dict[tuple[str, str], dict[str, Any]] | None = None,
                     max_requests: int | None = None, backfill_days: int = 30,
                     now: datetime | None = None) -> None:
        self.queries = list(dict.fromkeys(clean_query(q) for q in queries if q and q.strip()))
        self.countries = list(dict.fromkeys(countries))
        self.cursors = cursors or {}
        self.max_requests = None if max_requests is None else max(0, int(max_requests))
        self.backfill_days = max(1, int(backfill_days))
        self.now = now or datetime.now(UTC)
        self.requests_made = 0
        self.visits: list[Visit] = []

    # ── cursors ──

    def _row(self, query: str, country: str) -> dict[str, Any]:
        return self.cursors.get((query, country)) or {}

    def last_read(self, query: str, country: str) -> datetime | None:
        value = self._row(query, country).get("last_fetched_at")
        if not value:
            return None
        try:
            ts = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)

    def backfilled(self, query: str, country: str) -> bool:
        return bool(self._row(query, country).get("backfilled"))

    def backfill_pending(self) -> bool:
        return any(not self.backfilled(q, c) for q in self.queries for c in self.countries)

    def days_for(self, query: str, country: str) -> int:
        """How far back this visit reads."""
        last = self.last_read(query, country)
        if last is None or not self.backfilled(query, country):
            return self.backfill_days
        gap = (self.now - last).total_seconds() / 86400
        return max(1, min(self.backfill_days, math.ceil(gap) + 1))

    def since(self, query: str, country: str) -> datetime:
        """The oldest posting date this visit asks for."""
        from datetime import timedelta

        return self.now - timedelta(days=self.days_for(query, country))

    def priority(self, query: str, country: str) -> float:
        """How much a visit to this search is worth now: days since the last
        visit, times what it has yielded (relevant jobs per request, plus a
        floor, so a quiet search is visited less often but never abandoned).

        `relevant` comes from the caller (`market.Context.cursors`), counted
        from what the filters passed. Before any history, every search is
        worth the same, and the oldest goes first.
        """
        row = self._row(query, country)
        last = self.last_read(query, country)
        days = 1e6 if last is None else max(0.0, (self.now - last).total_seconds() / 86400)
        requests = float(row.get("requests") or 0)
        relevant = float(row.get("relevant") or 0)
        rate = relevant / requests if requests else 1.0
        return days * (YIELD_FLOOR + min(rate, 5.0))

    def plan(self) -> list[tuple[str, str]]:
        """Never-read searches first (interleaved, so the first queries reach
        every country before any country gets the last ones); then the rest by
        `priority`. Capped at the allowance."""
        pairs = [(q, c) for q in self.queries for c in self.countries]
        order = {pair: i for i, pair in enumerate(pairs)}
        pairs.sort(key=lambda p: (self.backfilled(*p), -self.priority(*p), order[p]))
        return pairs if self.max_requests is None else pairs[: self.max_requests]

    # ── requests ──

    def left(self) -> bool:
        return self.max_requests is None or self.requests_made < self.max_requests

    def counted(self, client: Any) -> int:
        """Count the request just made, if it went to the network. Returns 1/0."""
        last = getattr(client, "last_was_network", None)
        if last is None or last():
            self.requests_made += 1
            return 1
        return 0

    def open_visit(self, query: str, country: str) -> Visit:
        visit = Visit(query, country, backfill=self.days_for(query, country) == self.backfill_days)
        self.visits.append(visit)
        return visit
