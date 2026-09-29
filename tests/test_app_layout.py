"""The two-app layout: PitWall and RocketApply side by side on one engine.

What this guards is the split itself. PitWall must keep every page and its
exact dark theme, and no theme may live at the repo root, or RocketApply
inherits dark keys it never asked for (Streamlit layers the file beside the
main script over the one in the working directory).
"""

from __future__ import annotations

import tomllib

from .paths import PITWALL_APP, PITWALL_PAGES, ROOT, on_path  # noqa: TID252

PITWALL_PAGE_FILES = (
    "1_⚙️_Setup.py", "2_🔎_Find_People.py", "3_👤_People.py", "5_📊_Model_Performance.py",
    "6_💼_Openings.py", "7_🎯_Job_Search.py", "8_📨_Applications.py", "9_🧭_Job_Setup.py",
    "10_✍️_Applications_Studio.py", "11_📦_Batch_Applications.py",
)


def _toml(path):
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_pitwall_keeps_every_page():
    assert (PITWALL_APP / "Home.py").is_file()
    missing = [p for p in PITWALL_PAGE_FILES if not (PITWALL_PAGES / p).is_file()]
    assert not missing, missing
    assert not (ROOT / "app").exists(), "the old app/ folder came back"


def test_pitwall_theme_sits_beside_its_main_script():
    theme = _toml(PITWALL_APP / ".streamlit" / "config.toml")["theme"]
    assert theme["base"] == "dark"
    assert theme["primaryColor"] == "#FF8000"
    assert theme["backgroundColor"] == "#0A0A0A"
    assert theme["font"].startswith("JetBrains Mono")


def test_the_repo_root_config_has_no_theme():
    root = _toml(ROOT / ".streamlit" / "config.toml")
    assert "theme" not in root
    assert root["browser"]["gatherUsageStats"] is False


def test_pitwall_shared_finds_the_repo_root():
    on_path(PITWALL_APP)
    import _shared

    assert _shared.ROOT == ROOT
    assert (_shared.ROOT / "src" / "pitwall").is_dir()
