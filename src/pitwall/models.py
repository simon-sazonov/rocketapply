"""Pydantic models for every entity — §4 of PITWALL_PLAN.md.

These mirror the SQLite schema in :mod:`pitwall.db` but are the types the
Python layer actually passes around. JSON-array columns in SQLite become real
`list[str]` here; the repositories do the (de)serialisation.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

# ───────────────────────────── enumerations ──────────────────────────────

RoleFamily = Literal[
    "data_science",
    "data_engineering",
    "strategy",
    "performance",
    "simulation",
    "vehicle_dynamics",
    "software",
    "operations",
    "leadership",
]

Seniority = Literal[
    "intern",
    "graduate",
    "junior",
    "mid",
    "senior",
    "lead",
    "head",
    "executive",
    "unknown",
]

CompanyMatch = Literal[
    "exact", "parent", "subsidiary", "same_name_different_org", "none", "unknown"
]
RoleMatch = Literal["exact", "adjacent", "unrelated", "unknown"]
#: `skipped_triage` is deliberately distinct from `pending_llm`: one means "we
#: chose not to spend a verdict on this person", the other means "not done yet".
#: Conflating them would make an unfinished run and a triaged one look identical.
Verdict = Literal[
    "strong", "possible", "weak", "reject", "needs_review", "pending_llm",
    "skipped_triage",
]
CRMStatus = Literal[
    "new", "saved", "ignored", "contacted", "replied", "meeting", "closed"
]
QueryStatus = Literal["pending", "running", "done", "failed", "skipped"]

ROLE_FAMILIES: tuple[str, ...] = (
    "data_science",
    "data_engineering",
    "strategy",
    "performance",
    "simulation",
    "vehicle_dynamics",
    "software",
    "operations",
    "leadership",
)

SENIORITIES: tuple[str, ...] = (
    "intern",
    "graduate",
    "junior",
    "mid",
    "senior",
    "lead",
    "head",
    "executive",
    "unknown",
)

CRM_STATUSES: tuple[str, ...] = (
    "new",
    "saved",
    "ignored",
    "contacted",
    "replied",
    "meeting",
    "closed",
)


# ─────────────────────────────── inputs ──────────────────────────────────


class Company(BaseModel):
    """One row of `config/companies.csv`."""

    model_config = ConfigDict(str_strip_whitespace=True)

    id: int | None = None
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    negative_keywords: list[str] = Field(default_factory=list)
    linkedin_slug: str | None = None
    domain: str | None = None
    series: str | None = None
    country: str | None = None
    tier: int = Field(default=2, ge=1, le=3)
    active: bool = True

    @field_validator("aliases", "negative_keywords", mode="before")
    @classmethod
    def _pipe_split(cls, v: Any) -> Any:
        return _split_multi(v)

    @property
    def all_names(self) -> list[str]:
        """Canonical name first, then aliases — the order matching prefers."""
        seen: list[str] = []
        for n in [self.name, *self.aliases]:
            if n and n not in seen:
                seen.append(n)
        return seen


class Role(BaseModel):
    """One row of `config/roles.csv`."""

    model_config = ConfigDict(str_strip_whitespace=True)

    id: int | None = None
    title: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    family: str
    seniority: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    priority: int = Field(default=2, ge=1, le=3)
    active: bool = True

    @field_validator("aliases", "seniority", "keywords", mode="before")
    @classmethod
    def _pipe_split(cls, v: Any) -> Any:
        return _split_multi(v)

    @field_validator("family")
    @classmethod
    def _known_family(cls, v: str) -> str:
        v = v.strip().lower().replace(" ", "_").replace("-", "_")
        if v not in ROLE_FAMILIES:
            raise ValueError(f"family {v!r} not in {ROLE_FAMILIES}")
        return v

    @field_validator("seniority")
    @classmethod
    def _known_seniority(cls, v: list[str]) -> list[str]:
        out = []
        for s in v:
            s2 = s.strip().lower()
            if s2 and s2 not in SENIORITIES:
                raise ValueError(f"seniority {s!r} not in {SENIORITIES}")
            if s2:
                out.append(s2)
        return out

    @property
    def all_titles(self) -> list[str]:
        seen: list[str] = []
        for t in [self.title, *self.aliases]:
            if t and t not in seen:
                seen.append(t)
        return seen


# ─────────────────────────────── queries ─────────────────────────────────


class QuerySpec(BaseModel):
    """A planned search query, before it has been executed."""

    id: int | None = None
    company_id: int | None = None
    role_id: int | None = None
    company_name: str | None = None
    role_title: str | None = None
    template_id: str
    query_text: str
    query_hash: str = ""
    priority: float = 0.0
    status: str = "pending"
    provider: str | None = None
    results_count: int = 0
    error: str | None = None
    last_run_at: str | None = None

    @field_validator("query_text")
    @classmethod
    def _collapse_ws(cls, v: str) -> str:
        return " ".join(v.split())

    def hashed(self, provider: str) -> str:
        return make_query_hash(provider, self.query_text)


def make_query_hash(provider: str, query_text: str) -> str:
    """`sha1(provider|normalized_query_text)` — the dedup key (§5, Phase 1)."""
    normalized = " ".join(query_text.split()).casefold()
    return hashlib.sha1(f"{provider}|{normalized}".encode()).hexdigest()


# ───────────────────────────── search hits ───────────────────────────────


class SearchHit(BaseModel):
    """One SERP row, provider-agnostic."""

    model_config = ConfigDict(frozen=True)

    rank: int
    url: str
    title: str = ""
    snippet: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


class SearchResultRow(BaseModel):
    """A persisted SERP row (`search_results`)."""

    id: int | None = None
    query_id: int | None = None
    provider: str
    rank: int
    url: str
    canonical_url: str
    title: str = ""
    snippet: str = ""
    raw_json: str | None = None
    fetched_at: str | None = None


# ─────────────────────────────── people ─────────────────────────────────


class Candidate(BaseModel):
    """A distinct person (`candidates`), keyed by LinkedIn slug."""

    id: int | None = None
    linkedin_slug: str
    profile_url: str
    full_name: str | None = None
    headline: str | None = None
    current_title: str | None = None
    current_company: str | None = None
    location: str | None = None
    evidence_count: int = 0
    first_seen_at: str | None = None
    last_seen_at: str | None = None


class Evidence(BaseModel):
    """A SERP row that surfaced a candidate, joined back to its query."""

    search_result_id: int
    query_id: int | None = None
    query_text: str | None = None
    template_id: str | None = None
    provider: str | None = None
    title: str = ""
    snippet: str = ""
    url: str = ""
    rank: int = 0
    company_name: str | None = None
    role_title: str | None = None
    fetched_at: str | None = None


# ─────────────────────────────── scoring ────────────────────────────────


class HeuristicResult(BaseModel):
    """Output of the free, deterministic pre-score (§9.1)."""

    score: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    hard_reject: bool = False


class LLMVerdict(BaseModel):
    """The per-candidate object the LLM must return (§9.2)."""

    model_config = ConfigDict(extra="ignore")

    candidate_id: int
    is_person_profile: bool = True
    full_name: str | None = None
    current_company: str | None = None
    current_title: str | None = None
    is_current_employee: bool | None = None
    company_match: CompanyMatch = "unknown"
    role_match: RoleMatch = "unknown"
    seniority: Seniority = "unknown"
    motorsport_experience: bool | None = None
    score: float = Field(default=0.0, ge=0, le=100)
    confidence: float = Field(default=0.0, ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)

    @field_validator("score", mode="before")
    @classmethod
    def _clamp_score(cls, v: Any) -> Any:
        try:
            return max(0.0, min(100.0, float(v)))
        except (TypeError, ValueError):
            return 0.0

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_conf(cls, v: Any) -> Any:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        if f > 1.0:  # models sometimes answer 0-100
            f = f / 100.0
        return max(0.0, min(1.0, f))

    @field_validator("company_match", "role_match", "seniority", mode="before")
    @classmethod
    def _norm_enum(cls, v: Any, info: ValidationInfo) -> Any:
        """Normalise case/spacing, and coerce anything unrecognised to
        `unknown`.

        A model inventing a value must not fail the whole batch — that would
        cost a retry and could push seven innocent candidates into
        `needs_review` because of one bad token.
        """
        if not isinstance(v, str):
            return "unknown"
        cleaned = v.strip().lower().replace(" ", "_").replace("-", "_")
        allowed = _ENUM_VALUES.get(info.field_name or "", ())
        return cleaned if cleaned in allowed else "unknown"

    @field_validator("reasons", "red_flags", mode="before")
    @classmethod
    def _listify(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v


#: Allowed values per LLMVerdict enum field, used to coerce model output.
_ENUM_VALUES: dict[str, tuple[str, ...]] = {
    "company_match": (
        "exact", "parent", "subsidiary", "same_name_different_org", "none", "unknown",
    ),
    "role_match": ("exact", "adjacent", "unrelated", "unknown"),
    "seniority": SENIORITIES,
}


class LLMBatchResponse(BaseModel):
    """The whole JSON object the LLM returns for one batch."""

    results: list[LLMVerdict] = Field(default_factory=list)


class Score(BaseModel):
    """A persisted score row (`scores`) — one per candidate×company×role×prompt."""

    id: int | None = None
    candidate_id: int
    company_id: int | None = None
    role_id: int | None = None
    heuristic_score: float | None = None
    llm_score: float | None = None
    final_score: float | None = None
    verdict: str = "reject"
    is_current_employee: bool | None = None
    company_match: str | None = None
    role_match: str | None = None
    seniority: str | None = None
    confidence: float | None = None
    reasons: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    model: str | None = None
    #: Which LLM answered. Degradation is per batch, so one run can legitimately
    #: mix providers — this is what makes such a run auditable afterwards.
    provider: str | None = None
    prompt_version: str = "v1"
    scored_at: str | None = None


# ───────────────────────────────── CRM ──────────────────────────────────


class CRMEntry(BaseModel):
    candidate_id: int
    status: CRMStatus = "new"
    note: str | None = None
    tags: list[str] = Field(default_factory=list)
    contacted_at: str | None = None
    followup_at: str | None = None
    updated_at: str | None = None

    @field_validator("followup_at", "contacted_at", mode="before")
    @classmethod
    def _dates_to_iso(cls, v: Any) -> Any:
        if isinstance(v, datetime):
            return v.isoformat(timespec="seconds")
        if isinstance(v, date):
            return v.isoformat()
        return v


# ────────────────────────────── bookkeeping ─────────────────────────────


class Run(BaseModel):
    id: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    queries_planned: int = 0
    queries_executed: int = 0
    results_fetched: int = 0
    candidates_new: int = 0
    llm_calls: int = 0
    llm_tokens_in: int = 0
    llm_tokens_out: int = 0
    cost_estimate: float = 0.0
    status: str = "running"
    error: str | None = None


class ValidationIssue(BaseModel):
    """One row-level problem found while loading an input CSV."""

    file: str
    row: int
    column: str | None = None
    level: Literal["error", "warning"] = "error"
    message: str
    value: str | None = None


class ValidationReport(BaseModel):
    issues: list[ValidationIssue] = Field(default_factory=list)
    rows_read: int = 0
    rows_ok: int = 0

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(
        self,
        file: str,
        row: int,
        message: str,
        *,
        column: str | None = None,
        level: str = "error",
        value: str | None = None,
    ) -> None:
        self.issues.append(
            ValidationIssue(
                file=file,
                row=row,
                column=column,
                level=level,  # type: ignore[arg-type]
                message=message,
                value=value,
            )
        )

    def merge(self, other: ValidationReport) -> ValidationReport:
        return ValidationReport(
            issues=[*self.issues, *other.issues],
            rows_read=self.rows_read + other.rows_read,
            rows_ok=self.rows_ok + other.rows_ok,
        )


# ────────────────────────────── helpers ─────────────────────────────────


def _split_multi(v: Any) -> Any:
    """Split a `a|b; c` cell into a clean list. Idempotent on lists."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        s = v.strip()
        if not s or s in {"-", "—", "–", "n/a", "N/A", "none", "None"}:
            return []
        parts: list[str] = []
        for chunk in s.split("|"):
            for sub in chunk.split(";"):
                sub = sub.strip()
                if sub and sub not in {"-", "—", "–"}:
                    parts.append(sub)
        # de-dup, preserve order
        seen: list[str] = []
        for p in parts:
            if p not in seen:
                seen.append(p)
        return seen
    return v
