"""What the machine found out about an employer, on disk.

The COMPANY NOTES slot in the planner prompt is filled by hand from
`profile/companies.yaml`. This is the fallback for every employer Simon has not
written about yet, and the reason it is a separate module from `research.py` is
narrow and load-bearing:

**NOTHING HERE MAY REACH THE NETWORK, AND NOTHING HERE MAY IMPORT ANYTHING THAT
CAN.** `planner._company_notes` imports this file while a letter is being
written. If the searching lived here too, every letter would drag `build_chain`
-> `ddgs` -> `httpx` into its import graph, and the next person to add a
`if stale: refresh()` line would find nothing structural stopping them. With the
split, `test_the_planner_never_imports_the_searcher` is a one-line assertion on
`sys.modules`. `research.py` imports this; this imports nothing of `research.py`.

Shape follows `search/cache.py`: sharded content-addressed JSON under
`data/cache/research/`, `fetched_at` first in the body, TTL checked on read only,
a torn file counted as a miss rather than raised. Two deliberate departures are
marked where they occur.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import Settings, get_settings
from ..jobsearch.companies import normalize_company

__all__ = ["CompanyResearch", "ResearchCache", "cached_notes", "SCHEMA_VERSION"]

#: Bumped when the record shape changes. An unrecognised version reads as a miss,
#: so a shape change re-researches instead of crashing on an old file.
SCHEMA_VERSION = 1


@dataclass
class CompanyResearch:
    """One employer's researched notes, and everything needed to defend them.

    `notes` is the only field the letter ever sees. The rest exists so that a
    sentence in a PDF can be traced back to a page six months later, and so that
    `apply research --show` can explain a blank.
    """

    company: str = ""
    key: str = ""
    notes: str = ""
    #: `[{"text": …, "source": url}]` — the per-sentence citation. `notes` is
    #: these joined with a space; keeping both means a single bad sentence can be
    #: identified without re-running anything.
    sentences: list[dict[str, Any]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    #: What the DRAFTING model said about its own work. RECORDED, NEVER CONSULTED.
    #: The fabrication this whole feature is built around came back with
    #: `confidence: "high"` and a citation to ResearchGate. No branch anywhere may
    #: read this field; it is here so the trace shows how confidently wrong a
    #: model was.
    model_confidence: str = ""
    model: str = ""
    verified: bool = False
    #: False when the drafting model also did the verifying, which is worth less
    #: and the human should be told which one they got.
    independent: bool = False
    #: `"<sentence> — <why>"` for every sentence a gate threw away. A good
    #: sentence killed by an over-strict rule has to be visible, or the rule can
    #: never be tuned.
    rejected: list[str] = field(default_factory=list)
    #: ok | thin | ungrounded | unverified
    outcome: str = ""
    fetched_at: str = ""
    schema_version: int = SCHEMA_VERSION

    @property
    def usable(self) -> bool:
        """May these notes go in front of the planner?

        Unverified notes are not usable, and that is the whole gate. Refusing
        costs exactly the status quo — a blank `company_angle` — which is already
        correct behaviour, so there is no reason to take the risk.
        """
        return bool(self.notes.strip()) and self.verified

    def to_json(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at,
            "schema_version": self.schema_version,
            "company": self.company,
            "key": self.key,
            "notes": self.notes,
            "sentences": self.sentences,
            "sources": self.sources,
            "queries": self.queries,
            "model_confidence": self.model_confidence,
            "model": self.model,
            "verified": self.verified,
            "independent": self.independent,
            "rejected": self.rejected,
            "outcome": self.outcome,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CompanyResearch:
        return cls(
            company=str(data.get("company") or ""),
            key=str(data.get("key") or ""),
            notes=str(data.get("notes") or ""),
            sentences=list(data.get("sentences") or []),
            sources=[str(u) for u in (data.get("sources") or [])],
            queries=[str(q) for q in (data.get("queries") or [])],
            model_confidence=str(data.get("model_confidence") or ""),
            model=str(data.get("model") or ""),
            verified=bool(data.get("verified")),
            independent=bool(data.get("independent")),
            rejected=[str(r) for r in (data.get("rejected") or [])],
            outcome=str(data.get("outcome") or ""),
            fetched_at=str(data.get("fetched_at") or ""),
            schema_version=int(data.get("schema_version") or 0),
        )


class ResearchCache:
    """One JSON per employer under `data/cache/research/`."""

    def __init__(
        self,
        root: pathlib.Path | str | None = None,
        ttl_days: int | None = None,
        settings: Settings | None = None,
    ) -> None:
        settings = settings or get_settings()
        self.root = pathlib.Path(root) if root else settings.cache_path / "research"
        self.ttl_days = (
            settings.apply_research_ttl_days if ttl_days is None else ttl_days
        )
        self.hits = 0
        self.misses = 0
        self.writes = 0

    def key_for(self, company: str) -> str:
        """The NORMALISED name, hashed.

        Normalised for the same reason `planner._company_notes` normalises before
        matching: "Helsing GmbH" and "Helsing" are one employer and must be one
        record, or the research done under one spelling is invisible to a posting
        that uses the other. An empty normalised name has no key, and `get`/`put`
        become no-ops rather than writing a record called "".
        """
        target = normalize_company(company)
        if not target:
            return ""
        return hashlib.sha1(target.encode()).hexdigest()

    def path_for(self, key: str) -> pathlib.Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, company: str, *, ignore_ttl: bool = False) -> CompanyResearch | None:
        """The stored record, or None on a miss, an expiry or a torn file."""
        key = self.key_for(company)
        if not key:
            return None
        path = self.path_for(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            self.misses += 1
            return None
        if int(data.get("schema_version") or 0) != SCHEMA_VERSION:
            self.misses += 1
            return None
        if not ignore_ttl and self.ttl_days > 0 and self.is_expired(data.get("fetched_at")):
            self.misses += 1
            return None
        self.hits += 1
        return CompanyResearch.from_json(data)

    def is_expired(self, fetched_at: str | None) -> bool:
        if not fetched_at:
            return True
        try:
            ts = datetime.fromisoformat(fetched_at)
        except ValueError:
            return True
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return datetime.now(UTC) - ts > timedelta(days=self.ttl_days)

    def put(self, record: CompanyResearch) -> pathlib.Path | None:
        key = record.key or self.key_for(record.company)
        if not key:
            return None
        record.key = key
        record.schema_version = SCHEMA_VERSION
        if not record.fetched_at:
            record.fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # uuid4, NOT `SerpCache`'s shared `.json.tmp`. That name races: two
        # writers of one record rename out from under each other and the loser
        # raises FileNotFoundError. The pre-pass is serial today, but this is not
        # the thing that should break when it stops being — see `llm.py:_store`,
        # where a 16-thread stress test produced 213 of them.
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(
                json.dumps(record.to_json(), indent=2, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        self.writes += 1
        return path

    def drop(self, company: str) -> bool:
        key = self.key_for(company)
        if not key:
            return False
        try:
            self.path_for(key).unlink()
        except OSError:
            return False
        return True

    def all(self) -> list[CompanyResearch]:
        """Every stored record, freshest first. Ignores the TTL — `--show` is for
        looking at what is there, including what has gone stale."""
        if not self.root.exists():
            return []
        out: list[CompanyResearch] = []
        for path in self.root.rglob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, ValueError):
                continue
            if int(data.get("schema_version") or 0) != SCHEMA_VERSION:
                continue
            out.append(CompanyResearch.from_json(data))
        return sorted(out, key=lambda r: r.fetched_at, reverse=True)

    def size(self) -> tuple[int, int]:
        files = list(self.root.rglob("*.json")) if self.root.exists() else []
        return len(files), sum(f.stat().st_size for f in files)


def cached_notes(company: str, *, cache: ResearchCache | None = None) -> str:
    """The machine's sentences for this employer, or `""`.

    NEVER RAISES. This is called while a letter is being written, and a missing
    directory, a torn file or a settings object that will not build must not be
    able to fail a run whose only loss is a paragraph that is blank today anyway.
    A record that exists but is not `usable` returns `""`, which is
    indistinguishable to the planner from no record at all — correct, because the
    letter behaves identically for both.
    """
    try:
        record = (cache or ResearchCache()).get(company)
    except Exception:  # noqa: BLE001 - see the docstring; a letter outranks a note
        return ""
    if record is None or not record.usable:
        return ""
    return record.notes.strip()
