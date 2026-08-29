"""Dependency floors and the interpreter trap — §0.6.

The system `python3` on the owner's machine is Anaconda 3.13 with
pydantic-settings 2.6.1, which cannot import this package: `NoDecode` first
shipped in 2.7.0. v1 failed with a bare `cannot import name 'NoDecode'`, which
names the symbol and not the cause.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8")


def _floors(specs: list[str]) -> dict[str, str]:
    out = {}
    for spec in specs:
        req = Requirement(spec)
        for clause in req.specifier:
            if clause.operator == ">=":
                out[req.name.lower()] = clause.version
    return out


def test_pydantic_settings_floor_is_the_release_that_shipped_nodecode():
    """2.6.1 does not have it, 2.7.0 does — checked against the PyPI wheels."""
    assert _floors(PYPROJECT["project"]["dependencies"])["pydantic-settings"] == "2.7"
    assert "pydantic-settings>=2.7" in REQUIREMENTS


def test_the_floor_is_explained_where_someone_will_read_it():
    assert "NoDecode" in REQUIREMENTS


def test_python_is_pinned_at_install_time():
    assert PYPROJECT["project"]["requires-python"] == ">=3.11"


def test_every_runtime_import_is_declared():
    """`pip install -e .` has to produce a working install on a clean machine."""
    declared = set(_floors(PYPROJECT["project"]["dependencies"]))
    for name in ("streamlit", "pandas", "pydantic", "httpx", "openai", "typer",
                 "pyyaml", "jinja2", "ddgs", "tenacity", "rapidfuzz", "openpyxl"):
        assert name in declared, f"{name} is imported but not declared"


def test_the_config_import_guard_names_the_cause_not_the_symbol():
    source = (ROOT / "src" / "pitwall" / "config.py").read_text(encoding="utf-8")
    assert "pydantic-settings >= 2.7" in source
    assert "Anaconda" in source
    assert ".venv/bin/python" in source


def test_the_package_refuses_an_interpreter_that_is_too_old():
    source = (ROOT / "src" / "pitwall" / "__init__.py").read_text(encoding="utf-8")
    assert "sys.version_info < (3, 11)" in source


def test_the_version_moved_to_v2():
    import pitwall

    assert pitwall.__version__ == PYPROJECT["project"]["version"] == "2.0.0"
