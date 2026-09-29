"""STAGE 2 — requirement to evidence, deterministically.

No LLM in the common case. A requirement is normalised to a canonical term, the
term is looked up in the profile's skills, and the skill carries the bullet ids
that prove it. Tag intersection is explainable, debuggable and free; embeddings
would buy nothing over sixty records and would make a wrong match impossible to
explain.

The output feeds two things. It builds the `JobSignal` the selector scores
against, and — more importantly — its `missing` list is what the truth layer
turns into forbidden claims for this run.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .analyzer import JobAnalysis, Requirement
from .models import Profile, Skill
from .selector import JobSignal

log = logging.getLogger(__name__)

__all__ = ["Matched", "Partial", "Missing", "MatchResult", "match", "canonical_tags"]

#: Below this, the honest advice is "this is a stretch, and here is what is
#: missing" rather than "generate an application anyway".
STRETCH_THRESHOLD = 0.45


@dataclass(frozen=True)
class Matched:
    requirement: str
    skill_id: str
    skill_name: str
    confidence: str
    evidence_ids: list[str]


@dataclass(frozen=True)
class Partial:
    requirement: str
    skill_id: str
    skill_name: str
    #: The exact wording the human approved. A model may not paraphrase it.
    approved_phrasing: str


@dataclass(frozen=True)
class Missing:
    requirement: str
    criticality: str  # required | preferred
    #: Set when the profile explicitly records having none of it — a stronger
    #: signal than "no match found", and the reason it becomes forbidden.
    declared_gap: bool = False


@dataclass
class MatchResult:
    matched: list[Matched] = field(default_factory=list)
    partial: list[Partial] = field(default_factory=list)
    missing: list[Missing] = field(default_factory=list)
    #: Terms the taxonomy has never heard of. Recorded rather than dropped
    #: silently, so the vocabulary can be grown deliberately.
    unmatched_terms: list[str] = field(default_factory=list)
    #: Matched on bullet tags alone, with no `skills.yaml` record behind them.
    #: Real evidence, but it cannot print in a skills row — so this list is a
    #: standing suggestion of records worth adding to the profile.
    tag_only: list[str] = field(default_factory=list)
    coverage_required: float = 0.0
    coverage_preferred: float = 0.0

    @property
    def overall(self) -> float:
        return round(0.7 * self.coverage_required + 0.3 * self.coverage_preferred, 3)

    @property
    def is_stretch(self) -> bool:
        return self.overall < STRETCH_THRESHOLD

    @property
    def missing_terms(self) -> list[str]:
        """What the truth layer forbids for this run."""
        return [m.requirement for m in self.missing]

    @property
    def evidence_ids(self) -> list[str]:
        seen: list[str] = []
        for row in self.matched:
            for ref in row.evidence_ids:
                if ref not in seen:
                    seen.append(ref)
        return seen


def _slug(term: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", term.lower()).strip("_")


def _index(profile: Profile) -> dict[str, Skill]:
    """Every string that should resolve to a skill: its name, its aliases, and
    the underscored forms that match profile tags."""
    index: dict[str, Skill] = {}
    for skill in profile.skills:
        for term in skill.match_terms:
            index.setdefault(term, skill)
            index.setdefault(_slug(term), skill)
    return index


def canonical_tags(profile: Profile, terms: list[str]) -> set[str]:
    """Job terms mapped onto the profile's own tag vocabulary. Direct lookup only.

    NO EXPANSION THROUGH EVIDENCE, and that is a deliberate correction. Taking a
    skill's evidence-bullet tags as a proxy for the skill turns a tool into a
    topic: PyTorch's two evidence bullets are NLP projects, so a posting asking
    for PyTorch would have boosted every NLP project on the shelf. Tightening it
    to the tags common to all evidence is no better — scikit-learn and Optuna
    then expand to nothing at all, because their uses share no tag.

    A term that resolves to no canonical tag therefore contributes nothing to
    the SIGNAL. It still counts in the MATCH, which asks a different question and
    resolves through the skill index. And `pytorch` is not a canonical tag, so no
    bullet carries it — meaning there is genuinely nothing for it to match, and
    inventing an association would only mis-steer the selector.

    Unresolved terms are reported in `unmatched_terms`, which is how the
    vocabulary gets grown deliberately rather than by accident.
    """
    vocabulary = {
        t for group in (profile.taxonomy.get("canonical") or {}).values() for t in (group or [])
    }
    aliases = profile.taxonomy.get("aliases") or {}

    out: set[str] = set()
    for term in terms:
        slug = aliases.get(_slug(term), _slug(term))
        if slug in vocabulary:
            out.add(slug)
        elif (contained := _contained_tag(term, vocabulary)) is not None:
            out.add(contained)
    return out


@dataclass(frozen=True)
class _TagEvidence:
    """Bullet-level evidence for a requirement with no `skills.yaml` record."""

    tag: str
    bullet_ids: list[str]

    @property
    def confidence(self) -> str:
        # The same ladder skills.yaml uses: two independent pieces of work is
        # `high`, one is `medium`.
        return "high" if len(self.bullet_ids) >= 2 else "medium"


def _contained_tag(term: str, vocabulary: set[str]) -> str | None:
    """The canonical tag a requirement names, even inside a longer phrase.

    Models return responsibility phrases as skills no matter how the prompt is
    worded — "model evaluation and developing metrics for simulator realism"
    came back from a real Waymo posting. The exact slug matches nothing, so the
    requirement reads as a gap and gets FORBIDDEN, which would gag
    `model_evaluation`: something Simon has six bullets of evidence for.

    So a tag counts if its words appear consecutively in the requirement. The
    longest match wins, because `model_evaluation` is a better reading of that
    phrase than `evaluation` would be. Consecutive rather than merely present,
    to keep "data quality and engineering standards" from matching
    `data_engineering`.
    """
    exact = _slug(term)
    if exact in vocabulary:
        return exact
    words = exact.split("_")
    best: str | None = None
    for tag in vocabulary:
        parts = tag.split("_")
        # A ONE-WORD tag only counts on an exact match, handled above. Finding
        # `software` inside "backend software for robotics" is not evidence of
        # anything: every engineering posting contains such a word, and matching
        # on it inflates coverage with something the candidate cannot defend.
        # A multi-word tag inside a phrase — `model_evaluation`, `data_quality`
        # — is specific enough to mean what it says.
        if len(parts) < 2 or len(parts) > len(words):
            continue
        for i in range(len(words) - len(parts) + 1):
            if words[i : i + len(parts)] == parts:
                if best is None or len(tag) > len(best):
                    best = tag
                break
    return best


def _tag_evidence(profile: Profile, term: str, vocabulary: set[str]) -> _TagEvidence | None:
    """Fall back to the tags on the bullets themselves.

    WHY THIS EXISTS. "Computer Vision" has no record in `skills.yaml`, but
    `computer_vision` is a canonical tag carried by a real project bullet — the
    painter-classification CNN. Without this fallback the matcher calls the
    requirement MISSING, and the truth layer then turns it into a FORBIDDEN
    CLAIM, so a posting asking for computer vision would silently gag a genuine
    strength. That is the opposite of what the forbidden list is for.

    Bullet tags are legitimate evidence: they are validated against the taxonomy,
    and `evidence:` on a skill points at these same bullets.
    """
    slug = _contained_tag(term, vocabulary)
    if slug is None:
        return None
    ids = [
        bullet.id
        for bullet in (
            *profile.experience_bullets,
            *(b for project in profile.projects for b in project.bullets),
        )
        if slug in bullet.tags
    ]
    return _TagEvidence(slug, ids) if ids else None


def _resolve(requirement: Requirement, index: dict[str, Skill]) -> Skill | None:
    term = requirement.term.strip()
    return index.get(term.lower()) or index.get(_slug(term))


def match(analysis: JobAnalysis, profile: Profile) -> MatchResult:
    """Score the posting's stated requirements against what the profile proves."""
    index = _index(profile)
    vocabulary = {
        t for group in (profile.taxonomy.get("canonical") or {}).values() for t in (group or [])
    }
    result = MatchResult()

    def consider(requirement: Requirement, criticality: str) -> float:
        """Returns the credit earned: 1.0 matched, 0.5 partial, 0.0 missing."""
        skill = _resolve(requirement, index)
        if skill is None:
            # No skill record — but the bullets may still prove it.
            evidence = _tag_evidence(profile, requirement.term, vocabulary)
            if evidence is not None:
                result.matched.append(
                    Matched(requirement.term, f"tag.{evidence.tag}", requirement.term,
                            evidence.confidence, evidence.bullet_ids)
                )
                result.tag_only.append(requirement.term)
                return 1.0
            result.unmatched_terms.append(requirement.term)
            result.missing.append(Missing(requirement.term, criticality))
            return 0.0
        if skill.confidence in {"high", "medium"}:
            result.matched.append(
                Matched(requirement.term, skill.id, skill.name, skill.confidence,
                        list(skill.evidence))
            )
            return 1.0
        if skill.confidence == "low" and skill.low_phrasing:
            # Only ever stated in the human's exact words.
            result.partial.append(
                Partial(requirement.term, skill.id, skill.name, skill.low_phrasing)
            )
            return 0.5
        # `confidence: none` is a DECLARED gap, not an absence of information.
        result.missing.append(
            Missing(requirement.term, criticality, declared_gap=skill.confidence == "none")
        )
        return 0.0

    required_credit = [consider(r, "required") for r in analysis.required_skills]
    preferred_credit = [consider(r, "preferred") for r in analysis.preferred_skills]

    result.coverage_required = (
        round(sum(required_credit) / len(required_credit), 3) if required_credit else 0.0
    )
    result.coverage_preferred = (
        round(sum(preferred_credit) / len(preferred_credit), 3) if preferred_credit else 0.0
    )
    return result


def build_signal(analysis: JobAnalysis, profile: Profile, job: dict | None = None) -> JobSignal:
    """What the selector scores against, built from the posting's own words."""
    return JobSignal(
        required=canonical_tags(profile, [r.term for r in analysis.required_skills]),
        preferred=canonical_tags(profile, [r.term for r in analysis.preferred_skills]),
        keywords=canonical_tags(profile, [*analysis.keywords, *analysis.domain]),
        role_family=analysis.family_slug,
        title=str((job or {}).get("title") or ""),
        company=str((job or {}).get("posting_company") or (job or {}).get("company_name") or ""),
    )


#: Generic capability vocabulary. A requirement built only from these words
#: describes an activity, not a product — and activities are where the profile's
#: own evidence lives, so gagging one is far more likely to suppress true work
#: than to prevent a false claim.
_GENERIC = {
    "model", "models", "modelling", "modeling", "development", "developing",
    "experience", "previous", "prior", "years", "strong", "solid", "good",
    "excellent", "knowledge", "understanding", "skills", "ability", "working",
    "hands", "on", "with", "and", "or", "of", "in", "the", "a", "an", "for",
    "background", "familiarity", "proficiency", "expertise", "track", "record",
    "software", "engineering", "technical", "analytical", "problem", "solving",
    "communication", "team", "teamwork", "collaboration", "internship",
    "graduate", "degree", "research", "data", "analysis", "design", "building",
    "build", "writing", "written", "tools", "systems", "methods", "techniques",
}


def looks_like_a_product(term: str) -> bool:
    """Is this a named technology, or a description of an activity?

    ONLY NAMED THINGS ARE WORTH FORBIDDING. Turning a missing requirement into a
    forbidden claim is a strong, run-wide action, and it earns its place against
    "AWS", "Kubernetes", "Snowflake" — concrete, checkable, and damaging to claim
    falsely. Applied to "model development" or "previous internship experience"
    it would gag things Simon genuinely does, which is the opposite of the point.

    The test is shape, not meaning: a capitalised or all-caps token that is not
    ordinary capability vocabulary. Terms that fail it are still REPORTED as
    gaps and still count against coverage — they simply do not silence a subject.
    """
    tokens = [t for t in re.split(r"[\s/,()]+", term.strip()) if t]
    if not tokens:
        return False
    for token in tokens:
        bare = token.strip(".-+#")
        if not bare or bare.lower() in _GENERIC:
            continue
        if bare.isupper() and len(bare) >= 2:
            return True
        if bare[:1].isupper() and bare.lower() not in _GENERIC:
            return True
    return False


def verify_gaps(profile: Profile, terms: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Separate real gaps from terms the profile can actually prove.

    WHY THIS GUARD EXISTS. `job_matches.missing_skills` is the job search's own
    opinion, computed against `config/my_skills.yaml` — a narrower inventory than
    `profile/`. On a real posting it reports `simulation` missing, while the
    profile carries Monte Carlo path simulation at Ophis, the pre-trade engine
    and the thesis hazard model. Folding that straight into `forbidden_claims`
    would gag one of the strongest things Simon has.

    So nothing is forbidden without checking the profile first. Returns
    `(real_gaps, contradicted)`, where each contradiction names the evidence, so
    the disagreement is visible rather than silently resolved either way.
    """
    index = _index(profile)
    vocabulary = {
        t for group in (profile.taxonomy.get("canonical") or {}).values() for t in (group or [])
    }
    gaps: list[str] = []
    contradicted: list[tuple[str, str]] = []

    for term in terms:
        skill = index.get(term.lower()) or index.get(_slug(term))
        if skill is not None and skill.confidence in {"high", "medium"}:
            contradicted.append((term, f"{skill.id} at confidence {skill.confidence}"))
            continue
        if skill is not None and skill.confidence == "none":
            # A DECLARED gap. The strongest possible signal — keep it.
            gaps.append(term)
            continue
        evidence = _tag_evidence(profile, term, vocabulary)
        if evidence is not None:
            contradicted.append(
                (term, f"tagged on {len(evidence.bullet_ids)} bullet(s): "
                       f"{', '.join(evidence.bullet_ids[:3])}")
            )
            continue
        if not looks_like_a_product(term):
            contradicted.append(
                (term, "describes an activity rather than a named technology — "
                       "reported as a gap, but not worth silencing")
            )
            continue
        gaps.append(term)
    return gaps, contradicted
