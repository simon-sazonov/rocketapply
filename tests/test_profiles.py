"""Domain profiles — §3.1, and the rule that governs the whole phase.

    "if generalisation makes the owner's case worse, do not do it"

So the first half of this file is not about generalisation at all: it is about
proving that adding it changed nothing. `config/profiles/motorsport.yaml` was
generated from the constants it replaces, and the test below fails the moment
the two drift apart.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pitwall import profiles
from pitwall.models import ROLE_FAMILIES, SENIORITIES, Role
from pitwall.scoring.heuristics import FAMILY_KEYWORDS, family_keywords

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "config" / "profiles"


@pytest.fixture(autouse=True)
def fresh_profile(monkeypatch):
    monkeypatch.delenv("PITWALL_PROFILE", raising=False)
    profiles.reload()
    yield
    profiles.reload()


# ──────────────── the owner's case is unchanged (the rule) ───────────────


def test_motorsport_is_the_default():
    assert profiles.DEFAULT_PROFILE == "motorsport"
    assert profiles.selected_name() == "motorsport"


def test_the_motorsport_profile_matches_the_built_in_defaults():
    """The transcription check. If this fails, §3.1 changed the owner's setup."""
    raw = yaml.safe_load((SHIPPED / "motorsport.yaml").read_text(encoding="utf-8"))
    assert tuple(raw["role_families"]) == ROLE_FAMILIES
    assert tuple(raw["seniorities"]) == SENIORITIES
    assert {k: tuple(v) for k, v in raw["family_keywords"].items()} == FAMILY_KEYWORDS


def test_the_loaded_profile_matches_the_built_in_defaults():
    p = profiles.active()
    assert tuple(p.role_families) == ROLE_FAMILIES
    assert tuple(p.seniorities) == SENIORITIES
    assert {k: tuple(v) for k, v in p.family_keywords.items()} == FAMILY_KEYWORDS


def test_family_keywords_resolve_to_the_built_in_list_by_default():
    for family in ROLE_FAMILIES:
        assert family_keywords(family) == FAMILY_KEYWORDS[family]


# ─────────────────── degrading rather than breaking ──────────────────────


def test_a_missing_profile_falls_back_to_the_built_ins(monkeypatch):
    """A typo in PITWALL_PROFILE must not empty the vocabulary."""
    monkeypatch.setenv("PITWALL_PROFILE", "does-not-exist")
    profiles.reload()
    p = profiles.active()
    assert tuple(p.role_families) == ROLE_FAMILIES
    assert p.path is None


def test_a_broken_profile_falls_back_to_the_built_ins(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("role_families: [a, b\n  broken: {", encoding="utf-8")
    p = profiles.load_profile("bad", path=bad)
    assert tuple(p.role_families) == ROLE_FAMILIES


def test_an_empty_profile_falls_back_field_by_field(tmp_path):
    thin = tmp_path / "thin.yaml"
    thin.write_text("name: thin\n", encoding="utf-8")
    p = profiles.load_profile("thin", path=thin)
    assert p.name == "thin"
    assert tuple(p.role_families) == ROLE_FAMILIES
    assert {k: tuple(v) for k, v in p.family_keywords.items()} == FAMILY_KEYWORDS


# ───────────────────────── another profile works ─────────────────────────


def test_the_generic_profile_is_a_working_example():
    p = profiles.load_profile("generic", path=SHIPPED / "generic.yaml")
    assert p.name == "generic"
    assert "software" in p.role_families
    assert "vehicle_dynamics" not in p.role_families
    assert p.keywords_for("software")
    assert p.organisation_noun == "organisation"


def test_switching_profile_changes_the_role_vocabulary(monkeypatch):
    Role(title="Race Engineer", family="vehicle_dynamics")   # fine on motorsport

    monkeypatch.setenv("PITWALL_PROFILE", "generic")
    profiles.reload()
    Role(title="Backend Engineer", family="software")        # fine on generic
    with pytest.raises(ValueError, match="not in"):
        Role(title="Race Engineer", family="vehicle_dynamics")


def test_switching_profile_changes_the_family_keywords(monkeypatch):
    assert "trackside" in family_keywords("performance")
    monkeypatch.setenv("PITWALL_PROFILE", "generic")
    profiles.reload()
    assert "backend" in family_keywords("software")
    assert family_keywords("performance") == FAMILY_KEYWORDS["performance"], (
        "a family the profile does not define falls back rather than emptying"
    )


def test_every_shipped_profile_is_self_consistent():
    for path in sorted(SHIPPED.glob("*.yaml")):
        p = profiles.load_profile(path.stem, path=path)
        assert p.role_families, path.name
        assert p.default_role_family in p.role_families, path.name
        unknown = [f for f in p.family_keywords if f not in p.role_families]
        assert not unknown, f"{path.name}: family_keywords for unknown families {unknown}"
        assert "unknown" in p.seniorities, f"{path.name}: the LLM must be able to say 'unknown'"


def test_the_profiles_directory_is_discoverable():
    assert set(profiles.available()) >= {"motorsport", "generic"}


def test_seniority_validation_follows_the_profile(monkeypatch):
    Role(title="X", family="data_science", seniority="graduate|senior")
    with pytest.raises(ValueError, match="not in"):
        Role(title="X", family="data_science", seniority="wizard")
