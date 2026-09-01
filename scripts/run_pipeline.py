#!/usr/bin/env python
"""The PitWall CLI, for anyone who has not run `pip install -e .`.

The commands live in `src/pitwall/cli.py` so that `pitwall …` and
`python scripts/run_pipeline.py …` are the same program rather than two that
drift. Every invocation in the README and in PITWALL_PLAN still works.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pitwall.cli import app  # noqa: E402

if __name__ == "__main__":
    app()
