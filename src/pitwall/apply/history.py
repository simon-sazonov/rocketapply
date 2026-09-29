"""Every application already generated, read back off disk.

There is no new store here, because there does not need to be one: every run
already writes a `provenance.json` naming the job, the company, the prompt
version and the moment it was made, alongside the files themselves. That IS the
history — it just was not being read.

Reading rather than recording has a useful property: the history cannot drift
from what is actually on disk. A directory deleted by hand disappears from the
list, and one copied in by hand appears.
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass, field
from typing import Any

from .voice_eval import Rating, for_directory, load

log = logging.getLogger(__name__)

__all__ = ["Generation", "scan", "find"]


@dataclass
class Generation:
    directory: pathlib.Path
    generated_at: str = ""
    job_id: int = 0
    company: str = ""
    title: str = ""
    url: str = ""
    prompt_version: str = ""
    cv_pdf: pathlib.Path | None = None
    cv_docx: pathlib.Path | None = None
    letter_path: pathlib.Path | None = None
    #: The letter typeset on the CV's letterhead (since 29 Sep 2026); absent
    #: for older runs and for a letter marked DO_NOT_SEND.
    letter_pdf: pathlib.Path | None = None
    letter_docx: pathlib.Path | None = None
    needs_review: bool = False
    #: Tokens the run spent on model calls that were not answered from cache.
    tokens: int = 0
    #: clean | review | unsent | "". Read from provenance where it exists, else
    #: inferred from the letter's filename.
    state: str = ""
    sentences: int = 0
    supported: int = 0
    independent: bool = True
    coverage: float = 0.0
    used_ids: list[str] = field(default_factory=list)
    rating: Rating | None = None
    #: The sentences the auditor still questioned, from `provenance.fact_check`.
    #: Empty for anything generated before v6 persisted them — those objections
    #: existed only in the browser tab that made the letter and are gone.
    flagged: list[dict] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.directory.name

    @property
    def key(self) -> str:
        """Unique per generation, for a widget key or a dict.

        NOT `name`. `apply tidy` sorts runs into `1_ready_to_send/`,
        `2_needs_changes/` and `3_archive/`, and a job regenerated after its
        first attempt was filed produces the same directory name in two
        buckets. The Studio keyed its widgets on the name and Streamlit refused
        the second one — "multiple elements with the same key" — which took the
        whole page down, not just the duplicate.
        """
        parent = self.directory.parent.name
        return f"{parent}__{self.directory.name}" if parent else self.directory.name

    @property
    def graded(self) -> bool:
        return self.rating is not None

    @property
    def letter_text(self) -> str:
        """Read on demand. A history list of twenty should not hold twenty
        letters in memory to show one."""
        if self.letter_path and self.letter_path.exists():
            return self.letter_path.read_text(encoding="utf-8")
        return ""

    @property
    def when(self) -> str:
        """`2026-09-25T11:24:42+00:00` -> `25 Sep 11:24`."""
        import datetime as dt

        try:
            stamp = dt.datetime.fromisoformat(self.generated_at)
        except (TypeError, ValueError):
            return self.generated_at
        return stamp.strftime("%d %b %H:%M")

    @property
    def objections_lost(self) -> bool:
        """Flagged, but from before the verdicts were written down.

        Worth saying out loud rather than showing an empty list: the letter needs
        review and the record of why does not exist, so the only way to recover
        it is to generate again.
        """
        return self.needs_review and not self.flagged

    @property
    def summary(self) -> str:
        if self.rating is not None:
            return (
                f"{self.rating.naturalness}/5 natural · "
                f"{self.rating.edit_burden_pct}% to rewrite"
            )
        return "not graded yet"


def _file(directory: pathlib.Path, *suffixes: str) -> pathlib.Path | None:
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.name.endswith(suffixes):
            return path
    return None


def _application_dirs(root: pathlib.Path):
    """Directories holding a `provenance.json`, at the top level or one below.

    Depth is capped at two rather than walking the tree: everything an
    application writes lives in one flat directory, so anything deeper is
    something else, and `rglob` over an `output/` with a hundred runs would
    stat every file in all of them.
    """
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        if (directory / "provenance.json").exists():
            yield directory
            continue
        for nested in sorted(directory.iterdir()):
            if nested.is_dir() and (nested / "provenance.json").exists():
                yield nested


def scan(output_dir: pathlib.Path | str = "output") -> list[Generation]:
    """Every generation under `output_dir`, newest first.

    A directory without a readable `provenance.json` is skipped rather than
    guessed at — `output/preview/` holds loose CVs from `apply preview` and is
    not an application.

    TWO LEVELS DEEP, because `apply tidy` sorts applications into buckets
    (`1_ready_to_send/`, `2_needs_changes/`, `3_archive/`) while a fresh run
    still lands at the top level: `packager.output_dir` claims its directory
    before the letter exists, so which bucket it belongs in is not yet known.
    Scanning one level only would have emptied the Studio's history the first
    time anything was tidied.
    """
    root = pathlib.Path(output_dir)
    if not root.exists():
        return []

    ratings = load()
    out = [g for g in (read(d, ratings=ratings) for d in _application_dirs(root)) if g]
    return sorted(out, key=lambda g: g.generated_at, reverse=True)


def read(directory: pathlib.Path | str, *, ratings: Any = None) -> Generation | None:
    """One application folder, or None when it has no readable manifest.

    What `scan` does per folder, for a caller that already knows the folder —
    the CSV export resolves an application's files through this, without
    walking all of `output/`.
    """
    directory = pathlib.Path(directory)
    manifest = directory / "provenance.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.info("skipping %s: %s", directory.name, exc)
        return None
    if ratings is None:
        ratings = load()
    job = data.get("job") or {}
    check = data.get("fact_check") or {}
    # Three suffixes since 27 Sep 2026. `_file` is `str.endswith(tuple)`, so
    # a `_DO_NOT_SEND.txt` letter matched none of them and the worst letter in
    # a folder read as "there is no letter here at all".
    letter = _file(
        directory, "_Cover_Letter.txt", "_Cover_Letter_NEEDS_REVIEW.txt",
        "_Cover_Letter_DO_NOT_SEND.txt",
    )
    # The state is recorded now, rather than inferred from a filename. The
    # fallback is the filename, because every folder generated before this
    # change has no `letter` block in its provenance.
    state = str((data.get("letter") or {}).get("state") or "")
    if not state and letter:
        state = ("clean" if letter.name.endswith("_Cover_Letter.txt")
                 else "unsent" if letter.name.endswith("_DO_NOT_SEND.txt") else "review")
    calls = data.get("llm_calls") or []
    return Generation(
        directory=directory,
        generated_at=str(data.get("generated_at") or ""),
        job_id=int(job.get("id") or 0),
        company=str(job.get("company") or ""),
        title=str(job.get("title") or "").strip(),
        url=str(job.get("url") or ""),
        prompt_version=str(job.get("prompt_version") or ""),
        cv_pdf=_file(directory, "_CV.pdf"),
        cv_docx=_file(directory, "_CV.docx"),
        letter_path=letter,
        letter_pdf=_file(directory, "_Cover_Letter.pdf"),
        letter_docx=_file(directory, "_Cover_Letter.docx"),
        tokens=sum(int(c.get("prompt_tokens") or 0) + int(c.get("completion_tokens") or 0)
                   for c in calls if isinstance(c, dict) and not c.get("cached")),
        state=state,
        needs_review=bool(state and state != "clean"),
        sentences=int(check.get("sentences") or 0),
        supported=int(check.get("supported") or 0),
        independent=bool(check.get("independent", True)),
        coverage=float((data.get("coverage") or {}).get("overall") or 0.0),
        used_ids=list(data.get("used_ids") or []),
        rating=for_directory(ratings, directory.name),
        flagged=[f for f in (check.get("flagged") or []) if isinstance(f, dict)],
    )


def find(name: str, output_dir: pathlib.Path | str = "output") -> Generation | None:
    return next((g for g in scan(output_dir) if g.name == name), None)
