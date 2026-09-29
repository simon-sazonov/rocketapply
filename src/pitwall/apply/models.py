"""Typed views over `profile/`.

`extra="allow"` throughout, deliberately. The profile carries a great deal of
documentation alongside its data — `role_note`, `readme_note`, `years_basis`,
attribution caveats — and a model that dropped those would quietly discard the
reasoning behind the record. Shape is enforced by `validate.py`; these models
exist for ergonomics and type safety, not as a second validator.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Variant", "Bullet", "ExperienceEntry", "Project", "Skill", "Metric",
    "EvidenceBlock", "SkillRows", "Personal", "Style", "Forbidden", "Profile",
]

STRENGTH_WEIGHT = {"strong": 1.0, "medium": 0.6, "supporting": 0.3}
CONFIDENCE_WEIGHT = {"high": 3, "medium": 2, "low": 1, "none": 0}


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Variant(_Base):
    """A rephrasing the human pre-approved. The model may select one; it may
    never write one. Line-neutrality with the canonical bullet is checked at
    profile-validation time, so an unusable variant cannot reach the renderer."""

    id: str
    text: str
    #: Role families this wording was written FOR. Empty means "never chosen
    #: automatically" — several variants are plain alternates with no register
    #: of their own, and guessing at one from the prose would pick wrong.
    #: Until 28 Sep 2026 the register lived only in a YAML comment, so the
    #: selector could not read it and every CV printed the canonical text.
    use_for: list[str] = Field(default_factory=list)


class Bullet(_Base):
    id: str
    text: str
    tags: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    strength: str = "medium"
    use_for: list[str] = Field(default_factory=list)
    never_use_for: list[str] = Field(default_factory=list)
    variants: list[Variant] = Field(default_factory=list)
    #: Measured off the reference PDF, not computed. Only experience bullets
    #: declare it; project bullets are all one line by construction.
    rendered_lines: int | None = None
    #: Experience bullets naming the project that describes the same work.
    overlaps_with: list[str] = Field(default_factory=list)

    def variant(self, variant_id: str) -> Variant | None:
        return next((v for v in self.variants if v.id == variant_id), None)

    @property
    def strength_weight(self) -> float:
        return STRENGTH_WEIGHT.get(self.strength, 0.6)


class ExperienceEntry(_Base):
    """Fixed. All three entries and all seven bullets print on every application,
    in this order. Only the wording may swap, and only for a pre-approved,
    line-neutral variant."""

    id: str
    org: str
    role: str
    bullets: list[Bullet] = Field(default_factory=list)
    employment_type: str | None = None
    location: str | None = None
    start: str | None = None
    end: str | None = None
    context: str | None = None


class Project(_Base):
    """The variable slot. Exactly three of these print, chosen from the library."""

    id: str
    name: str
    bullets: list[Bullet] = Field(default_factory=list)
    link_label: str | None = None
    url: str = ""
    themes: list[str] = Field(default_factory=list)
    use_for: list[str] = Field(default_factory=list)
    never_use_for: list[str] = Field(default_factory=list)
    strength: str = "medium"
    context_type: str | None = None
    summary: str | None = None
    stack: list[str] = Field(default_factory=list)
    period: str | None = None
    #: ok | stub | none. A linked repository WILL be opened, and a stub reads as
    #: abandoned — so the renderer drops the link label rather than the project.
    readme_state: str | None = None
    #: Experience bullet ids describing the same work. Printing both would show
    #: one body of work twice on one page.
    conflicts_with: list[str] = Field(default_factory=list)

    @property
    def show_link(self) -> bool:
        """Whether to print the `GitHub` label. False for a stub or absent README:
        the bullets stand on their own, and it is the link that would hurt."""
        return bool(self.link_label) and self.readme_state not in {"stub", "none"}

    @property
    def tag_set(self) -> set[str]:
        return {t for b in self.bullets for t in b.tags}

    @property
    def strength_weight(self) -> float:
        return STRENGTH_WEIGHT.get(self.strength, 0.6)


class Skill(_Base):
    id: str
    name: str
    row: str | None = None
    category: str | None = None
    confidence: str = "none"
    evidence: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    years: int | None = None
    pin: bool = False
    printable: bool = False
    low_phrasing: str | None = None
    note: str | None = None
    #: The work is real but sits in a repository Simon does not publish. It
    #: satisfies the evidence requirement; what it cannot do is supply a project
    #: name to cite, so a letter must never invent one.
    evidence_private: bool = False
    #: `skills_row_only` — may print as one token in a CV row, but never as an
    #: elaborated claim in letter prose. The two are different sizes of claim.
    letter_policy: str | None = None
    user_override: str | None = None

    @property
    def match_terms(self) -> set[str]:
        return {self.name.lower(), *(a.lower() for a in self.aliases)}

    @property
    def letter_claimable(self) -> bool:
        return self.letter_policy != "skills_row_only" and self.confidence != "none"

    @property
    def confidence_weight(self) -> int:
        return CONFIDENCE_WEIGHT.get(self.confidence, 0)


class Metric(_Base):
    """The strictest object in the system. `claim` is copied verbatim into
    documents; nothing derives a new figure from it."""

    id: str
    claim: str
    method: str | None = None
    verifiable: bool | None = None


class EvidenceBlock(_Base):
    """Facts plus framing, deliberately not finished prose. `facts` are the only
    assertions a writer may make about this block."""

    id: str
    title: str = ""
    facts: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    use_for: list[str] = Field(default_factory=list)
    strength: str = "medium"
    one_line_hook: str | None = None
    story: str | None = None

    @property
    def strength_weight(self) -> float:
        return STRENGTH_WEIGHT.get(self.strength, 0.6)


class SkillRows(_Base):
    """The four CV rows. Row 4 is fixed text and never re-packed — it has one
    character of slack, and rewording it pushes the page to two."""

    rows: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def pinned(self, row_id: str) -> list[str]:
        return list((self.rows.get(row_id) or {}).get("pinned") or [])

    def fixed_text(self, row_id: str) -> str | None:
        return (self.rows.get(row_id) or {}).get("fixed_text")


class Personal(_Base):
    name: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""
    notice_period: str = ""
    willing_to_relocate: bool = False
    relocation_targets: list[str] = Field(default_factory=list)
    work_authorization: str = ""
    #: `never_mention` makes work authorisation a TOPIC block rather than a
    #: phrasing rule: nothing may state, imply or hint at it, including where a
    #: form invites it. Simon answers that one himself.
    work_authorization_policy: str = ""


class Style(_Base):
    voice_notes: list[str] = Field(default_factory=list)
    #: Whole letters, `verdict: good` or `verdict: avoid`. Worth more than every
    #: other field here — a model imitates a finished example far better than it
    #: follows a description of one.
    exemplars: list[dict[str, Any]] = Field(default_factory=list)
    banned_phrases: list[str] = Field(default_factory=list)
    preferred_openings: list[str] = Field(default_factory=list)
    signature_moves: list[str] = Field(default_factory=list)
    writing_samples: list[dict[str, Any]] = Field(default_factory=list)
    spelling: str = "british"
    length: dict[str, Any] = Field(default_factory=dict)


class Forbidden(_Base):
    never_claim: list[str] = Field(default_factory=list)
    never_mention: list[str] = Field(default_factory=list)
    never_say: list[str] = Field(default_factory=list)
    never_claim_specifically: list[str] = Field(default_factory=list)
    not_my_work: list[dict[str, Any]] = Field(default_factory=list)
    attribution_caveats: list[dict[str, Any]] = Field(default_factory=list)
    gated_verbs: list[str] = Field(default_factory=list)
    preferred_verbs: list[str] = Field(default_factory=list)


class Profile(_Base):
    """Everything in `profile/`, loaded once. The only place facts live."""

    personal: Personal
    education: list[dict[str, Any]] = Field(default_factory=list)
    experience: list[ExperienceEntry] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    skill_rows: SkillRows = Field(default_factory=SkillRows)
    metrics: list[Metric] = Field(default_factory=list)
    evidence: list[EvidenceBlock] = Field(default_factory=list)
    style: Style = Field(default_factory=Style)
    forbidden: Forbidden = Field(default_factory=Forbidden)
    companies: list[dict[str, Any]] = Field(default_factory=list)
    summaries: list[dict[str, Any]] = Field(default_factory=list)
    taxonomy: dict[str, Any] = Field(default_factory=dict)
    #: Short SHA of the last commit touching `profile/`, for provenance. Empty
    #: outside a git checkout.
    git_sha: str = ""

    # ── lookups ──

    def project(self, project_id: str) -> Project | None:
        return next((p for p in self.projects if p.id == project_id), None)

    def skill(self, skill_id: str) -> Skill | None:
        return next((s for s in self.skills if s.id == skill_id), None)

    def metric(self, metric_id: str) -> Metric | None:
        return next((m for m in self.metrics if m.id == metric_id), None)

    def evidence_block(self, block_id: str) -> EvidenceBlock | None:
        return next((e for e in self.evidence if e.id == block_id), None)

    @property
    def experience_bullets(self) -> list[Bullet]:
        """All seven, in printed order. There is no selection here."""
        return [b for entry in self.experience for b in entry.bullets]

    def bullet(self, bullet_id: str) -> Bullet | None:
        for bullet in self.experience_bullets:
            if bullet.id == bullet_id:
                return bullet
        for project in self.projects:
            for bullet in project.bullets:
                if bullet.id == bullet_id:
                    return bullet
        return None

    def skills_in_row(self, row_id: str) -> list[Skill]:
        return [s for s in self.skills if s.row == row_id and s.printable]
