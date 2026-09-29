"""Does a prompt change actually make the letters sound more like Simon?

Without a record this is a matter of impression, and impressions do not survive
a prompt edit: a change that improves one letter and quietly ruins the next
looks like progress. So each round writes a row per letter, keyed by the prompt
version that produced it, and the trend is a table rather than a feeling.

Two numbers, because they measure different failures:

* NATURALNESS (1-5) — would a person have written this? A letter can be perfectly
  true and still read like a machine.
* EDIT BURDEN (%) — how much would you change before sending? This is the number
  that decides whether the tool is worth using. The spec ships at 30% or below.

Scores are Simon's. Nothing here grades itself.
"""

from __future__ import annotations

import csv
import datetime as dt
import pathlib
import statistics
from dataclasses import asdict, dataclass, field

__all__ = ["Rating", "EVAL_PATH", "append", "load", "by_version", "format_trend"]

EVAL_PATH = pathlib.Path("run") / "voice_eval.csv"

FIELDS = (
    "rated_at", "prompt_version", "job_id", "company", "role",
    "naturalness", "edit_burden_pct", "words", "rounds", "needs_review", "note",
    # Which generation was graded. The same job at the same prompt version can
    # be generated more than once — three Waymo letters exist at v5 — and
    # without this a grade could not say which one it was for, nor could the
    # history tell a graded application from an ungraded one.
    "directory",
)


@dataclass
class Rating:
    rated_at: str = ""
    prompt_version: str = ""
    job_id: int = 0
    company: str = ""
    role: str = ""
    #: 1-5. Would a person have written this? A float, because a careful reader
    #: grades 4.2 and 4.7 differently and rounding both to 4 discards the only
    #: signal that separates two letters at the same prompt version.
    naturalness: float = 0.0
    #: 0-100. How much would you change before sending?
    edit_burden_pct: int = 0
    words: int = 0
    rounds: int = 1
    needs_review: bool = False
    note: str = ""
    #: The output directory name, e.g. "Waymo_2027_Summer_Intern_v2". Empty on
    #: rows written before this existed.
    directory: str = ""

    def __post_init__(self) -> None:
        self.rated_at = self.rated_at or dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def append(rating: Rating, path: pathlib.Path | None = None) -> pathlib.Path:
    path = path or EVAL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if new:
            writer.writeheader()
        writer.writerow({k: v for k, v in asdict(rating).items() if k in FIELDS})
    return path


def load(path: pathlib.Path | None = None) -> list[Rating]:
    path = path or EVAL_PATH
    if not path.exists():
        return []
    out: list[Rating] = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                out.append(Rating(
                    rated_at=row.get("rated_at", ""),
                    prompt_version=row.get("prompt_version", ""),
                    job_id=int(row.get("job_id") or 0),
                    company=row.get("company", ""),
                    role=row.get("role", ""),
                    naturalness=float(row.get("naturalness") or 0),
                    edit_burden_pct=int(row.get("edit_burden_pct") or 0),
                    words=int(row.get("words") or 0),
                    rounds=int(row.get("rounds") or 1),
                    needs_review=str(row.get("needs_review", "")).lower() == "true",
                    note=row.get("note", ""),
                    directory=row.get("directory", ""),
                ))
            except (TypeError, ValueError):
                continue
    return out


@dataclass
class VersionSummary:
    version: str
    count: int = 0
    naturalness: float = 0.0
    edit_burden: float = 0.0
    jobs: list[int] = field(default_factory=list)

    @property
    def ships(self) -> bool:
        """The spec's bar: edit burden consistently at or under 30%."""
        return self.count >= 3 and self.edit_burden <= 30


def for_directory(ratings: list[Rating], directory: str) -> Rating | None:
    """The grade given to one specific generation, if any."""
    return next((r for r in ratings if r.directory and r.directory == directory), None)


def by_version(ratings: list[Rating]) -> list[VersionSummary]:
    grouped: dict[str, list[Rating]] = {}
    for rating in ratings:
        grouped.setdefault(rating.prompt_version, []).append(rating)
    out = []
    for version, rows in grouped.items():
        out.append(VersionSummary(
            version=version,
            count=len(rows),
            naturalness=round(statistics.fmean(r.naturalness for r in rows), 2),
            edit_burden=round(statistics.fmean(r.edit_burden_pct for r in rows), 1),
            jobs=sorted({r.job_id for r in rows}),
        ))
    return sorted(out, key=lambda s: s.version)


def format_trend(ratings: list[Rating], *, how: str = "") -> str:
    """The trend across prompt versions.

    `how` is the caller's own instruction for the empty case, because the CLI
    and the Streamlit studio reach this from different places and telling a
    browser user to run a terminal command is no help at all.
    """
    if not ratings:
        return "  no ratings yet — " + (
            how or "generate a few letters for jobs you know well, and score each one"
        )
    lines = [f"  {'version':<10}{'letters':>8}{'naturalness':>14}{'edit burden':>14}   verdict"]
    for summary in by_version(ratings):
        verdict = (
            "ships" if summary.ships
            else ("needs more ratings" if summary.count < 3 else "too much editing")
        )
        lines.append(
            f"  {summary.version:<10}{summary.count:>8}{summary.naturalness:>14.2f}"
            f"{summary.edit_burden:>13.0f}%   {verdict}"
        )
    return "\n".join(lines)
