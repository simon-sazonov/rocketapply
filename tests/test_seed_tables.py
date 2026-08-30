"""The research tables are seed input, and only that — §3.4.

`motorsport_teams.csv` and `motorsport_positions.csv` sat at the repository root
carrying eleven `YOUR_status` / `YOUR_contact_name` / `YOUR_notes` columns: a
spreadsheet CRM from before PitWall had one. They were empty in all 882 rows,
and keeping them would have left two systems for the same job — which §3.4 says
not to do.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "config" / "seed"
TABLES = ("motorsport_teams.csv", "motorsport_positions.csv")


@pytest.mark.parametrize("name", TABLES)
def test_the_table_lives_under_config_seed(name):
    assert (SEED / name).exists()
    assert not (ROOT / name).exists(), "it must not be in two places"


@pytest.mark.parametrize("name", TABLES)
def test_no_crm_columns_survive(name):
    rows = list(csv.DictReader((SEED / name).open(encoding="utf-8-sig")))
    leftovers = [c for c in rows[0] if c.upper().startswith("YOUR_")]
    assert leftovers == [], f"{name} still carries a parallel CRM: {leftovers}"


@pytest.mark.parametrize("name", TABLES)
def test_the_research_itself_survived(name):
    """Stripping the CRM columns must not have cost the useful content."""
    rows = list(csv.DictReader((SEED / name).open(encoding="utf-8-sig")))
    assert len(rows) > 40
    assert "team" in rows[0]
    assert any(r.get("team") for r in rows)


def test_the_seed_script_finds_them():
    import importlib.util

    spec = importlib.util.spec_from_file_location("seed", ROOT / "scripts" / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.TEAMS_CSV.exists()
    assert module.POSITIONS_CSV.exists()
    assert module.TEAMS_CSV.parent.name == "seed"


def test_the_script_was_renamed_and_the_old_name_is_gone():
    assert (ROOT / "scripts" / "seed.py").exists()
    assert not (ROOT / "scripts" / "seed_motorsport.py").exists()
