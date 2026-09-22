"""The owner's search profile — §4 and §45 of the spec.

Everything that says what the owner wants lives in `config/job_search.yaml`,
never in code (§3: "must not hard-code these personal details into application
logic"). The file is personal and gitignored; `job_search.example.yaml` is the
tracked starting point and is what loads when the personal file is absent.

Validation is strict about shape and lenient about content: an unknown country
name is a loud error (it would silently filter everything), an empty list is a
legitimate "no preference".
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import config_dir
from .locations import country_code
from .roles import LABELS

CONFIG_FILE = "job_search.yaml"
EXAMPLE_FILE = "job_search.example.yaml"

EMPLOYMENT_TYPES = (
    "full_time", "part_time", "internship", "working_student", "graduate_programme",
    "fixed_term", "permanent", "contract", "freelance",
)
WORK_MODES = ("remote", "hybrid", "onsite")


def _codes(values: Any, field: str) -> list[str]:
    out: list[str] = []
    for value in values or []:
        code = country_code(str(value))
        if not code:
            raise ValueError(f"{field}: {value!r} is not a country this app recognises")
        if code not in out:
            out.append(code)
    return out


class Locations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Where you would take a job. Empty = anywhere.
    countries: list[str] = Field(default_factory=list)
    #: Scored up, never required.
    preferred_cities: list[str] = Field(default_factory=list)
    #: A job ONLY in these cities is filtered out.
    excluded_cities: list[str] = Field(default_factory=list)
    #: Where you live. Decides whether "Remote (EU)" is open to you (§59).
    home_country: str | None = None
    work_modes: list[str] = Field(default_factory=lambda: list(WORK_MODES))

    @field_validator("countries", mode="before")
    @classmethod
    def _countries(cls, v: Any) -> Any:
        return _codes(v, "locations.countries")

    @field_validator("home_country", mode="before")
    @classmethod
    def _home(cls, v: Any) -> Any:
        if v in (None, ""):
            return None
        code = country_code(str(v))
        if not code:
            raise ValueError(f"locations.home_country: {v!r} is not a recognised country")
        return code

    @field_validator("work_modes")
    @classmethod
    def _modes(cls, v: list[str]) -> list[str]:
        bad = [m for m in v if m not in WORK_MODES]
        if bad:
            raise ValueError(f"locations.work_modes: {bad} — use {list(WORK_MODES)}")
        return v


class Roles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Labels a job must carry at least one of to pass the role filter.
    accepted: list[str] = Field(default_factory=lambda: [
        "MACHINE_LEARNING", "AI", "DATA_SCIENCE", "APPLIED_SCIENCE", "DECISION_SCIENCE",
        "OPTIMIZATION", "SIMULATION",
    ])
    #: How much each label is worth in the role component, 0-1.
    preference: dict[str, float] = Field(default_factory=lambda: {
        "MACHINE_LEARNING": 1.0, "AI": 0.95, "OPTIMIZATION": 1.0, "SIMULATION": 0.95,
        "DECISION_SCIENCE": 0.95, "APPLIED_SCIENCE": 0.85, "DATA_SCIENCE": 0.9,
        "DATA_ENGINEERING": 0.5, "SOFTWARE_ENGINEERING": 0.45, "ANALYTICS": 0.35,
        "BI": 0.15, "OTHER": 0.0,
    })
    #: Titles you would recognise as exactly what you want. A close match earns
    #: a bonus; nothing is required to match one.
    preferred_titles: list[str] = Field(default_factory=list)

    @field_validator("accepted")
    @classmethod
    def _labels(cls, v: list[str]) -> list[str]:
        v = [x.strip().upper() for x in v]
        bad = [x for x in v if x not in LABELS]
        if bad:
            raise ValueError(f"roles.accepted: {bad} — use {list(LABELS)}")
        return v

    @field_validator("preference")
    @classmethod
    def _pref(cls, v: dict[str, float]) -> dict[str, float]:
        out = {}
        for key, value in v.items():
            label = key.strip().upper()
            if label not in LABELS:
                raise ValueError(f"roles.preference: {key!r} is not a label ({list(LABELS)})")
            out[label] = max(0.0, min(1.0, float(value)))
        return out


class Seniority(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Whole words that remove a job when they appear in its title (§14).
    excluded_title_words: list[str] = Field(default_factory=lambda: [
        "senior", "sr", "staff", "principal", "lead", "manager", "director", "head",
        "vp", "vice president", "chief", "distinguished",
    ])
    #: Phrases that protect a title from the rule above. "Member of Technical
    #: Staff" is a flat title at several AI labs, not a seniority.
    allowed_title_phrases: list[str] = Field(default_factory=lambda: [
        "member of technical staff", "member of the technical staff",
    ])
    #: A job that explicitly REQUIRES more than this is filtered out. Unstated
    #: never is.
    max_experience_years: float = Field(default=2.0, ge=0, le=30)


class WorkAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    needs_sponsorship: bool = True
    #: Countries you can already work in — no sponsorship needed there.
    authorized_countries: list[str] = Field(default_factory=list)

    @field_validator("authorized_countries", mode="before")
    @classmethod
    def _countries(cls, v: Any) -> Any:
        return _codes(v, "work_authorization.authorized_countries")


class Weights(BaseModel):
    """§25. Relative, not percentages — they are normalised when scoring."""

    model_config = ConfigDict(extra="forbid")

    role: float = Field(default=25, ge=0)
    experience: float = Field(default=20, ge=0)
    skills: float = Field(default=20, ge=0)
    location: float = Field(default=10, ge=0)
    visa: float = Field(default=10, ge=0)
    career: float = Field(default=10, ge=0)
    company: float = Field(default=5, ge=0)

    @model_validator(mode="after")
    def _not_all_zero(self) -> Weights:
        if sum(self.model_dump().values()) <= 0:
            raise ValueError("weights: at least one must be above zero")
        return self


class ArbeitnowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    #: 250 postings a page, newest first. The API asks not to be abused.
    max_pages: int = Field(default=4, ge=1, le=20)


class AdzunaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    #: Searched against job TITLES in each country (`title_only`).
    queries: list[str] = Field(default_factory=lambda: [
        "data scientist", "machine learning", "AI engineer", "applied scientist",
        "research engineer", "decision scientist", "optimisation", "simulation engineer",
    ])
    #: Empty = your `locations.countries` that Adzuna covers.
    countries: list[str] = Field(default_factory=list)
    max_requests_per_run: int = Field(default=40, ge=0, le=500)
    #: A ceiling across a whole day, across runs — well inside the free tier.
    daily_request_cap: int = Field(default=200, ge=0)
    results_per_page: int = Field(default=50, ge=1, le=50)
    max_days_old: int = Field(default=14, ge=1, le=90)
    title_only: bool = True

    @field_validator("countries", mode="before")
    @classmethod
    def _countries(cls, v: Any) -> Any:
        return _codes(v, "sources.adzuna.countries")


class Sources(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Poll the public ATS board of every target company that has one.
    target_ats: bool = True
    #: …and of every company on the people-search list (config/companies.csv,
    #: the motorsport teams) that has one, so "motorsport list only" has jobs.
    people_list_ats: bool = True
    arbeitnow: ArbeitnowConfig = Field(default_factory=ArbeitnowConfig)
    adzuna: AdzunaConfig = Field(default_factory=AdzunaConfig)
    #: How long a fetched page is reused. A daily search wants hours, not the
    #: week the Openings cache keeps.
    cache_hours: float = Field(default=6.0, ge=0, le=168)


class JobSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locations: Locations = Field(default_factory=Locations)
    roles: Roles = Field(default_factory=Roles)
    seniority: Seniority = Field(default_factory=Seniority)
    #: Accepted employment types. A job whose stated types are all outside
    #: this list is filtered; a job that states none passes.
    employment_types: list[str] = Field(default_factory=lambda: list(EMPLOYMENT_TYPES))
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    #: Your highest degree, finished or in progress. A posting that REQUIRES a
    #: PhD is filtered out unless this is `phd`; "PhD preferred" never is.
    degree: Literal["bachelors", "masters", "phd"] = "masters"
    #: §23 — concepts that pull a job up the list without being required.
    career_interests: list[str] = Field(default_factory=list)
    #: Spoken languages (ISO 639-1). A posting that asks for one you do not
    #: list is marked down and says so — never filtered, since "German is a
    #: plus" and "German required" are hard to tell apart reliably.
    languages_spoken: list[str] = Field(default_factory=lambda: ["en"])
    weights: Weights = Field(default_factory=Weights)
    sources: Sources = Field(default_factory=Sources)
    #: Ask the LLM for a second opinion on this many top jobs per run (0 = off).
    ai_fit_top_n: int = Field(default=0, ge=0, le=200)

    @field_validator("languages_spoken", mode="before")
    @classmethod
    def _langs(cls, v: Any) -> Any:
        return [str(x).strip().lower()[:2] for x in v or [] if str(x).strip()]

    @field_validator("employment_types")
    @classmethod
    def _types(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if t not in EMPLOYMENT_TYPES]
        if bad:
            raise ValueError(f"employment_types: {bad} — use {list(EMPLOYMENT_TYPES)}")
        return v

    def fingerprint(self) -> str:
        """Which profile produced a score — stored on every `job_matches` row."""
        blob = json.dumps(self.model_dump(exclude={"sources", "ai_fit_top_n"}),
                          sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()[:12]

    def is_authorized(self, country: str | None) -> bool:
        wa = self.work_authorization
        return bool(country) and (not wa.needs_sponsorship or country in wa.authorized_countries)


def config_path(*, personal_only: bool = False) -> Path:
    personal = config_dir() / CONFIG_FILE
    if personal.exists() or personal_only:
        return personal
    return config_dir() / EXAMPLE_FILE


def load_config(path: Path | str | None = None) -> JobSearchConfig:
    """The profile. Raises `ValueError` with the field at fault on bad input."""
    p = Path(path) if path else config_path()
    if not p.exists():
        return JobSearchConfig()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    try:
        return JobSearchConfig.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise ValueError(f"{p.name}: {exc}") from exc


def save_config(cfg: JobSearchConfig, path: Path | str | None = None) -> Path:
    """Write the profile to the PERSONAL file — never over the example."""
    p = Path(path) if path else config_dir() / CONFIG_FILE
    header = (
        "# Your job-search profile. Personal — this file is gitignored.\n"
        "# Edited from the 🧭 Job Setup page, or by hand; see\n"
        "# job_search.example.yaml for what every field means.\n"
    )
    data = cfg.model_dump()
    p.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                 encoding="utf-8")
    return p
