"""The explainable match score — §20, §25, §69 of the spec.

Seven components, each a value in 0-1 with a word for it (strong / ok / weak /
unknown) and the reasons behind it, combined with the owner's weights into a
0-100 score. The output is never a bare number:

    82 — strong
    + Role: machine learning, AI (title)
    + Graduate / entry level
    + Skills: Python, PyTorch, SQL (5 of 7)
    ? Sponsorship unclear — the posting does not say
    - Not in your profile: Kubernetes, AWS

`+` is why it matched, `?` is uncertain, `-` is a concern. Unknown components
sit in the middle of their range, so a posting that says little is neither
promoted nor buried for its silence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from .companies import normalize_title
from .filters import regions_of, remote_open_to_you
from .locations import canonical_city, country_name
from .settings import JobSearchConfig
from .skills import OwnedSkill, Vocabulary
from .visa import LIKELY_SUPPORTED, NO_SPONSORSHIP, SPONSORSHIP_EXPLICIT

LANGUAGE_NAMES = {"de": "German", "fr": "French", "nl": "Dutch", "it": "Italian",
                  "es": "Spanish", "sv": "Swedish", "pt": "Portuguese", "en": "English",
                  "da": "Danish", "fi": "Finnish", "no": "Norwegian", "ru": "Russian",
                  "uk": "Ukrainian", "pl": "Polish"}
LANGUAGE_PENALTY = 8.0      # points per spoken language asked for that you do not list
#: How much of that a posting costs by the level you listed. B1 gets you
#: through a Portuguese-language office, not a Portuguese-language client call.
LEVEL_SHARE = {None: 1.0, "A1": 1.0, "A2": 1.0, "B1": 0.5}
SKILLS_FOR_CONFIDENCE = 5   # below this many named skills, skill fit is shrunk to neutral


@dataclass
class Component:
    value: float
    label: str               # strong | ok | weak | unknown
    note: str = ""

    def as_dict(self, weight: float) -> dict[str, Any]:
        return {"value": round(self.value, 3), "label": self.label, "note": self.note,
                "weight": weight}


@dataclass
class Match:
    score: float
    label: str
    components: dict[str, Component]
    plus: list[str] = field(default_factory=list)
    minus: list[str] = field(default_factory=list)
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    #: Languages the posting requires at a level the owner does not have, as
    #: "German" or "Italian (you have A2)". Kept apart from `missing_skills`,
    #: where they used to land by accident — the letter's truth layer still
    #: forbids claiming them, it just reads both lists.
    missing_languages: list[str] = field(default_factory=list)
    visa_fit: str = "unknown"      # ok | explicit | likely | unknown | no


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        out = json.loads(value or "[]")
        return out if isinstance(out, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _grade(value: float, strong: float = 0.8, ok: float = 0.55) -> str:
    return "strong" if value >= strong else "ok" if value >= ok else "weak"


def score_label(score: float) -> str:
    return "strong" if score >= 75 else "good" if score >= 60 else "fair" if score >= 45 \
        else "weak"


# ───────────────────────────── components ────────────────────────────────


def role_component(job: dict[str, Any], cfg: JobSearchConfig,
                   plus: list[str], minus: list[str]) -> Component:
    labels = _list(job.get("role_labels")) or ["OTHER"]
    prefs = cfg.roles.preference
    value = max((prefs.get(label, 0.0) for label in labels), default=0.0)
    names = ", ".join(label.replace("_", " ").lower() for label in labels if label != "OTHER")
    title = normalize_title(job.get("title"))
    best_title, best = "", 0.0
    for wanted in cfg.roles.preferred_titles:
        ratio = fuzz.token_set_ratio(title, normalize_title(wanted))
        if ratio > best:
            best_title, best = wanted, ratio
    if best >= 90:
        value = min(1.0, value + 0.1)
        plus.append(f"+ Title is close to \"{best_title}\"")
    if names:
        (plus if value >= 0.8 else minus).append(
            f"{'+' if value >= 0.8 else '-'} Role: {names}"
        )
    return Component(value, _grade(value, 0.85, 0.6), names or "other")


def experience_component(job: dict[str, Any], cfg: JobSearchConfig,
                         plus: list[str], minus: list[str]) -> Component:
    limit = cfg.seniority.max_experience_years
    lo = job.get("experience_min")
    hi = job.get("experience_max")
    level = job.get("experience_level") or "unknown"
    required = bool(job.get("experience_required"))
    if lo is not None:
        lo = float(lo)
        span = f"{lo:g}–{float(hi):g}" if hi is not None else f"{lo:g}+"
        if required:
            if lo <= 0 or (hi is not None and float(hi) <= limit):
                plus.append(f"+ {span} years asked")
                return Component(1.0, "strong", f"{span} years")
            value = max(0.5, 1.0 - 0.1 * lo)
            plus.append(f"+ {span} years asked — within your {limit:g}")
            return Component(value, _grade(value), f"{span} years")
        if lo > limit:
            minus.append(f"? Prefers {span} years (not required)")
            return Component(0.55, "ok", f"prefers {span} years")
        return Component(0.85, "strong", f"prefers {span} years")
    if level == "entry":
        plus.append("+ Graduate / entry level")
        return Component(1.0, "strong", "entry level")
    if level == "intern":
        plus.append("+ Internship / student role")
        return Component(0.9, "strong", "internship")
    if level == "mid":
        minus.append("- Asks for several years' experience (no number given)")
        return Component(0.45, "weak", "several years")
    if level == "senior":
        minus.append("- Marked as a senior role by the source")
        return Component(0.25, "weak", "senior")
    minus.append("? Experience not stated")
    return Component(0.5, "unknown", "not stated")


#: What a skill named only as a wish ("a plus") counts for, against 1 for a
#: requirement. Missing one should cost a graduate little: it is the list an
#: employer expects a junior hire not to have covered yet.
NICE_TO_HAVE_WEIGHT = 1 / 3


def skills_component(job: dict[str, Any], owned: dict[str, OwnedSkill], vocab: Vocabulary,
                     plus: list[str], minus: list[str]) -> tuple[Component, list[str], list[str]]:
    wanted = [str(s) for s in _list(job.get("skills"))]
    if not wanted:
        minus.append("? No recognisable skills in the posting")
        return Component(0.5, "unknown", "none named"), [], []
    nice = {str(s) for s in _list(job.get("skills_nice"))}

    def weight(skill: str) -> float:
        return NICE_TO_HAVE_WEIGHT if skill in nice else 1.0

    matched = [s for s in wanted if s in owned]
    missing = [s for s in wanted if s not in owned]
    total = sum(weight(s) for s in wanted) or 1.0
    coverage = min(1.0, sum(owned[s].weight * weight(s) for s in matched) / total * 1.15)
    # Three skills named and three matched says less than eight of ten: with
    # few skills named, the value is pulled toward the neutral middle.
    certainty = min(1.0, len(wanted) / SKILLS_FOR_CONFIDENCE)
    value = coverage * certainty + 0.5 * (1.0 - certainty)
    labels = [vocab.label(s) for s in matched]
    if matched:
        head = ", ".join(labels[:5]) + ("…" if len(labels) > 5 else "")
        (plus if value >= 0.55 else minus).append(
            f"{'+' if value >= 0.55 else '-'} Skills: {head} ({len(matched)} of {len(wanted)})"
        )
    gaps = [s for s in missing if s not in nice]
    wishes = [s for s in missing if s in nice]
    if gaps:
        gap = ", ".join(vocab.label(s) for s in gaps[:4]) + ("…" if len(gaps) > 4 else "")
        minus.append(f"- Not in your profile: {gap}")
    if wishes:
        wish = ", ".join(vocab.label(s) for s in wishes[:4]) + ("…" if len(wishes) > 4 else "")
        minus.append(f"? Nice to have, not in your profile: {wish}")
    return Component(value, _grade(value, 0.7, 0.45), f"{len(matched)}/{len(wanted)}"), \
        matched, missing


def location_component(job: dict[str, Any], cfg: JobSearchConfig,
                       plus: list[str], minus: list[str]) -> Component:
    cities = list(_list(job.get("cities")))
    countries = list(_list(job.get("countries")))
    preferred = {canonical_city(c).lower() for c in cfg.locations.preferred_cities}
    liked = [c for c in cities if canonical_city(c).lower() in preferred]
    if liked:
        plus.append(f"+ {liked[0]} is a preferred city")
        return Component(1.0, "strong", liked[0])
    if job.get("remote_status") == "remote" and not cities:
        regions = regions_of(job)
        open_ = remote_open_to_you(regions, cfg)
        region = ", ".join(regions) or "region not stated"
        if open_:
            plus.append(f"+ Remote ({region}) — open to you")
            return Component(0.9, "strong", f"remote {region}")
        minus.append(f"? Remote ({region}) — check it is open where you live")
        return Component(0.6, "unknown", f"remote {region}")
    allowed = set(cfg.locations.countries)
    inside = [c for c in countries if not allowed or c in allowed]
    if inside:
        plus.append(f"+ {country_name(inside[0])} is on your list")
        return Component(0.75, "ok", country_name(inside[0]))
    if not countries:
        minus.append("? Location unclear")
        return Component(0.5, "unknown", "unclear")
    return Component(0.3, "weak", ", ".join(country_name(c) for c in countries))


def visa_component(job: dict[str, Any], cfg: JobSearchConfig,
                   plus: list[str], minus: list[str],
                   sponsors: Any = None) -> tuple[Component, str]:
    status = job.get("visa_status") or "UNKNOWN"
    evidence = job.get("visa_evidence") or ""
    countries = list(_list(job.get("countries")))
    remote_only = job.get("remote_status") == "remote" and not _list(job.get("cities"))
    if not cfg.work_authorization.needs_sponsorship:
        return Component(1.0, "strong", "not needed"), "ok"
    authorized = [c for c in countries if cfg.is_authorized(c)]
    if authorized or (remote_only and remote_open_to_you(regions_of(job), cfg)):
        where = country_name(authorized[0]) if authorized else "the remote region"
        plus.append(f"+ No sponsorship needed in {where}")
        return Component(1.0, "strong", "not needed"), "ok"
    if status == SPONSORSHIP_EXPLICIT:
        plus.append(f"+ Sponsorship indicated: \"{evidence}\"")
        return Component(1.0, "strong", "indicated"), "explicit"
    if status == LIKELY_SUPPORTED:
        minus.append(f"? {evidence[:1].upper() + evidence[1:]} — confirm before relying on it")
        return Component(0.75, "ok", "likely"), "likely"
    if status == NO_SPONSORSHIP:
        minus.append(f"- Says no sponsorship: \"{evidence}\"")
        return Component(0.2, "weak", "not offered"), "no"
    # The posting says nothing; a government register may. Never for an
    # agency posting, whose "company" is the agency, not the employer.
    if sponsors and not job.get("agency") and countries:
        hit = sponsors.lookup(job.get("posting_company"), countries)
        if hit:
            plus.append(f"+ Licensed visa sponsor — {hit}")
            return Component(0.8, "ok", "licensed sponsor"), "likely"
    if evidence.startswith("mixed signals"):
        minus.append(f"? Sponsorship: {evidence}")
    else:
        minus.append("? Sponsorship unclear — the posting does not say")
    return Component(0.45, "unknown", "unclear"), "unknown"


def _interest_re(term: str) -> re.Pattern[str]:
    """British and American spellings both count: optimisation / optimization."""
    body = re.escape(term.strip().lower()).replace(r"\ ", r"[\s-]+")
    body = re.sub(r"is(e|ing|ation)", r"i[sz]\1", body)
    return re.compile(rf"\b{body}\w{{0,3}}\b", re.I)


def career_component(job: dict[str, Any], cfg: JobSearchConfig,
                     plus: list[str], minus: list[str]) -> Component:
    title = job.get("title") or ""
    text = job.get("description") or ""
    in_title: list[str] = []
    in_body: list[str] = []
    for term in cfg.career_interests:
        pattern = _interest_re(term)
        if pattern.search(title):
            in_title.append(term)
        elif pattern.search(text):
            in_body.append(term)
    hits = in_title + in_body
    if not hits:
        return Component(0.25, "weak", "no overlap")
    value = min(1.0, 0.25 + 0.18 * len(hits) + (0.15 if in_title else 0.0))
    plus.append("+ Career direction: " + ", ".join(hits[:5]))
    return Component(value, _grade(value, 0.75, 0.5), ", ".join(hits[:3]))


def company_component(target: dict[str, Any] | None,
                      plus: list[str], minus: list[str]) -> Component:
    if not target:
        return Component(0.3, "unknown", "not a target")
    priority = (target.get("priority") or "B").upper()
    value = {"A": 1.0, "B": 0.75, "C": 0.5}.get(priority, 0.75)
    plus.append(f"+ Target company (priority {priority})")
    return Component(value, _grade(value, 0.9, 0.6), f"priority {priority}")


def fit_component(job: dict[str, Any], plus: list[str], minus: list[str]) -> Component:
    """The CV-similarity percentile, when a CV was there to compare with."""
    rank = job.get("cv_similarity")
    if rank is None:
        return Component(0.5, "unknown", "no CV to compare")
    rank = float(rank)
    if rank >= 0.9:
        plus.append(f"+ Reads like your CV — closer than {rank:.0%} of open postings")
    elif rank < 0.25:
        minus.append("- Little in common with your CV's wording")
    return Component(rank, _grade(rank, 0.8, 0.5), f"top {1 - rank:.0%}")


# ─────────────────────────────── the score ───────────────────────────────


#: Points off for a title naming a topic you avoid (`avoid_topics`).
AVOID_PENALTY = 10.0

#: A recruitment agency's posting hides the employer: the target list, the
#: sponsor registers and dedupe cannot see who it is. Marked, never filtered.
AGENCY_PENALTY = 3.0


def score(job: dict[str, Any], cfg: JobSearchConfig, owned: dict[str, OwnedSkill],
          vocab: Vocabulary, target: dict[str, Any] | None = None,
          sponsors: Any = None) -> Match:
    """One posting against the owner's profile. Pure."""
    plus: list[str] = []
    minus: list[str] = []
    components: dict[str, Component] = {}
    components["role"] = role_component(job, cfg, plus, minus)
    components["experience"] = experience_component(job, cfg, plus, minus)
    components["skills"], matched, missing = skills_component(job, owned, vocab, plus, minus)
    components["location"] = location_component(job, cfg, plus, minus)
    components["visa"], visa_fit = visa_component(job, cfg, plus, minus, sponsors)
    components["career"] = career_component(job, cfg, plus, minus)
    components["company"] = company_component(target, plus, minus)
    components["fit"] = fit_component(job, plus, minus)

    weights = cfg.weights.model_dump()
    total = sum(weights[name] for name in components) or 1.0
    value = sum(weights[name] * comp.value for name, comp in components.items()) / total * 100

    cost = 0.0
    missing_langs: list[str] = []
    for lang in _list(job.get("languages_required")):
        level = cfg.language_level(lang)
        share = LEVEL_SHARE.get(level, 0.0)
        name = LANGUAGE_NAMES.get(lang, lang)
        if share >= 1.0:
            missing_langs.append(f"{name} (you have {level})" if level else name)
        elif share > 0:
            minus.append(f"? Asks for {name} — you have {level}")
        cost += share
    if missing_langs:
        minus.append("- Asks for " + " and ".join(missing_langs))
    value -= min(2.0, cost) * LANGUAGE_PENALTY
    masters = "masters" in (job.get("education_required"), job.get("education_preferred"))
    if masters and cfg.degree in ("masters", "phd"):
        plus.append("+ Master's level asked for")
    if job.get("education_preferred") == "phd" and cfg.degree != "phd":
        minus.append("? PhD preferred")
    if job.get("relocation"):
        plus.append("+ Relocation support mentioned (not the same as a visa)")
    avoided = [t for t in cfg.avoid_topics if _interest_re(t).search(job.get("title") or "")]
    if avoided:
        value -= AVOID_PENALTY
        minus.append("- A topic you avoid: " + ", ".join(avoided[:3]))
    if job.get("agency"):
        value -= AGENCY_PENALTY
        minus.append("? Posted by a recruitment agency — the employer is not named")
    if job.get("description_truncated"):
        minus.append("? Only a snippet of the posting is available — details may be missing")
    elif not job.get("description"):
        minus.append("? No description — judged on the title alone")

    value = max(0.0, min(100.0, value))
    return Match(
        score=round(value, 1),
        label=score_label(value),
        components=components,
        plus=plus,
        minus=minus,
        matched_skills=matched,
        missing_skills=missing,
        missing_languages=missing_langs,
        visa_fit=visa_fit,
    )
