"""First-run setup — §3.2.

The acceptance criterion is "a clean clone + `pitwall init` + sample CSVs
produces a working run on a machine with no prior state", so these tests run
against a throwaway project root rather than the repository.

Running it for real is what found the bug the last test here guards: the sample
CSVs carry their column documentation as `#` comments, and the reader was
treating that block as the header row.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pitwall import setup_wizard as wiz
from pitwall.inputs import load_companies, load_roles

ROOT = Path(__file__).resolve().parents[1]


# ────────────────── step 1: the interpreter, by name ─────────────────────


def test_the_running_interpreter_passes():
    step = wiz.check_python()
    assert step.ok, step.action
    assert sys.executable in step.detail


def test_an_old_interpreter_is_refused_with_the_remedy(monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 10, 4, "final", 0))
    step = wiz.check_python()
    assert step.ok is False
    assert "3.11" in step.action and "make venv" in step.action


def test_the_anaconda_trap_is_named_not_just_the_symbol(monkeypatch):
    """v1 failed with "cannot import name 'NoDecode'", which names neither."""
    monkeypatch.setattr(sys, "executable", "/opt/anaconda3/bin/python3")
    monkeypatch.setattr(sys, "prefix", "/opt/anaconda3")
    monkeypatch.setattr(sys, "base_prefix", "/opt/anaconda3")
    step = wiz.check_python()
    assert step.ok is False
    assert "Anaconda" in step.action
    assert "pydantic-settings" in step.action
    assert ".venv/bin/python" in step.action


def test_a_conda_base_python_inside_a_venv_is_fine(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/project/.venv/bin/python")
    monkeypatch.setattr(sys, "prefix", "/project/.venv")
    monkeypatch.setattr(sys, "base_prefix", "/opt/anaconda3")
    assert wiz.check_python().ok


# ───────────────────── step 2: the sample CSVs ───────────────────────────


def test_sample_csvs_are_written_into_an_empty_project(project):
    (project / "config" / "companies.csv").unlink()
    (project / "config" / "roles.csv").unlink()
    created, skipped = wiz.write_sample_inputs()
    assert {p.name for p in created} == {"companies.csv", "roles.csv"}
    assert skipped == []


def test_the_samples_carry_their_column_documentation(project):
    (project / "config" / "companies.csv").unlink()
    (project / "config" / "roles.csv").unlink()
    wiz.write_sample_inputs()
    text = (project / "config" / "companies.csv").read_text(encoding="utf-8")
    assert text.startswith("#")
    assert "aliases" in text and "recall" in text.lower()
    assert "negative_keywords" in text and "precision" in text.lower()


def test_the_samples_parse_and_validate_cleanly(project):
    """The §3.2 bug: `#` comments made the header row unreadable."""
    (project / "config" / "companies.csv").unlink()
    (project / "config" / "roles.csv").unlink()
    wiz.write_sample_inputs()

    companies, creport = load_companies()
    roles, rreport = load_roles()
    assert creport.errors == [], creport.errors
    assert rreport.errors == [], rreport.errors
    assert len(companies) == 2
    assert len(roles) == 3
    assert companies[0].aliases and companies[0].negative_keywords


def test_the_sample_roles_use_families_the_profile_allows(project):
    (project / "config" / "roles.csv").unlink()
    wiz.write_sample_inputs()
    roles, report = load_roles()
    assert report.errors == []
    assert {r.family for r in roles} <= set(wiz.active_profile().role_families)


def test_existing_inputs_are_never_clobbered(project):
    original = (project / "config" / "companies.csv").read_text(encoding="utf-8")
    created, skipped = wiz.write_sample_inputs()
    assert created == []
    assert len(skipped) == 2
    assert (project / "config" / "companies.csv").read_text(encoding="utf-8") == original


def test_force_replaces_them(project):
    created, skipped = wiz.write_sample_inputs(force=True)
    assert len(created) == 2 and skipped == []


def test_row_counting_ignores_the_comment_block(project):
    (project / "config" / "companies.csv").unlink()
    wiz.write_sample_inputs()
    assert wiz.count_rows(project / "config" / "companies.csv") == 2
    assert wiz.count_rows(project / "config" / "nope.csv") == 0


# ─────────────────────────── step 3: .env ────────────────────────────────


def test_env_is_copied_from_the_example(project):
    (project / ".env.example").write_text("GROQ_API_KEY=\n", encoding="utf-8")
    created, skipped = wiz.write_env()
    assert created and created.name == ".env"
    assert skipped is None


def test_an_existing_env_is_never_overwritten(project):
    (project / ".env.example").write_text("GROQ_API_KEY=\n", encoding="utf-8")
    (project / ".env").write_text("GROQ_API_KEY=gsk_mine\n", encoding="utf-8")
    created, skipped = wiz.write_env()
    assert created is None and skipped is not None
    assert "gsk_mine" in (project / ".env").read_text(encoding="utf-8")


def test_a_missing_example_is_reported_not_fatal(project):
    assert wiz.write_env() == (None, None)


# ───────────────── step 6: validation, and the vocabulary ────────────────


def test_validation_reports_both_files_together(project):
    report = wiz.validate_inputs()
    assert report.rows_read >= 2
    assert report.ok


def test_validation_does_not_write_to_the_database(project, settings):
    """`load_and_sync` would; validating must not."""
    db = Path(settings.db_file)
    wiz.validate_inputs()
    assert not db.exists()


def test_a_bad_family_is_reported_with_the_legal_values(project):
    (project / "config" / "roles.csv").write_text(
        "title,aliases,family,seniority,keywords,priority,active\n"
        "Chef,,catering,,,1,true\n",
        encoding="utf-8",
    )
    report = wiz.validate_inputs()
    assert report.errors
    assert any("catering" in i.message for i in report.errors)


def test_the_vocabulary_hint_lists_what_the_csv_may_contain(project):
    hint = wiz.vocabulary_hint()
    assert "motorsport" in hint
    assert "data_science" in hint
    assert "graduate" in hint


# ─────────────────────────── the CLI surface ─────────────────────────────


def test_init_and_validate_are_registered_commands():
    from pitwall.cli import app

    names = {c.name or c.callback.__name__ for c in app.registered_commands}
    assert {"init", "validate"} <= names


def test_the_console_script_points_at_the_same_app():
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["scripts"]["pitwall"] == "pitwall.cli:app"


def test_the_script_shim_still_works():
    """Every invocation in the README and PITWALL_PLAN goes through this path."""
    text = (ROOT / "scripts" / "run_pipeline.py").read_text(encoding="utf-8")
    assert "from pitwall.cli import app" in text


@pytest.mark.parametrize("command", ["run", "search", "extract", "score", "jobs",
                                     "openings", "coverage", "export", "stats",
                                     "preview", "init-db", "build-queries"])
def test_no_command_was_lost_in_the_move(command):
    from pitwall.cli import app

    names = {c.name or c.callback.__name__.replace("_", "-") for c in app.registered_commands}
    assert command in names
