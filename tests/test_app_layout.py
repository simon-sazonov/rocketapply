"""The two-app layout: PitWall and RocketApply side by side on one engine.

What this guards is the split itself. PitWall must keep every page and its
exact dark theme, and no theme may live at the repo root, or RocketApply
inherits dark keys it never asked for (Streamlit layers the file beside the
main script over the one in the working directory).
"""

from __future__ import annotations

import tomllib

from .paths import ROOT  # noqa: TID252

PITWALL_PAGE_FILES = (
    "1_⚙️_Setup.py", "2_🔎_Find_People.py", "3_👤_People.py", "5_📊_Model_Performance.py",
    "6_💼_Openings.py", "7_🎯_Job_Search.py", "8_📨_Applications.py", "9_🧭_Job_Setup.py",
    "10_✍️_Applications_Studio.py", "11_📦_Batch_Applications.py",
)


def _toml(path):
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_the_repo_root_config_has_no_theme():
    root = _toml(ROOT / ".streamlit" / "config.toml")
    assert "theme" not in root
    assert root["browser"]["gatherUsageStats"] is False


