"""Domain profiles — §3.1 of PITWALL_V2_IMPLEMENTATION.md.

The vocabulary that used to be hardcoded: which role families exist, which
seniority ladder is legal, and what a job title has to contain for the heuristic
layer to recognise a family. `PITWALL_PROFILE` selects one from
`config/profiles/`.

**The governing rule is that generalisation must not make the owner's case
worse.** Two things enforce it here:

* `motorsport` is the default, and `config/profiles/motorsport.yaml` is a
  transcription of the pre-Phase-3 constants — generated from that code, with a
  test that fails if the two drift.
* every lookup falls back to those same built-in constants, so a missing,
  unreadable or half-written profile degrades to exactly the v1 behaviour rather
  than to an empty vocabulary.

Loading is lazy and cached, not done at import: `PITWALL_ROOT` moves under the
test suite, and a constant resolved at import time would be the wrong one.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .config import config_dir

log = logging.getLogger(__name__)

PROFILES_DIR = "profiles"
DEFAULT_PROFILE = "motorsport"


def _builtin_families() -> tuple[str, ...]:
    from .models import ROLE_FAMILIES

    return ROLE_FAMILIES


def _builtin_seniorities() -> tuple[str, ...]:
    from .models import SENIORITIES

    return SENIORITIES


def _builtin_family_keywords() -> dict[str, tuple[str, ...]]:
    from .scoring.heuristics import FAMILY_KEYWORDS

    return FAMILY_KEYWORDS


@dataclass(frozen=True)
class Profile:
    """One domain's vocabulary."""

    name: str = DEFAULT_PROFILE
    description: str = ""
    organisation_noun: str = "motorsport organisation"
    default_role_family: str = "data_science"
    query_templates: str = "query_templates.yaml"
    negative_keywords: str = "negative_keywords.yaml"
    role_families: tuple[str, ...] = field(default_factory=_builtin_families)
    seniorities: tuple[str, ...] = field(default_factory=_builtin_seniorities)
    family_keywords: dict[str, tuple[str, ...]] = field(
        default_factory=_builtin_family_keywords
    )
    path: Path | None = None

    @property
    def is_builtin_default(self) -> bool:
        return self.name == DEFAULT_PROFILE

    def keywords_for(self, family: str) -> tuple[str, ...]:
        return tuple(self.family_keywords.get(family, ()))


def profiles_dir() -> Path:
    return config_dir() / PROFILES_DIR


def available() -> list[str]:
    directory = profiles_dir()
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.yaml"))


def selected_name() -> str:
    return os.getenv("PITWALL_PROFILE", DEFAULT_PROFILE).strip() or DEFAULT_PROFILE


def load_profile(name: str | None = None, path: Path | str | None = None) -> Profile:
    """Read one profile. A missing or broken file degrades to the built-ins."""
    name = name or selected_name()
    target = Path(path) if path else profiles_dir() / f"{name}.yaml"
    if not target.exists():
        if name != DEFAULT_PROFILE:
            log.warning(
                "profile %r not found at %s — falling back to the built-in %s vocabulary",
                name, target, DEFAULT_PROFILE,
            )
        return Profile(name=name)

    try:
        raw: dict[str, Any] = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        log.error("profile %s is not valid YAML (%s) — using the built-in defaults", target, exc)
        return Profile(name=name, path=target)

    families = tuple(str(f) for f in (raw.get("role_families") or ())) or _builtin_families()
    seniorities = tuple(str(s) for s in (raw.get("seniorities") or ())) or _builtin_seniorities()
    keywords_raw = raw.get("family_keywords") or {}
    keywords = (
        {str(k): tuple(str(w) for w in (v or ())) for k, v in keywords_raw.items()}
        if keywords_raw
        else _builtin_family_keywords()
    )

    unknown = [f for f in keywords if f not in families]
    if unknown:
        log.warning(
            "profile %s has family_keywords for %s, which are not in role_families",
            target.name, unknown,
        )

    return Profile(
        name=str(raw.get("name") or name),
        description=str(raw.get("description") or ""),
        organisation_noun=str(raw.get("organisation_noun") or "organisation"),
        default_role_family=str(raw.get("default_role_family") or families[0]),
        query_templates=str(raw.get("query_templates") or "query_templates.yaml"),
        negative_keywords=str(raw.get("negative_keywords") or "negative_keywords.yaml"),
        role_families=families,
        seniorities=seniorities,
        family_keywords=keywords,
        path=target,
    )


@lru_cache(maxsize=8)
def _cached(name: str, root: str) -> Profile:
    return load_profile(name)


def active() -> Profile:
    """The selected profile. Cached per (name, project root)."""
    return _cached(selected_name(), str(config_dir()))


def reload() -> Profile:
    _cached.cache_clear()
    return active()
