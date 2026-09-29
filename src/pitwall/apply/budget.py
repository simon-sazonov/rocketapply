"""How many applications fit in what is left of today's Groq quota.

RocketApply caps a batch at this number rather than queueing past it. The
arithmetic is simple; the inputs are where the care goes:

* **Room** is per key, from `run/key_usage.json` — the same ledger the key pool
  spends against — over a ROLLING 24 hours, not a calendar day. A key parked on
  its daily ceiling has none. The per-minute limit is ignored here: it is a
  pause, not a ceiling, and the router already waits it out.
* **Cost per job** is the 80th percentile of what real runs in `output/` spent
  on uncached calls, per mode — the mean undersells it, because a letter that
  needs a second draft costs half as much again. Until a mode has five runs to
  learn from, a measured estimate stands in.
* **A 10% margin** for Groq calls no ledger records (the job search's AI fit,
  PitWall's people scoring), and a reserve per employer whose company research
  is not cached yet.

It is an estimate, and says so: `Budget.basis` names where every number came
from, so the screen can too.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .keys import DAILY_TOKENS, USAGE_PATH, KeyPool

__all__ = ["MODES", "MODE_LABELS", "FALLBACK", "KeyRoom", "Budget", "per_job_tokens",
           "key_room", "fits_today", "run_mode"]

MODES = ("cv", "cv_letter", "cv_letter_audit")
MODE_LABELS = {
    "cv": "CV only",
    "cv_letter": "CV + letter",
    "cv_letter_audit": "CV + letter + independent fact-check",
}
#: Measured on this machine, 28 Sep 2026: one `job_analysis` call for a CV; the
#: batch bench (audit off) and bench2 (audit on) ledgers for the letters.
FALLBACK = {"cv": 2_600, "cv_letter": 18_200, "cv_letter_audit": 34_200}
#: One employer's research: a `company_research` call and its verification.
RESEARCH_TOKENS = 4_000
MARGIN = 0.10
MIN_SAMPLES = 5


@dataclass
class KeyRoom:
    key: str                # fingerprint only — never the key
    day_used: int
    day_limit: int
    room: int
    parked_for: float = 0.0
    reason: str = ""


@dataclass
class Budget:
    mode: str
    per_job: int
    samples: int
    keys: list[KeyRoom] = field(default_factory=list)
    room: int = 0
    margin: float = MARGIN
    reserve: int = 0
    fits: int = 0
    #: Employers in the asked-for jobs with no research cached (letter modes).
    unresearched: int = 0
    basis: str = ""

    @property
    def usable(self) -> int:
        return max(0, int(self.room * (1 - self.margin)))

    @property
    def daily_limit(self) -> int:
        return sum(k.day_limit for k in self.keys)


# ─────────────────────────────── cost per job ────────────────────────────────


def run_mode(calls: Sequence[dict[str, Any]]) -> str:
    """What a run did, read from its model calls."""
    tasks = {str(c.get("stage") or c.get("task") or "") for c in calls}
    if not tasks & {"letter_write", "narrative_plan"}:
        return "cv"
    return "cv_letter_audit" if "fact_check" in tasks else "cv_letter"


def _uncached(calls: Sequence[dict[str, Any]]) -> int:
    return sum(int(c.get("prompt_tokens") or 0) + int(c.get("completion_tokens") or 0)
               for c in calls if isinstance(c, dict) and not c.get("cached"))


def _samples(output: pathlib.Path) -> dict[str, list[tuple[str, int]]]:
    """(generated_at, tokens) per mode, from every application's provenance."""
    from .history import _application_dirs

    out: dict[str, list[tuple[str, int]]] = {m: [] for m in MODES}
    if not output.exists():
        return out
    for directory in _application_dirs(output):
        try:
            data = json.loads((directory / "provenance.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        calls = [c for c in (data.get("llm_calls") or []) if isinstance(c, dict)]
        spent = _uncached(calls)
        if spent:                   # a fully cached rerun says nothing about cost
            out[run_mode(calls)].append((str(data.get("generated_at") or ""), spent))
    return out


def _quantile(values: list[int], q: float) -> int:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def per_job_tokens(mode: str, *, output: pathlib.Path | str = "output", quantile: float = 0.8,
                   min_samples: int = MIN_SAMPLES) -> tuple[int, int, str]:
    """(tokens, samples, where the number came from) for one job in `mode`."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    values = [n for _, n in _samples(pathlib.Path(output))[mode]]
    if len(values) >= min_samples:
        return (_quantile(values, quantile), len(values),
                f"p{round(quantile * 100)} of {len(values)} past run(s)")
    return (FALLBACK[mode], len(values),
            f"measured estimate ({len(values)} past run(s), need {min_samples})")


# ───────────────────────────────── room left ─────────────────────────────────


def key_room(settings: Any = None, *, usage_path: pathlib.Path | None = USAGE_PATH,
             output: pathlib.Path | str = "output") -> list[KeyRoom]:
    """What each Groq key can still spend in the rolling day."""
    from ..config import get_settings

    settings = settings or get_settings()
    keys = settings.provider_keys("groq")
    if not keys:
        return []
    pool = KeyPool(keys, usage_path=usage_path)     # reads the ledger; spends nothing
    rows = pool.status()
    if len(keys) == 1:
        # One key has no pool at run time, so the ledger never saw its spend.
        # The applications themselves are the record.
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
        used = 0
        for runs in _samples(pathlib.Path(output)).values():
            for at, n in runs:
                try:
                    when = dt.datetime.fromisoformat(at)
                except ValueError:
                    continue
                if when.tzinfo is None:
                    when = when.replace(tzinfo=dt.UTC)
                if when >= since:
                    used += n
        rows[0]["day_used"] = max(int(rows[0]["day_used"]), used)
    out = []
    for row in rows:
        limit = int(row.get("day_limit") or DAILY_TOKENS)
        used = int(row.get("day_used") or 0)
        parked_on_day = row.get("reason") == "daily ceiling" and float(row.get("parked_for") or 0) > 0
        out.append(KeyRoom(
            key=str(row["key"]), day_used=used, day_limit=limit,
            room=0 if parked_on_day else max(0, limit - used),
            parked_for=float(row.get("parked_for") or 0.0), reason=str(row.get("reason") or ""),
        ))
    return out


def _research_reserve(job_ids: Sequence[int], settings: Any) -> list[str]:
    """Employers among these jobs, in order, whose research is not cached."""
    try:
        from .jobs import companies_for
        from .research_cache import ResearchCache

        cache = ResearchCache(settings=settings)
        names = companies_for(list(job_ids))
    except Exception:  # noqa: BLE001 - no database, no cache: reserve nothing
        return []
    order: list[str] = []
    for job_id in job_ids:
        name = names.get(job_id)
        if name and name not in order:
            order.append(name)
    return [name for name in order if cache.get(name) is None]


def fits_today(mode: str, *, job_ids: Sequence[int] = (), settings: Any = None,
               margin: float = MARGIN, output: pathlib.Path | str = "output",
               usage_path: pathlib.Path | None = USAGE_PATH) -> Budget:
    """How many of `job_ids` (best first) today's quota covers in `mode`.

    With no `job_ids` it answers "how many jobs like this", without a research
    reserve.
    """
    from ..config import get_settings

    settings = settings or get_settings()
    per_job, samples, basis = per_job_tokens(mode, output=output)
    rooms = key_room(settings, usage_path=usage_path, output=output)
    budget = Budget(mode=mode, per_job=per_job, samples=samples, keys=rooms,
                    room=sum(k.room for k in rooms), margin=margin)
    usable = budget.usable
    if not job_ids:
        budget.fits = usable // per_job if per_job else 0
        budget.basis = f"{basis}; {round(margin * 100)}% margin"
        return budget

    unresearched = _research_reserve(job_ids, settings) if mode != "cv" else []
    pending = set(unresearched)
    names: dict[int, str] = {}
    if pending:
        from .jobs import companies_for

        names = companies_for(list(job_ids))
    spent, reserved, seen, fits = 0, 0, set(), 0
    for job_id in job_ids:
        company = names.get(job_id)
        extra = RESEARCH_TOKENS if company in pending and company not in seen else 0
        if spent + per_job + reserved + extra > usable:
            break
        spent += per_job
        reserved += extra
        if extra:
            seen.add(company)
        fits += 1
    budget.fits = fits
    budget.reserve = reserved
    budget.unresearched = len(pending)
    budget.basis = (f"{basis}; {round(margin * 100)}% margin"
                    + (f"; {RESEARCH_TOKENS:,} per unresearched employer" if pending else ""))
    return budget


def rolling_reset_hint(rooms: Sequence[KeyRoom], usage_path: pathlib.Path | None = USAGE_PATH
                       ) -> str:
    """When the oldest spend leaves the window — "more room from 14:20"."""
    if not usage_path or not usage_path.exists():
        return ""
    try:
        saved = json.loads(usage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    now = time.time()
    oldest = [float(w) for row in saved.get("groq", []) for w, _ in row.get("spend", [])
              if now - float(w) < 86_400]
    if not oldest:
        return ""
    when = dt.datetime.fromtimestamp(min(oldest) + 86_400)
    return f"more room from {when:%H:%M}"
