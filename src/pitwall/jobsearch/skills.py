"""Skill extraction and the owner's skill inventory — §17-19 of the spec.

Both sides of the match are mapped onto one controlled vocabulary
(`config/skills_vocab.yaml`), so a posting's "sklearn" and a profile's
"scikit-learn" meet at `scikit_learn`. Nothing is fuzzy here: a skill is found
when one of its spellings appears as a whole word, and not otherwise. That keeps
"missing skills" honest — a skill is only called missing when the posting
actually names it.
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..config import config_dir

log = logging.getLogger(__name__)

VOCAB_FILE = "skills_vocab.yaml"
MY_SKILLS_FILE = "my_skills.yaml"
MY_SKILLS_EXAMPLE = "my_skills.example.yaml"

#: Proficiency on the 1-3 scale -> how much a matched skill counts. A skill you
#: have only touched still counts for something; claiming it is another matter,
#: and that is the CV tailoring's job, not the score's.
PROFICIENCY_WEIGHT = {1: 0.6, 2: 0.85, 3: 1.0}


@dataclass(frozen=True)
class Skill:
    id: str
    label: str
    category: str


@dataclass
class Vocabulary:
    skills: dict[str, Skill] = field(default_factory=dict)
    #: (pattern, canonical id, prefilter, case_sensitive). The prefilter is the
    #: synonym itself as a plain substring (spaced or hyphenated), which rules
    #: out almost every pattern before its regex runs — ~400 boundary regexes
    #: over a 5 KB posting were most of a job-search run's time.
    patterns: list[tuple[re.Pattern[str], str, str, bool]] = field(default_factory=list)
    #: every spelling -> canonical id, for resolving the owner's own list
    lookup: dict[str, str] = field(default_factory=dict)

    def label(self, skill_id: str) -> str:
        skill = self.skills.get(skill_id)
        return skill.label if skill else skill_id.replace("_", " ")

    def resolve(self, name: str) -> str | None:
        """A skill as the owner typed it ("Scikit-Learn", "sklearn") -> id."""
        key = " ".join(str(name).strip().split())
        if key in self.skills:
            return key
        return self.lookup.get(key.lower()) or self.lookup.get(key)

    def extract(self, text: str | None) -> list[str]:
        if not text:
            return []
        lowered = " ".join(text.lower().split())
        spaced = " ".join(text.split())
        found: dict[str, None] = {}
        for pattern, skill_id, key, case_sensitive in self.patterns:
            if skill_id in found:
                continue
            haystack = spaced if case_sensitive else lowered
            if key not in haystack and key.replace(" ", "-") not in haystack:
                continue
            if pattern.search(text):
                found[skill_id] = None
        return list(found)


def _pattern(synonym: str, case_sensitive: bool) -> re.Pattern[str]:
    # \b does not work around "+", "#" or "." ("c++", "c#", ".net", "a/b"),
    # so the boundary is spelled out: not glued to a word character either side.
    body = re.escape(synonym).replace(r"\ ", r"[\s-]+")
    flags = 0 if case_sensitive else re.I
    # "&" too: "R&D" is not the R language.
    return re.compile(rf"(?<![\w+#&]){body}(?![\w+#&])", flags)


def parse_vocabulary(data: dict[str, Any]) -> Vocabulary:
    vocab = Vocabulary()
    owner: dict[str, str] = {}
    for skill_id, spec in (data or {}).items():
        spec = spec or {}
        target = spec.get("alias_of") or skill_id
        if target == skill_id:
            vocab.skills[skill_id] = Skill(
                skill_id, str(spec.get("label") or skill_id), str(spec.get("category") or "other")
            )
        case_sensitive = bool(spec.get("case_sensitive"))
        for synonym in spec.get("synonyms") or []:
            synonym = str(synonym)
            key = synonym if case_sensitive else synonym.lower()
            if key in owner and owner[key] != target:
                raise ValueError(
                    f"skills_vocab.yaml: {synonym!r} is listed under both "
                    f"{owner[key]!r} and {target!r} — a spelling may mean one skill only"
                )
            owner[key] = target
            vocab.lookup[key] = target
            key = " ".join(synonym.split())
            vocab.patterns.append((
                _pattern(synonym, case_sensitive), target,
                key if case_sensitive else key.lower(), case_sensitive,
            ))
        vocab.lookup.setdefault(skill_id, target)
        label = str(spec.get("label") or "")
        if label:
            vocab.lookup.setdefault(label.lower(), target)
    # Longer spellings first, so "apache spark" is tried before "spark" — the
    # id is the same either way, but the order makes the evidence readable.
    vocab.patterns.sort(key=lambda pair: -len(pair[0].pattern))
    return vocab


@lru_cache(maxsize=4)
def _load_vocabulary(path: str, mtime: float) -> Vocabulary:
    _ = mtime  # part of the cache key: an edited file is re-read
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return parse_vocabulary(data)


def vocabulary(path: Path | str | None = None) -> Vocabulary:
    p = Path(path) if path else config_dir() / VOCAB_FILE
    if not p.exists():
        log.warning("no skill vocabulary at %s — skill matching is off", p)
        return Vocabulary()
    return _load_vocabulary(str(p), p.stat().st_mtime)


# ─────────────────────────── the owner's skills ──────────────────────────


@dataclass(frozen=True)
class OwnedSkill:
    id: str
    proficiency: int = 2
    years: float | None = None
    evidence: tuple[str, ...] = ()

    @property
    def weight(self) -> float:
        return PROFICIENCY_WEIGHT.get(self.proficiency, 0.85)


def my_skills_path() -> Path:
    """The personal file if it exists, else the tracked example."""
    personal = config_dir() / MY_SKILLS_FILE
    return personal if personal.exists() else config_dir() / MY_SKILLS_EXAMPLE


def load_my_skills(path: Path | str | None = None,
                   vocab: Vocabulary | None = None) -> tuple[dict[str, OwnedSkill], list[str]]:
    """`({skill_id: OwnedSkill}, [warnings])`. Unknown names are reported, not
    dropped silently — a typo in the profile would otherwise read as a gap."""
    vocab = vocab or vocabulary()
    p = Path(path) if path else my_skills_path()
    if not p.exists():
        return {}, [f"no skill profile at {p.name} — skill matching scores every job 'unknown'"]
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out: dict[str, OwnedSkill] = {}
    warnings: list[str] = []
    for row in data.get("skills") or []:
        if isinstance(row, str):
            row = {"skill": row}
        name = str(row.get("skill") or row.get("id") or "").strip()
        if not name:
            continue
        skill_id = vocab.resolve(name)
        if not skill_id:
            warnings.append(
                f"{name!r} is not in skills_vocab.yaml — add it there to have it matched"
            )
            continue
        try:
            proficiency = int(row.get("proficiency") or 2)
        except (TypeError, ValueError):
            proficiency = 2
        years = row.get("years")
        out[skill_id] = OwnedSkill(
            id=skill_id,
            proficiency=max(1, min(3, proficiency)),
            years=float(years) if isinstance(years, int | float) else None,
            evidence=tuple(str(e) for e in row.get("evidence") or []),
        )
    return out, warnings


def save_my_skills(rows: list[dict[str, Any]], path: Path | str | None = None) -> Path:
    """Write the Profile page's table back to `config/my_skills.yaml`."""
    p = Path(path) if path else config_dir() / MY_SKILLS_FILE
    clean = []
    for row in rows:
        name = str(row.get("skill") or "").strip()
        if not name:
            continue
        entry: dict[str, Any] = {"skill": name, "proficiency": int(row.get("proficiency") or 2)}
        if row.get("years") not in (None, ""):
            with contextlib.suppress(TypeError, ValueError):
                entry["years"] = float(row["years"])
        evidence = row.get("evidence")
        if isinstance(evidence, str):
            evidence = [e.strip() for e in evidence.split(";") if e.strip()]
        if evidence:
            entry["evidence"] = list(evidence)
        clean.append(entry)
    header = (
        "# Your skill inventory — §18-19 of the job-search spec. Personal: this file\n"
        "# is gitignored. Edited from the 🧭 Job Setup page, or by hand.\n"
        "#   skill        a name or synonym from config/skills_vocab.yaml\n"
        "#   proficiency  1 = touched it, 2 = working knowledge, 3 = strong\n"
        "#   evidence     where you used it — what a CV line could point to\n"
    )
    p.write_text(header + yaml.safe_dump({"skills": clean}, sort_keys=False,
                                         allow_unicode=True), encoding="utf-8")
    return p
