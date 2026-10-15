"""STAGE 1 — a posting becomes structured facts.

The anti-hallucination measure here is cheap and effective: every extracted
skill must carry a short verbatim span from the posting, and the program checks
that the span really occurs. A 20B model will occasionally invent a requirement;
this catches it for free, and a fabricated requirement is worse than a missed
one because the truth layer turns missing requirements into forbidden claims.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

from .llm import TaskRouter

log = logging.getLogger(__name__)

__all__ = ["JobAnalysis", "Requirement", "analyze_job", "JOB_ANALYSIS_SCHEMA"]

PROMPTS = pathlib.Path(__file__).parent / "prompts"

ROLE_FAMILIES = (
    "DATA_SCIENCE", "MACHINE_LEARNING", "DATA_ENGINEERING", "ANALYTICS",
    "RESEARCH", "QUANTITATIVE", "SOFTWARE", "OTHER",
)
SENIORITIES = ("INTERN", "ENTRY_LEVEL", "JUNIOR", "MID", "SENIOR", "UNKNOWN")

#: Descriptions run long and the tail is usually benefits and boilerplate.
DESCRIPTION_LIMIT = 6000

#: Headings worth keeping when a description has to be truncated — the
#: requirements section is the part every downstream stage actually reads.
SECTION_HEADINGS = re.compile(
    r"^\s*(?:#+\s*)?(requirements?|qualifications?|what you.{0,12}(?:bring|need|have)|"
    r"your profile|skills?|we are looking for|must have|essential)\b",
    re.I | re.M,
)

_SKILL = {
    "type": "object",
    "properties": {
        "term": {"type": "string"},
        "evidence_quote": {"type": "string"},
    },
    "required": ["term", "evidence_quote"],
    "additionalProperties": False,
}

JOB_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "role_family": {"type": "string", "enum": list(ROLE_FAMILIES)},
        "seniority": {"type": "string", "enum": list(SENIORITIES)},
        "required_skills": {"type": "array", "items": _SKILL},
        "preferred_skills": {"type": "array", "items": _SKILL},
        "responsibilities": {"type": "array", "items": {"type": "string"}},
        "domain": {"type": "array", "items": {"type": "string"}},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "language": {"type": "string"},
        "company_signals": {"type": "array", "items": {"type": "string"}},
        "hard_gates": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "role_family", "seniority", "required_skills", "preferred_skills",
        "responsibilities", "domain", "keywords", "language", "company_signals",
        "hard_gates",
    ],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Requirement:
    term: str
    evidence_quote: str


@dataclass
class JobAnalysis:
    role_family: str = "OTHER"
    seniority: str = "UNKNOWN"
    required_skills: list[Requirement] = field(default_factory=list)
    preferred_skills: list[Requirement] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)
    domain: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    language: str = "en"
    company_signals: list[str] = field(default_factory=list)
    hard_gates: list[str] = field(default_factory=list)
    #: Skills whose quote was not found in the posting. Dropped, not trusted.
    rejected: list[tuple[str, str]] = field(default_factory=list)
    truncated: bool = False
    description_sha: str = ""

    @property
    def family_slug(self) -> str:
        """`DATA_SCIENCE` -> `data_science`, matching `use_for` in the profile."""
        return self.role_family.lower()

    @property
    def all_terms(self) -> list[str]:
        return [r.term for r in (*self.required_skills, *self.preferred_skills)]


#: Models return "english" or "English (US)" about as often as "en", and
#: truncating to two characters would turn "deutsch" into "de" by luck and
#: "portuguese" into "po" by accident.
_LANGUAGE_NAMES = {
    "english": "en", "german": "de", "deutsch": "de", "french": "fr",
    "français": "fr", "portuguese": "pt", "português": "pt", "spanish": "es",
    "español": "es", "italian": "it", "italiano": "it", "dutch": "nl",
    "nederlands": "nl", "swedish": "sv", "polish": "pl", "danish": "da",
    "norwegian": "no", "finnish": "fi", "ukrainian": "uk", "українська": "uk",
    "russian": "ru", "русский": "ru", "polski": "pl",
}


def _language(value: Any) -> str:
    text = str(value or "en").strip().lower()
    if text in _LANGUAGE_NAMES:
        return _LANGUAGE_NAMES[text]
    head = re.split(r"[^a-z]", text, maxsplit=1)[0]
    if head in _LANGUAGE_NAMES:
        return _LANGUAGE_NAMES[head]
    return head[:2] if len(head) >= 2 else "en"


def _normalise(text: str) -> str:
    """Whitespace-insensitive, case-insensitive, for the substring check.

    A model reflows a quote across line breaks constantly; that is formatting,
    not invention, and failing it would drop good skills.
    """
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def trim_description(description: str, limit: int = DESCRIPTION_LIMIT) -> tuple[str, bool]:
    """Keep the head, and the requirements section if one can be found.

    A 200-character snippet produces a 200-character-quality application, so
    truncation is logged rather than done silently.
    """
    if len(description) <= limit:
        return description, False
    head = description[: limit // 2]
    match = SECTION_HEADINGS.search(description, pos=limit // 2)
    tail = description[match.start(): match.start() + limit // 2] if match else description[-limit // 2:]
    return f"{head}\n…\n{tail}", True


def _requirements(rows: Any, description: str, rejected: list[tuple[str, str]]) -> list[Requirement]:
    """Keep only skills whose quote really occurs in the posting."""
    haystack = _normalise(description)
    out: list[Requirement] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        term = str(row.get("term") or "").strip()
        quote = str(row.get("evidence_quote") or "").strip()
        if not term:
            continue
        if not quote or _normalise(quote) not in haystack:
            rejected.append((term, quote))
            log.info("dropped invented requirement %r (quote %r not in posting)", term, quote[:60])
            continue
        out.append(Requirement(term, quote))
    return out


def analyze_job(
    job: dict[str, Any],
    router: TaskRouter,
    *,
    limit: int = DESCRIPTION_LIMIT,
) -> JobAnalysis:
    """Analyse one posting. `job` needs at least `title` and `description`."""
    description = str(job.get("description") or "")
    trimmed, truncated = trim_description(description, limit)
    if truncated:
        log.info("job %s: description truncated to ~%d chars", job.get("id"), limit)

    system = (PROMPTS / "01_job_analysis.md").read_text(encoding="utf-8")
    user = (
        f"TITLE: {job.get('title') or ''}\n"
        f"COMPANY: {job.get('posting_company') or job.get('company_name') or ''}\n"
        f"LOCATION: {job.get('location') or ''}\n\n"
        f"POSTING:\n{trimmed}"
    )
    raw = router.complete_json(
        "job_analysis", system, user, schema=JOB_ANALYSIS_SCHEMA, temperature=0.0
    )

    rejected: list[tuple[str, str]] = []
    family = str(raw.get("role_family") or "OTHER").upper()
    seniority = str(raw.get("seniority") or "UNKNOWN").upper()
    return JobAnalysis(
        role_family=family if family in ROLE_FAMILIES else "OTHER",
        seniority=seniority if seniority in SENIORITIES else "UNKNOWN",
        # Checked against the FULL description, not the trimmed one: a quote
        # from a section that truncation removed is still a real quote.
        required_skills=_requirements(raw.get("required_skills"), description, rejected),
        preferred_skills=_requirements(raw.get("preferred_skills"), description, rejected),
        responsibilities=[str(x) for x in (raw.get("responsibilities") or [])],
        domain=[str(x) for x in (raw.get("domain") or [])],
        keywords=[str(x) for x in (raw.get("keywords") or [])],
        language=_language(raw.get("language")),
        company_signals=[str(x) for x in (raw.get("company_signals") or [])],
        hard_gates=[str(x) for x in (raw.get("hard_gates") or [])],
        rejected=rejected,
        truncated=truncated,
        description_sha=hashlib.sha256(description.encode()).hexdigest()[:16],
    )
