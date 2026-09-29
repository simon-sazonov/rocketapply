"""SLOT A and SLOT B — which three projects print, and what fills each skills row.

Deterministic. A model may express a preference, but it is one term in a score,
and the solver still enforces the constraint — so the model can never produce an
infeasible page. If its preferred trio does not fit, the best feasible trio wins
and the override is recorded in provenance.

THE KNAPSACK IS SOLVED EXACTLY. With 20 selectable projects there are
C(20,3) = 1,140 combinations. Enumerate all of them. No greedy approximation, no
heuristic, and emphatically no model arithmetic.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .layout import Frame
from .models import Bullet, Profile, Project

__all__ = [
    "JobSignal", "ProjectSlot", "CvConfig", "select_projects", "pack_row",
    "build_config", "choose_variants",
]

#: §6 of the spec. Explainable, debuggable and free — which is why v1 uses tag
#: intersection rather than embeddings.
W_REQUIRED = 3.0
W_PREFERRED = 1.5
W_KEYWORD = 1.0
W_STRENGTH = 0.8
W_USE_FOR = 1.0
W_NEVER_USE_FOR = -5.0
W_DIVERSITY = 0.4
W_ADVISORY = 1.2      # what an LLM preference is worth, at most
PENALTY_FAMILY = -2.0

ROLE_FAMILIES = (
    "data_science", "machine_learning", "data_engineering",
    "analytics", "research", "quantitative", "software",
)


@dataclass
class JobSignal:
    """The job, reduced to what the selector actually scores against.

    Built by the matcher from a posting; constructible by hand for tests and for
    the `preview` command, which is why it carries no LLM dependency.
    """

    required: set[str] = field(default_factory=set)
    preferred: set[str] = field(default_factory=set)
    keywords: set[str] = field(default_factory=set)
    role_family: str = "data_science"
    title: str = ""
    company: str = ""

    @property
    def all_tags(self) -> set[str]:
        return self.required | self.preferred | self.keywords


@dataclass
class ProjectSlot:
    id: str
    bullets: list[str]
    score: float = 0.0
    cost: int = 0


@dataclass
class CvConfig:
    """What the model emits, after the solver has had the last word.

    Note what is absent compared with a general-purpose resume generator: no
    section order, no experience selection, no education, no summary. The model
    cannot touch what it cannot address.
    """

    projects: list[ProjectSlot] = field(default_factory=list)
    skills: dict[str, list[str]] = field(default_factory=dict)
    experience_variants: dict[str, str | None] = field(default_factory=dict)
    rationale: dict[str, str] = field(default_factory=dict)
    #: Where the solver disagreed with an advisory preference, and why.
    overrides: list[str] = field(default_factory=list)
    total_project_lines: int = 0

    @property
    def used_ids(self) -> list[str]:
        ids: list[str] = []
        for slot in self.projects:
            ids.append(slot.id)
            ids.extend(slot.bullets)
        ids.extend(v for v in self.experience_variants.values() if v)
        return ids


# ─────────────────────────────── scoring ────────────────────────────────


def bullet_score(bullet: Bullet, signal: JobSignal) -> float:
    tags = set(bullet.tags)
    score = (
        W_REQUIRED * len(tags & signal.required)
        + W_PREFERRED * len(tags & signal.preferred)
        + W_KEYWORD * len(tags & signal.keywords)
        + W_STRENGTH * bullet.strength_weight
    )
    if signal.role_family in bullet.never_use_for:
        score += W_NEVER_USE_FOR
    elif signal.role_family in bullet.use_for:
        score += W_USE_FOR
    return score


def project_score(project: Project, signal: JobSignal, advisory: dict[str, float] | None = None) -> float:
    score = sum(bullet_score(b, signal) for b in project.bullets)
    themes = set(project.themes)
    score += W_KEYWORD * len(themes & signal.all_tags)
    score += W_STRENGTH * project.strength_weight
    if signal.role_family in project.never_use_for:
        score += W_NEVER_USE_FOR
    elif signal.role_family in project.use_for:
        score += W_USE_FOR
    if advisory:
        # Advisory only. Bounded, so a confident model cannot outvote the tags.
        score += W_ADVISORY * max(0.0, min(1.0, advisory.get(project.id, 0.0)))
    return score


def diversity_bonus(combo: tuple[Project, ...]) -> float:
    """Reward distinct themes, so three near-identical pipeline projects cannot
    crowd out a modelling one."""
    return float(len({t for p in combo for t in p.themes}))


def selectable(profile: Profile) -> list[Project]:
    """Projects the selector may choose from.

    Excludes any whose work is already described in the fixed experience block.
    Since all seven experience bullets print on every application, a project
    conflicting with one of them can never be printed — showing one body of work
    twice on a single page is worse than showing one fewer project.
    """
    printed = {b.id for b in profile.experience_bullets}
    return [p for p in profile.projects if not (set(p.conflicts_with) & printed)]


def choose_bullets(project: Project, signal: JobSignal, frame: Frame, quota: int) -> list[Bullet]:
    """The `bullets_per_project` best bullets whose cost fits the project's quota.

    Every project in the current library declares exactly two, so this is a
    no-op today. It is written generally so that a project gaining a third
    bullet tomorrow does not need a code change.
    """
    want = frame.bullets_per_project
    if len(project.bullets) <= want:
        return list(project.bullets)
    style = frame.project_bullet_style
    best: tuple[float, list[Bullet]] | None = None
    for combo in itertools.combinations(project.bullets, want):
        cost = 1 + sum(frame.line_cost(b.text, style) for b in combo)
        if cost > quota:
            continue
        score = sum(bullet_score(b, signal) for b in combo)
        if best is None or score > best[0]:
            best = (score, list(combo))
    return best[1] if best else list(project.bullets[:want])


class LayoutInfeasible(RuntimeError):
    """No legal combination fits the slot. A profile problem, not a run problem."""


def select_projects(
    profile: Profile,
    frame: Frame,
    signal: JobSignal,
    *,
    advisory: dict[str, float] | None = None,
) -> tuple[list[ProjectSlot], list[str]]:
    """The exact solution to a tiny 0/1 knapsack with a cardinality constraint.

    Returns the chosen slots ordered by relevance — the strongest match reads
    first — and any overrides worth recording.
    """
    pool = selectable(profile)
    k, quota = frame.project_count, frame.project_budget
    if len(pool) < k:
        raise LayoutInfeasible(f"only {len(pool)} selectable projects, need {k}")

    style = frame.project_bullet_style
    per_project_quota = quota - (k - 1) * 3  # leave room for two well-formed neighbours
    chosen_bullets: dict[str, list[Bullet]] = {
        p.id: choose_bullets(p, signal, frame, max(per_project_quota, 5)) for p in pool
    }
    costs = {
        p.id: 1 + sum(frame.line_cost(b.text, style) for b in chosen_bullets[p.id]) for p in pool
    }
    scores = {p.id: project_score(p, signal, advisory) for p in pool}

    best: tuple[float, tuple[Project, ...], int] | None = None
    for combo in itertools.combinations(pool, k):
        cost = sum(costs[p.id] for p in combo)
        if cost > quota:
            continue
        score = sum(scores[p.id] for p in combo) + W_DIVERSITY * diversity_bonus(combo)
        score += PENALTY_FAMILY * sum(1 for p in combo if signal.role_family in p.never_use_for)
        if best is None or score > best[0]:
            best = (score, combo, cost)

    if best is None:
        raise LayoutInfeasible(
            f"no {k}-project combination fits the {quota}-line slot "
            f"(cheapest trio costs {sum(sorted(costs.values())[:k])})"
        )

    _, combo, _ = best
    ordered = sorted(combo, key=lambda p: (-scores[p.id], p.id))
    slots = [
        ProjectSlot(
            id=p.id,
            bullets=[b.id for b in chosen_bullets[p.id]],
            score=round(scores[p.id], 2),
            cost=costs[p.id],
        )
        for p in ordered
    ]

    overrides: list[str] = []
    if advisory:
        wanted = {pid for pid, w in sorted(advisory.items(), key=lambda kv: -kv[1])[:k] if w > 0}
        got = {s.id for s in slots}
        if wanted and wanted != got:
            overrides.append(
                f"solver chose {sorted(got)} over the model's preferred "
                f"{sorted(wanted)} — the preference is advisory and the line budget is not"
            )
    return slots, overrides


# ──────────────────────────── SLOT B: skill rows ─────────────────────────


def row_line_budget(frame: Frame, row_id: str) -> int:
    for row in frame.skill_rows:
        if row.get("id") == row_id:
            return int(row.get("lines", 2))
    return 2


def pack_row(profile: Profile, frame: Frame, row_id: str, signal: JobSignal) -> list[str]:
    """Pack one row, most job-relevant first.

    TWO CONSTRAINTS, AND THE LINE ONE BINDS. The character budget in
    `layout.yaml` is a useful sanity limit but a bad proxy for the page: 193
    characters of "Data Quality & Validation, Decision Threshold Tuning" renders
    as THREE lines in a 455.5pt column, not the two the row declares — which
    silently grows the page by a line. Measured on an ML-engineering posting,
    where those long method names outrank the short ones.

    So an item is admitted only if the row still measures within its declared
    line count, which makes the row's height an invariant by construction rather
    than something to hope for. The character budget stays as the secondary cap,
    because it is what `validate.py` enforces and what keeps the row visually
    even.

    THE ROW IS FILLED, AND THAT IS ON PURPOSE. It is tempting to admit only
    what the posting asked for, so that a computer-vision CV stops printing
    Bizagi and Flutter at the end of its tools row. That was tried on 28 Sep
    2026 and reverted, for two reasons worth recording so it is not tried again:

    * **The page height stops being constant.** `test_body_line_count_is_invariant`
      caught it immediately — 47 body lines for one posting and 46 for another.
      Filling to the declared LINE COUNT instead does not save it either: a
      one-line row like `row.programming` reaches its limit on the first item,
      so the row collapsed to "Python, SQL, T-SQL".
    * **Filling the row and cutting the tail are the same decision.** A full
      `row.tools` holds ~18 of its 46 candidates and a full `row.programming`
      ~12 of 13. There is no setting that keeps the row visually full and drops
      the irrelevant tail, because the tail is what fills it.

    An unfilled row also costs ATS keyword coverage, which is a real channel for
    these applications and cannot be recovered elsewhere.

    So ORDER is the lever, not admission: `relevance` puts what the posting
    asked for at the front, where a human reads. Whether a tool may appear at
    all is a curation decision belonging to `printable:` in `skills.yaml`, not
    to this function.
    """
    fixed = profile.skill_rows.fixed_text(row_id)
    if fixed is not None:
        # Row 4. Copied verbatim, never re-packed — spoken languages do not vary
        # by job, and this row has one character of slack.
        return [fixed]

    char_limit = frame.row_limit(row_id)
    line_limit = row_line_budget(frame, row_id)
    pinned_names = profile.skill_rows.pinned(row_id)
    candidates = profile.skills_in_row(row_id)
    by_name = {s.name: s for s in candidates}

    def relevance(skill) -> int:
        terms = {t.replace(" ", "_") for t in skill.match_terms} | skill.match_terms
        hits = len(terms & signal.required) * 3 + len(terms & signal.preferred) * 2
        return hits + len(terms & signal.keywords)

    def fits(names: list[str]) -> bool:
        joined = ", ".join(names)
        return len(joined) <= char_limit and frame.line_cost(joined) <= line_limit

    chosen = [n for n in pinned_names if n in by_name]
    ranked = sorted(
        (s for s in candidates if s.name not in chosen),
        key=lambda s: (-relevance(s), -s.confidence_weight, s.name),
    )
    for skill in ranked:
        if fits([*chosen, skill.name]):
            chosen.append(skill.name)

    # A row must never render empty. Pinned items alone are checked against the
    # character budget at profile-validation time.
    if len(chosen) < 3:
        chosen = [s.name for s in candidates][:3]
    return chosen


def choose_variants(profile: Profile, signal: JobSignal) -> dict[str, str | None]:
    """The pre-approved rewording written for THIS role family, per bullet.

    Deterministic and deliberately dull: a variant is chosen only when it names
    the signal's family in its own `use_for`. There is no prose scoring, because
    the registers these variants are written in — "ML-engineering",
    "quant / market-microstructure", "MLOps" — are not recoverable from the
    words with any confidence, and a wrong pick is a worse bullet than the
    canonical one it replaced.

    A bullet with no matching variant keeps its canonical text, which is what
    every application printed before this existed: `pipeline.generate_application`
    called `build_config` without the argument at all, so nine hand-written
    variants sat in `experience.yaml` being read by nothing.

    The caller still passes the result through `_validated_variants`, so a
    variant that is not line-neutral is refused here as it always was.
    """
    out: dict[str, str | None] = {}
    for bullet in profile.experience_bullets:
        match = next(
            (v for v in bullet.variants if signal.role_family in (v.use_for or [])),
            None,
        )
        out[bullet.id] = match.id if match else None
    return out


def build_config(
    profile: Profile,
    frame: Frame,
    signal: JobSignal,
    *,
    advisory: dict[str, float] | None = None,
    variants: dict[str, str | None] | None = None,
) -> CvConfig:
    """The whole deterministic slot fill. No model involvement at all.

    This is also the fallback the LLM path degrades to, which means a Groq outage
    produces a good CV rather than no CV.
    """
    slots, overrides = select_projects(profile, frame, signal, advisory=advisory)
    variant_overrides: list[str] = []
    config = CvConfig(
        projects=slots,
        skills={row["id"]: pack_row(profile, frame, row["id"], signal) for row in frame.skill_rows},
        experience_variants=_validated_variants(profile, frame, variants or {}, variant_overrides),
        overrides=[*overrides, *variant_overrides],
        total_project_lines=sum(slot.cost for slot in slots),
    )
    for slot in slots:
        project = profile.project(slot.id)
        if project:
            shared = sorted(project.tag_set & signal.all_tags)
            config.rationale[slot.id] = (
                f"matches {', '.join(shared[:4])}" if shared else "highest remaining relevance"
            )
    return config


def _validated_variants(
    profile: Profile, frame: Frame, requested: dict[str, str | None], overrides: list[str]
) -> dict[str, str | None]:
    """Accept a variant only if it belongs to that exact bullet AND is
    line-neutral with it. Anything else falls back to the canonical text.

    Line-neutrality is already checked at profile-validation time, so a failure
    here means the frame and the profile have drifted — worth saying out loud.
    """
    out: dict[str, str | None] = {}
    for bullet in profile.experience_bullets:
        wanted = requested.get(bullet.id)
        if not wanted:
            out[bullet.id] = None
            continue
        variant = bullet.variant(wanted)
        if variant is None:
            overrides.append(f"{bullet.id}: {wanted!r} is not a declared variant — used the canonical text")
            out[bullet.id] = None
            continue
        if frame.line_cost(variant.text) != frame.line_cost(bullet.text):
            overrides.append(f"{variant.id}: not line-neutral with {bullet.id} — used the canonical text")
            out[bullet.id] = None
            continue
        out[bullet.id] = variant.id
    return out
