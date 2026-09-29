"""Where the two Streamlit apps live, so moving one is a single edit.

Both apps are launched from the repo root (the engine's `output/`, `run/` and
`data/` paths are relative to it). Each keeps its own theme in
`<app>/.streamlit/config.toml`, which Streamlit reads because it sits beside
the main script.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PITWALL_APP = ROOT / "apps" / "pitwall"
PITWALL_PAGES = PITWALL_APP / "pages"
ROCKET_APP = ROOT / "apps" / "rocketapply"


def on_path(folder: Path) -> None:
    """Make an app's helper modules (`_shared`, `_jobs`, `ui.*`) importable."""
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
