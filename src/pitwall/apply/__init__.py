"""Tailored CV and cover-letter generation — CV_COVER_LETTER_ENGINEERING_SPEC.md.

Ninety per cent deterministic, ten per cent model. The model decides what is
*relevant*; this package guarantees what is *true*.

The CV is a frozen frame with exactly three variable slots — which three projects
appear, which skills fill each row, and which pre-approved phrasing an experience
bullet uses. Everything else on the page is a constant, and the model has no field
in its output schema with which to change it.

    from pitwall.apply.loader import load_profile
    from pitwall.apply.layout import Frame

    profile = load_profile()
    frame = Frame.load(profile)
"""

from __future__ import annotations

__all__ = ["PROFILE_DIR", "project_root"]

import pathlib


def project_root() -> pathlib.Path:
    """The repository root — src/pitwall/apply/__init__.py is three levels down."""
    return pathlib.Path(__file__).resolve().parents[3]


#: The single source of truth. Hand-authored, version-controlled, never written
#: by a model. §3 of the spec: no fact may live in a prompt, a template or in
#: Python source.
PROFILE_DIR = project_root() / "profile"
