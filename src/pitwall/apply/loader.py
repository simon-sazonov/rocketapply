"""Load `profile/` into typed models, and refuse to proceed if it is unsound.

FAIL FAST AND LOUDLY. A profile that does not validate aborts the run naming the
file, the record and the field. Everything downstream — the truth layer, the
selector, the fact checker — treats these records as ground truth, so loading a
broken one would replace a loud failure with a quiet lie.
"""

from __future__ import annotations

import logging
import pathlib
import subprocess
from typing import Any

from . import PROFILE_DIR
from .models import (
    EvidenceBlock,
    ExperienceEntry,
    Forbidden,
    Metric,
    Personal,
    Profile,
    Project,
    Skill,
    SkillRows,
    Style,
)
from .validate import Report, format_report, load_yaml, validate

log = logging.getLogger(__name__)

__all__ = ["ProfileInvalid", "load_profile", "clear_cache"]


class ProfileInvalid(RuntimeError):
    """`profile/` failed validation. The message is the validation report."""

    def __init__(self, report: Report) -> None:
        self.report = report
        super().__init__(
            f"profile/ has {len(report.errors)} error(s) and cannot be used:\n"
            + format_report(report)
        )


def _optional(directory: pathlib.Path, name: str, default: Any) -> Any:
    path = directory / name
    if not path.exists():
        return default
    return load_yaml(directory, name) or default


def _git_sha(directory: pathlib.Path) -> str:
    """The commit that last touched `profile/`, for provenance.

    Six months later, when an interviewer asks about a line on the CV, this is
    what says which version of the profile produced it. Empty outside a checkout,
    which is not an error — it just means provenance cannot pin the version.
    """
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%h", "--", str(directory)],
            capture_output=True, text=True, timeout=5, cwd=directory.parent, check=False,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


#: (resolved path, signature) -> Profile. Signature is the set of file mtimes,
#: so editing any profile file invalidates the entry on the next load.
_CACHE: dict[tuple[str, tuple], Profile] = {}


def clear_cache() -> None:
    _CACHE.clear()


def _signature(directory: pathlib.Path) -> tuple:
    return tuple(sorted((p.name, p.stat().st_mtime_ns) for p in directory.glob("*.yaml")))


def load_profile(
    directory: pathlib.Path | str | None = None, *, check: bool = True
) -> Profile:
    """Read `profile/` into a `Profile`.

    `check=False` skips validation. Only tests that deliberately construct a
    broken profile should use it — every production path wants the abort.
    """
    directory = pathlib.Path(directory) if directory else PROFILE_DIR
    key = (str(directory.resolve()), _signature(directory))
    if check and key in _CACHE:
        return _CACHE[key]

    if check:
        report = validate(directory)
        if not report.ok:
            raise ProfileInvalid(report)
        if report.warnings:
            for warning in report.warnings:
                log.info("profile warning: %s", warning)

    skills_doc = _optional(directory, "skills.yaml", {}) or {}

    profile = Profile(
        personal=Personal.model_validate(_optional(directory, "personal.yaml", {}) or {}),
        education=_optional(directory, "education.yaml", []),
        experience=[
            ExperienceEntry.model_validate(e)
            for e in _optional(directory, "experience.yaml", [])
        ],
        projects=[Project.model_validate(p) for p in _optional(directory, "projects.yaml", [])],
        skills=[Skill.model_validate(s) for s in (skills_doc.get("skills") or [])],
        skill_rows=SkillRows(rows=skills_doc.get("rows") or {}),
        metrics=[Metric.model_validate(m) for m in _optional(directory, "metrics.yaml", [])],
        evidence=[
            EvidenceBlock.model_validate(e) for e in _optional(directory, "evidence.yaml", [])
        ],
        style=Style.model_validate(_optional(directory, "style.yaml", {}) or {}),
        forbidden=Forbidden.model_validate(_optional(directory, "forbidden.yaml", {}) or {}),
        companies=_optional(directory, "companies.yaml", []),
        summaries=_optional(directory, "summaries.yaml", []),
        taxonomy=_optional(directory, "taxonomy.yaml", {}) or {},
        git_sha=_git_sha(directory),
    )

    if check:
        _CACHE[key] = profile
    return profile
