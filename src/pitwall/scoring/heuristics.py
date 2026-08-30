"""Stage 1 scoring: free, deterministic, explainable — §9.1 of PITWALL_PLAN.md.

Runs on 100% of candidates. Pure function, no I/O, so it is trivially
unit-testable and its output can be shown verbatim in the UI.

Typical effect on a real run: 900 candidates in -> ~250 out. That is a ~72% LLM
cost reduction for a few dozen lines of code, and every rejection carries a
human-readable reason.

Rule table (§9.1)::

    company canonical name in title            +30
    company alias in title                     +24
    company name/alias in snippet only         +14
    role canonical title exact in title        +25
    role alias exact in title                  +20
    role family keyword in title               +12
    role/family match in snippet only           +8
    seniority in the wanted list               +10
    role-specific keyword in snippet     +6 each, max +12
    location matches company country            +5
    evidence_count >= 3                         +5
    company negative_keywords hit              -40
    past-tense markers                         -20
    recruiter/agency markers                   -18
    student-only markers with no employer      -12
    URL is /posts/ /pulse/ /company/ /jobs/    reject outright
"""

from __future__ import annotations

import re
from typing import Any

from ..extract.linkedin import NON_PROFILE_PATHS, is_profile_url
from ..extract.normalize import (
    contains_phrase,
    country_matches,
    normalize_company,
    squash,
    strip_accents,
)
from ..models import Company, HeuristicResult, Role

# ─────────────────────────── rule weights (§9.1) ───────────────────────────

W_COMPANY_TITLE = 30.0
W_COMPANY_ALIAS_TITLE = 24.0
W_COMPANY_SNIPPET = 14.0
W_ROLE_TITLE = 25.0
W_ROLE_ALIAS_TITLE = 20.0
W_FAMILY_TITLE = 12.0
W_ROLE_SNIPPET = 8.0
W_SENIORITY = 10.0
W_KEYWORD = 6.0
W_KEYWORD_MAX = 12.0
W_LOCATION = 5.0
W_EVIDENCE = 5.0

P_NEGATIVE_KEYWORD = -40.0
P_PAST_TENSE = -20.0
P_RECRUITER = -18.0
P_STUDENT = -12.0

EVIDENCE_BONUS_AT = 3

#: §1.4 corroboration. 285 of 384 candidates in the real database were surfaced
#: by exactly one query, so multi-query agreement is a strong and currently
#: unused quality signal.
#:
#: Both weights default to **0.0** and the brief is the reason: "do not guess the
#: weight". They are per-extra-source bonuses, and the right value is whatever
#: the re-labelled eval set says it is —
#:     python scripts/eval_scoring.py corroboration --path <labelled worksheet>
#: prints precision/recall/F1 across a grid. Until that has been run against
#: SearXNG SERP shapes (§1.3), leaving these at zero keeps the scoring rubric
#: byte-identical to v1's, which is the guard rail the 260 tests enforce.
W_CORROBORATION_QUERY = 0.0
W_CORROBORATION_TEMPLATE = 0.0
W_CORROBORATION_MAX = 12.0

#: Words that identify a role family inside a job title.
#:
#: The built-in motorsport list, and the fallback for every profile lookup.
#: `config/profiles/motorsport.yaml` is a transcription of it — a test fails if
#: the two drift — so the default path is unchanged by §3.1.
FAMILY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "data_science": (
        "data scientist", "data science", "machine learning", "ml engineer",
        "applied scientist", "data analyst", "analytics", "statistician",
        "ai engineer", "research scientist",
    ),
    "data_engineering": (
        "data engineer", "data platform", "analytics engineer", "data systems",
        "telemetry engineer", "data pipeline", "database", "data architect",
    ),
    "strategy": (
        "strategy", "strategist", "race strategy", "operations research",
        "optimisation", "optimization", "decision science",
    ),
    "performance": (
        "performance engineer", "performance analyst", "vehicle performance",
        "race engineer", "trackside", "aerodynamic", "aero", "cfd",
    ),
    "simulation": (
        "simulation", "simulator", "modelling", "modeling", "driver in loop",
        "driver-in-the-loop", "lap sim", "dil",
    ),
    "vehicle_dynamics": (
        "vehicle dynamics", "tyre model", "tire model", "suspension", "handling",
        "vehicle model",
    ),
    "software": (
        "software engineer", "software developer", "developer", "programmer",
        "tools engineer", "systems engineer", "control systems", "backend",
        "full stack", "embedded",
    ),
    "operations": ("race operations", "team manager", "logistics", "operations engineer"),
    "leadership": ("head of", "chief", "director", "principal", "lead"),
}

#: Default marker vocabularies; overridden by config/negative_keywords.yaml.
DEFAULT_PAST_TENSE = (
    "former", "formerly", "ex-", "ex ", "previously", "until 20", "alum",
    "alumni", "past:", "retired", "prev.", "was at",
)
DEFAULT_RECRUITER = (
    "recruiter", "recruitment", "talent acquisition", "headhunter", "staffing",
    "resourcer", "hr business partner", "executive search", "talent partner",
)
DEFAULT_STUDENT = (
    "student at", "msc candidate", "phd candidate", "undergraduate",
    "aspiring", "seeking opportunities", "looking for opportunities",
    "open to work",
)


def family_keywords(family: str) -> tuple[str, ...]:
    """The active profile's keywords for a family, or the built-in ones (§3.1)."""
    from ..profiles import active

    words = active().keywords_for(family)
    return words or FAMILY_KEYWORDS.get(family, ())


def _first_phrase_hit(haystack: str, phrases: tuple[str, ...] | list[str]) -> str | None:
    for p in phrases:
        if p and contains_phrase(haystack, p):
            return p
    return None


#: Cache of compiled marker patterns — markers are reused across every candidate.
_MARKER_CACHE: dict[str, re.Pattern[str]] = {}


def _marker_pattern(marker: str) -> re.Pattern[str]:
    """Word-boundary regex for a marker.

    `"ex-"` must match `ex-Ferrari` and `ex Ferrari` but never `experience` or
    `Alex`; `"until 20"` must match `until 2023`. A plain substring test on the
    squashed text does neither, which is how a naive `ex-` check ends up
    penalising every candidate whose snippet contains the word "next".
    """
    cached = _MARKER_CACHE.get(marker)
    if cached is not None:
        return cached
    m = marker.strip().lower()
    trailing_hyphen = m.endswith("-")
    core = m.rstrip("-").strip()
    body = re.escape(core).replace(r"\ ", r"\s+")
    if trailing_hyphen:
        pattern = rf"\b{body}[-\s]"          # ex-  ->  "ex-Ferrari" / "ex Ferrari"
    elif core and core[-1].isdigit():
        pattern = rf"\b{body}\d*\b"          # "until 20" -> "until 2023"
    elif core and core[-1].isalnum():
        pattern = rf"\b{body}\b"
    else:
        pattern = rf"\b{body}"
    compiled = re.compile(pattern, re.I)
    _MARKER_CACHE[marker] = compiled
    return compiled


def _raw_substring_hit(haystack: str, needles: tuple[str, ...] | list[str]) -> str | None:
    """Word-boundary marker match on the accent-folded text."""
    if not haystack:
        return None
    text = strip_accents(str(haystack)).lower()
    for n in needles:
        if n and _marker_pattern(str(n)).search(text):
            return n
    return None


def score_candidate(
    candidate: dict[str, Any],
    company: Company | None,
    role: Role | None,
    *,
    serp_title: str | None = None,
    serp_snippet: str | None = None,
    markers: dict[str, Any] | None = None,
    allow_recruiters: bool = False,
    weights: dict[str, float] | None = None,
) -> HeuristicResult:
    """Score one candidate against one company×role pair.

    `serp_title` / `serp_snippet` should be the **raw** SERP strings when
    available — the parsed fields are used as a fallback so the function still
    works on a candidate row alone.

    `weights` overrides the §1.4 corroboration bonuses for one call, which is
    how `scripts/eval_scoring.py corroboration` sweeps them without mutating
    module state.
    """
    markers = markers or {}
    past_markers = tuple(markers.get("past_tense_markers") or DEFAULT_PAST_TENSE)
    recruiter_markers = tuple(markers.get("recruiter_markers") or DEFAULT_RECRUITER)
    student_markers = tuple(markers.get("student_markers") or DEFAULT_STUDENT)
    global_negatives = tuple(markers.get("global") or ())

    reasons: list[str] = []
    url = candidate.get("profile_url") or candidate.get("url") or ""

    # ── hard reject: not a person profile ──
    low_url = url.lower()
    if any(p in low_url for p in NON_PROFILE_PATHS) or (url and not is_profile_url(url)):
        return HeuristicResult(
            score=0.0,
            reasons=[f"URL is not a linkedin.com/in profile: {url[:80]}"],
            hard_reject=True,
        )

    title_text = serp_title if serp_title is not None else _rebuild_title(candidate)
    snippet_text = serp_snippet if serp_snippet is not None else (candidate.get("headline") or "")
    both = f"{title_text} \n {snippet_text}"

    score = 0.0

    # ── company signals ──
    if company is not None:
        # The §9.1 ladder is canonical > alias > snippet-only, so the canonical
        # test must NOT also accept aliases — otherwise every alias hit is paid
        # at the canonical rate and the ladder collapses.
        canonical_hit = contains_phrase(title_text, company.name) or _normalized_hit(
            title_text, [company.name]
        )
        alias_hit = _first_phrase_hit(title_text, company.aliases) or _normalized_hit(
            title_text, company.aliases
        )
        if canonical_hit:
            score += W_COMPANY_TITLE
            reasons.append(f"+{W_COMPANY_TITLE:.0f} company '{company.name}' in the result title")
        elif alias_hit:
            score += W_COMPANY_ALIAS_TITLE
            reasons.append(f"+{W_COMPANY_ALIAS_TITLE:.0f} company alias '{alias_hit}' in the title")
        else:
            snip_hit = _first_phrase_hit(snippet_text, company.all_names) or _normalized_hit(
                snippet_text, company.all_names
            )
            if snip_hit:
                score += W_COMPANY_SNIPPET
                reasons.append(
                    f"+{W_COMPANY_SNIPPET:.0f} company '{snip_hit}' in the snippet only "
                    "(could be a past employer)"
                )

    # ── role signals ──
    if role is not None:
        if contains_phrase(title_text, role.title):
            score += W_ROLE_TITLE
            reasons.append(f"+{W_ROLE_TITLE:.0f} role '{role.title}' exactly in the title")
        else:
            alias = _first_phrase_hit(title_text, role.aliases)
            if alias:
                score += W_ROLE_ALIAS_TITLE
                reasons.append(f"+{W_ROLE_ALIAS_TITLE:.0f} role alias '{alias}' in the title")
            else:
                fam = _first_phrase_hit(title_text, family_keywords(role.family))
                if fam:
                    score += W_FAMILY_TITLE
                    reasons.append(
                        f"+{W_FAMILY_TITLE:.0f} '{role.family}' family keyword '{fam}' in the title"
                    )
                else:
                    snip = _first_phrase_hit(
                        snippet_text, [role.title, *role.aliases, *family_keywords(role.family)]
                    )
                    if snip:
                        score += W_ROLE_SNIPPET
                        reasons.append(f"+{W_ROLE_SNIPPET:.0f} role match '{snip}' in the snippet only")

        # ── seniority ──
        if role.seniority:
            sen_hit = _first_phrase_hit(both, role.seniority)
            if sen_hit:
                score += W_SENIORITY
                reasons.append(f"+{W_SENIORITY:.0f} wanted seniority '{sen_hit}' present")

        # ── role-specific keywords ──
        kw_points = 0.0
        kw_hits: list[str] = []
        for kw in role.keywords:
            if contains_phrase(snippet_text, kw) and kw_points < W_KEYWORD_MAX:
                kw_points += W_KEYWORD
                kw_hits.append(kw)
        if kw_points:
            kw_points = min(kw_points, W_KEYWORD_MAX)
            score += kw_points
            reasons.append(f"+{kw_points:.0f} role keyword(s) in snippet: {', '.join(kw_hits)}")

    # ── location ──
    if company is not None and country_matches(candidate.get("location"), company.country):
        score += W_LOCATION
        reasons.append(f"+{W_LOCATION:.0f} location matches {company.country}")

    # ── corroboration ──
    evidence = int(candidate.get("evidence_count") or 0)
    if evidence >= EVIDENCE_BONUS_AT:
        score += W_EVIDENCE
        reasons.append(f"+{W_EVIDENCE:.0f} surfaced by {evidence} distinct queries")

    # The graded version of the same idea (§1.4): the step above is all-or-nothing
    # at three queries, this scales with how much independent agreement there is.
    # Zero-weighted until the eval set says otherwise.
    templates = int(candidate.get("template_count") or 0)
    w_query = W_CORROBORATION_QUERY if weights is None else weights.get("query", 0.0)
    w_template = W_CORROBORATION_TEMPLATE if weights is None else weights.get("template", 0.0)
    cap = W_CORROBORATION_MAX if weights is None else weights.get("max", W_CORROBORATION_MAX)
    if (w_query or w_template) and (evidence > 1 or templates > 1):
        bonus = min(
            cap,
            w_query * max(0, evidence - 1) + w_template * max(0, templates - 1),
        )
        if bonus:
            score += bonus
            reasons.append(
                f"+{bonus:.1f} corroborated by {evidence} queries "
                f"across {templates} template(s)"
            )

    # ── negatives ──
    negatives = list(company.negative_keywords) if company else []
    negatives += list(global_negatives)
    neg_hit = _first_phrase_hit(both, negatives) or _raw_substring_hit(both, negatives)
    if neg_hit:
        score += P_NEGATIVE_KEYWORD
        reasons.append(f"{P_NEGATIVE_KEYWORD:.0f} negative keyword '{neg_hit}' — likely the wrong organisation")

    past_hit = _raw_substring_hit(both, past_markers)
    if past_hit:
        score += P_PAST_TENSE
        reasons.append(f"{P_PAST_TENSE:.0f} past-tense marker '{past_hit}' — may have left")

    if not allow_recruiters:
        rec_hit = _raw_substring_hit(both, recruiter_markers)
        if rec_hit:
            score += P_RECRUITER
            reasons.append(f"{P_RECRUITER:.0f} recruiter/agency marker '{rec_hit}'")

    stu_hit = _raw_substring_hit(both, student_markers)
    if stu_hit and not (candidate.get("current_company") or "").strip():
        score += P_STUDENT
        reasons.append(f"{P_STUDENT:.0f} student marker '{stu_hit}' with no employer stated")

    clamped = max(0.0, min(100.0, score))
    if not reasons:
        reasons.append("no matching signals found")
    return HeuristicResult(score=clamped, reasons=reasons, hard_reject=False)


def _rebuild_title(candidate: dict[str, Any]) -> str:
    """Reconstruct a SERP-like title from parsed candidate fields."""
    parts = [
        candidate.get("full_name"),
        candidate.get("current_title"),
        candidate.get("current_company"),
    ]
    return " - ".join(p for p in parts if p)


def _normalized_hit(text: str, names: list[str]) -> str | None:
    """Match on the normalized company form so sponsor prefixes do not matter.

    `"Scuderia Ferrari HP"` and `"Ferrari"` both normalize to `ferrari`, so a
    season name in the SERP title still matches the canonical CSV name.
    """
    if not text:
        return None
    haystack = f" {squash(text)} "
    for name in names:
        target = normalize_company(name)
        if target and f" {target} " in haystack:
            return name
    return None


def passes(result: HeuristicResult, threshold: float) -> bool:
    return (not result.hard_reject) and result.score >= threshold
