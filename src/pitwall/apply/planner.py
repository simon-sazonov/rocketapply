"""STAGE 4b — decide what the letter will say, before anything writes it.

Separating planning from writing is what makes a 20B model competitive. Asked to
do both at once it reaches for a template; given a plan it only has to execute.

The planner sees the job, the match and the four to six highest-scoring evidence
blocks — never the whole profile. Less context means less to hallucinate from,
and a much cheaper call.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass, field
from typing import Any

from .analyzer import JobAnalysis
from .llm import TaskRouter
from .matcher import MatchResult
from .models import EvidenceBlock, Profile
from .truth import TruthLayer

log = logging.getLogger(__name__)

__all__ = [
    "NarrativePlan", "select_evidence", "plan_letter", "PLAN_SCHEMA",
    "company_notes_with_source",
]

PROMPTS = pathlib.Path(__file__).parent / "prompts"

#: Four to six in, two or three out. A letter that touches five things says
#: nothing about any of them.
EVIDENCE_SHORTLIST = 6
EVIDENCE_IN_LETTER = 3

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "main_theme": {"type": "string"},
        "secondary_theme": {"type": "string"},
        "evidence_order": {"type": "array", "items": {"type": "string"}},
        "company_angle": {"type": "string"},
        "role_attraction": {"type": "string"},
        "domain_bridge": {"type": "string"},
        "opening_strategy": {
            "type": "string", "enum": ["problem_first", "result_first", "company_first"]
        },
        "avoid": {"type": "array", "items": {"type": "string"}},
        "target_words": {"type": "integer"},
    },
    "required": [
        "main_theme", "secondary_theme", "evidence_order", "company_angle",
        "role_attraction", "domain_bridge", "opening_strategy", "avoid",
        "target_words",
    ],
    "additionalProperties": False,
}


@dataclass
class NarrativePlan:
    main_theme: str = ""
    secondary_theme: str = ""
    evidence_order: list[str] = field(default_factory=list)
    company_angle: str = ""
    #: What about the WORK this posting describes is worth doing — drawn from the
    #: responsibilities it names, not from the company's reputation. Split out
    #: from `company_angle` in v6 because the two need different sources and
    #: conflating them cost the letters their closing paragraph: `company_angle`
    #: needs research Simon has not done (`companies.yaml` is empty), so it came
    #: back blank on five of seven runs and the writer glued `main_theme` onto
    #: the company name instead. This field needs only the posting, which the
    #: planner already has.
    role_attraction: str = ""
    #: One sentence naming the shared CONSTRAINT when the evidence comes from a
    #: far-off domain — ICU sepsis for a driving role, outreach scoring for
    #: defence. Three of the five reviews asked for exactly this sentence and
    #: nothing in the pipeline produced it.
    domain_bridge: str = ""
    opening_strategy: str = "problem_first"
    avoid: list[str] = field(default_factory=list)
    language: str = "en"
    target_words: int = 300
    #: Where COMPANY NOTES came from: "profile" (companies.yaml), "research" (the
    #: machine cache) or "" (neither). NOT a model output — set from the cache
    #: read after the call, and deliberately absent from `PLAN_SCHEMA`, because
    #: asking the model for it would be asking it to invent a provenance.
    company_notes_source: str = ""
    #: The URLs behind those notes, so a sentence in a letter traces to a page.
    #: Also not a model output, for the same reason.
    company_sources: list[str] = field(default_factory=list)


def select_evidence(
    profile: Profile, analysis: JobAnalysis, result: MatchResult
) -> list[EvidenceBlock]:
    """The blocks worth putting in front of the planner, best first."""
    matched_tags = {t.lower() for t in analysis.keywords} | {
        r.requirement.lower() for r in result.matched
    }
    evidence_ids = set(result.evidence_ids)

    def score(block: EvidenceBlock) -> float:
        skills = {s.lower().replace("_", " ") for s in block.skills}
        themes = {t.lower().replace("_", " ") for t in block.themes}
        value = 2.0 * len(skills & matched_tags) + 1.0 * len(themes & matched_tags)
        value += 1.5 * block.strength_weight
        if analysis.family_slug in block.use_for:
            value += 2.0
        # A block whose source bullets the matcher already credited is evidence
        # the posting itself asked for.
        value += 0.5 * len(set(block.source_refs) & evidence_ids)
        return value

    ranked = sorted(profile.evidence, key=lambda b: (-score(b), b.id))
    return ranked[:EVIDENCE_SHORTLIST]


def company_notes_with_source(
    profile: Profile, company: str
) -> tuple[str, str, list[str]]:
    """Two or three sentences about this employer, and where they came from.

    The weakest paragraph in any generated letter is "why this company", because
    the model only has the posting. Fifteen minutes of Simon's reading produces a
    paragraph no model could have written — and where he has not done that
    reading, `apply research` produces a grounded draft of one.

    Returns `(text, origin, source urls)`, where origin is `"profile"` for
    `companies.yaml`, `"research"` for the machine cache, or `""` for neither.
    The caller needs the origin and the URLs for provenance: a claim about an
    employer is the one class of claim `factcheck` cannot judge, because its
    `_FIRST_PERSON` filter skips every third-person sentence.

    Matched on the NORMALISED name, because the first version compared the raw
    strings and a posting that says "Predium Technology GmbH" would never have
    matched an entry called "Predium" — the entry would sit there looking
    correct and do nothing, which is the worst possible outcome for work only a
    human can do. `normalize_company` strips the legal suffix and folds accents,
    so either spelling finds the other. An `aliases` list covers the rest:
    "Meta" for "Facebook", "Alphabet" for "Google".

    A HAND-WRITTEN ENTRY ALWAYS WINS, INCLUDING A BLANK ONE. The match returns
    inside the loop even when the entry yields an empty string, so an entry with
    a name and no `notes` is Simon saying "I looked, there is nothing" and the
    machine cache is never consulted for it. That is deliberate; do not
    "fix" it into a fallthrough, or a deliberate blank becomes impossible to
    record.
    """
    from ..jobsearch.companies import normalize_company

    target = normalize_company(company)
    if not target:
        return "", "", []
    for entry in profile.companies:
        if not isinstance(entry, dict):
            continue
        candidates = {
            normalize_company(str(entry.get("name", ""))),
            normalize_company(str(entry.get("id", "")).replace("co.", "")),
            *(normalize_company(str(a)) for a in (entry.get("aliases") or [])),
        }
        if target in {c for c in candidates if c}:
            text = " ".join(
                str(entry.get(k, "")).strip() for k in ("notes", "angle") if entry.get(k)
            )
            urls = [str(u) for u in (entry.get("source_urls") or [])]
            return text, "profile", urls

    # Nothing hand-written matched. The machine cache is a FALLBACK and only a
    # fallback: this line is reached only after every entry in companies.yaml has
    # failed to match. Read directly rather than through `load_profile`, which
    # memoises on the mtimes of `profile/*.yaml` alone — a file under data/cache/
    # would never invalidate that memo.
    #
    # `research_cache` reaches nothing and imports nothing that can: writing a
    # letter must not pull the search chain, httpx or the task router into its
    # import graph.
    from .research_cache import ResearchCache, cached_notes

    text = cached_notes(company)
    if not text:
        return "", "", []
    try:
        record = ResearchCache().get(company)
    except Exception:  # noqa: BLE001 - the notes are already in hand; URLs are a bonus
        record = None
    return text, "research", list(record.sources) if record else []


def _company_notes(profile: Profile, company: str) -> str:
    """Just the text, for the prompt line."""
    return company_notes_with_source(profile, company)[0]


def plan_letter(
    profile: Profile,
    analysis: JobAnalysis,
    result: MatchResult,
    truth: TruthLayer,
    router: TaskRouter,
    job: dict[str, Any],
    *,
    hint: str = "",
) -> tuple[NarrativePlan, list[EvidenceBlock]]:
    shortlist = select_evidence(profile, analysis, result)
    company = str(job.get("posting_company") or job.get("company_name") or "")
    notes_text, notes_origin, notes_urls = company_notes_with_source(profile, company)

    blocks = "\n\n".join(
        f"[{b.id}] {b.title}\n"
        + "\n".join(f"  fact: {f}" for f in b.facts)
        + (f"\n  hook: {b.one_line_hook.strip()}" if b.one_line_hook else "")
        + (f"\n  story: {b.story.strip()}" if b.story else "")
        for b in shortlist
    )
    from .letter import posting_locations

    user = "\n".join([
        f"ROLE: {job.get('title') or ''}",
        f"COMPANY: {company}",
        f"WHERE: {', '.join(posting_locations(job)[:5]) or '(the posting does not say)'}",
        f"ROLE FAMILY: {analysis.role_family} · SENIORITY: {analysis.seniority}",
        f"POSTING LANGUAGE: {analysis.language}",
        "",
        "RESPONSIBILITIES THE POSTING NAMES:",
        *(f"  - {r}" for r in analysis.responsibilities[:8]),
        "",
        "WHAT THE CANDIDATE CAN EVIDENCE:",
        *(f"  - {m.requirement} ({m.confidence})" for m in result.matched[:12]),
        "",
        "COMPANY NOTES (the candidate's own, may be empty):",
        f"  {notes_text or '(none — do not invent one)'}",
        "",
        "AVOID — never claim, in any phrasing:",
        f"  {', '.join(truth.forbidden_claims[:30])}",
        "",
        "AVOID — never raise these subjects at all, even if the posting does:",
        f"  {', '.join(truth.forbidden_topics)}",
        "",
        "EVIDENCE BLOCKS:",
        blocks,
    ])
    if hint:
        user += f"\n\nTHE CANDIDATE ASKS: {hint}"

    system = (PROMPTS / "04b_narrative_plan.md").read_text(encoding="utf-8")
    raw = router.complete_json(
        "narrative_plan", system, user, schema=PLAN_SCHEMA, temperature=0.2
    )

    known = {b.id for b in shortlist}
    order = [b for b in (raw.get("evidence_order") or []) if b in known][:EVIDENCE_IN_LETTER]
    if not order:
        # The model named nothing usable. The ranking already is a plan.
        order = [b.id for b in shortlist[:2]]
        log.info("planner returned no usable evidence order; fell back to the ranking")

    plan = NarrativePlan(
        main_theme=str(raw.get("main_theme") or ""),
        secondary_theme=str(raw.get("secondary_theme") or ""),
        evidence_order=order,
        company_angle=str(raw.get("company_angle") or ""),
        role_attraction=str(raw.get("role_attraction") or ""),
        domain_bridge=str(raw.get("domain_bridge") or ""),
        opening_strategy=str(raw.get("opening_strategy") or "problem_first"),
        # The truth layer's list is the authority; the model's is a restatement.
        avoid=sorted({*(str(a) for a in raw.get("avoid") or []), *truth.forbidden_claims[:20]}),
        language=_letter_language(profile, analysis),
        target_words=max(240, min(int(raw.get("target_words") or 300), 340)),
        company_notes_source=notes_origin,
        company_sources=notes_urls,
    )
    chosen = [b for bid in plan.evidence_order if (b := profile.evidence_block(bid))]
    return plan, chosen


def _letter_language(profile: Profile, analysis: JobAnalysis) -> str:
    """Write in the posting's language only where the candidate could sustain an
    interview in it. Never machine-translate a letter into a language he cannot
    speak — the interview would follow in that language."""
    if analysis.language == "en":
        return "en"
    levels = {
        str(item.get("name", "")).lower(): str(item.get("level", "")).upper()
        for item in profile.personal.languages or []
    } if hasattr(profile.personal, "languages") else {}
    names = {"de": "german", "pt": "portuguese", "fr": "french", "it": "italian",
             "es": "spanish", "uk": "ukrainian", "ru": "russian"}
    level = levels.get(names.get(analysis.language, ""), "")
    if level in {"C1", "C2", "NATIVE"}:
        return analysis.language
    log.info("posting is in %r but the profile does not claim it; writing in English",
             analysis.language)
    return "en"
