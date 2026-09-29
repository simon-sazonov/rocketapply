"""STAGE 7 — naming, provenance, and the application record.

Provenance is what makes this auditable six months later, when an interviewer
asks about a line on the CV and the honest answer has to be more specific than
"the computer wrote it". Every generated artifact ships with the profile ids it
used and the commit those ids came from.

Nothing is ever overwritten. A second application to the same role gets `_v2`,
because the first one is what was actually sent.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = ["slug", "output_dir", "Provenance", "write_provenance", "record_application"]

_SAFE = re.compile(r"[^A-Za-z0-9]+")


def slug(value: str, limit: int = 40) -> str:
    """ASCII, underscore-joined, title-cased, truncated. Stable across runs."""
    parts = [p for p in _SAFE.split(value or "") if p]
    out = "_".join(p[:1].upper() + p[1:] for p in parts)
    return out[:limit].strip("_") or "Untitled"


def output_dir(
    root: pathlib.Path | str, company: str, role: str, *, claim: bool = False
) -> pathlib.Path:
    """`output/<Company>_<Role>/`, never reusing a directory that has content.

    A collision is a second application, not a mistake to silently overwrite —
    what went out the first time is the record of what was sent.

    `claim=True` creates the directory as it is chosen, which is what parallel
    shards need. Looking and then returning is a race: two workers whose jobs
    slug to the same company and role both see `_v2` free, both return it, and
    the second one's files land on top of the first one's. `mkdir(exist_ok=False)`
    is atomic, so exactly one worker wins each name and the loser moves on.

    The claim path never adopts an existing directory, even an empty one. A
    twelve-worker race showed why: two workers both fail `mkdir`, both then see
    the directory still empty because the winner has not written its first file
    yet, and both proceed as though they own it — the same collision, one step
    later. An orphaned empty directory from a run that died is harmless clutter;
    two runs writing one directory is lost work.
    """
    root = pathlib.Path(root)
    base = f"{slug(company)}_{slug(role)}"
    candidate = root / base
    version = 2
    while True:
        if claim:
            try:
                candidate.mkdir(parents=True, exist_ok=False)
                return candidate
            except FileExistsError:
                pass
        elif not (candidate.exists() and any(candidate.iterdir())):
            return candidate
        candidate = root / f"{base}_v{version}"
        version += 1


@dataclass
class Provenance:
    generated_at: str = ""
    profile_git_sha: str = ""
    job: dict[str, Any] = field(default_factory=dict)
    used_ids: list[str] = field(default_factory=list)
    cv_config: dict[str, Any] = field(default_factory=dict)
    narrative_plan: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, float] = field(default_factory=dict)
    fact_check: dict[str, Any] = field(default_factory=dict)
    #: Where the "why this company" paragraph's facts came from — "profile" for a
    #: hand-written `companies.yaml` entry, "research" for the machine cache,
    #: with the URLs behind them. Without this a claim about the employer has no
    #: trail back to a page, and it is the one class of claim the fact check
    #: cannot judge: `factcheck._FIRST_PERSON` skips every third-person sentence.
    company_research: dict[str, Any] = field(default_factory=dict)
    #: The letter's state (clean | review | unsent), the file it landed in, and
    #: what was repaired without asking. `history.scan` reads the state from here
    #: and falls back to the filename for folders generated before this existed.
    letter: dict[str, Any] = field(default_factory=dict)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    page_count: dict[str, int] = field(default_factory=dict)
    #: Where the solver disagreed with the model, and where the frame made a
    #: deliberate choice. The first thing to read when output looks odd.
    overrides: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def write_provenance(directory: pathlib.Path, provenance: Provenance) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "provenance.json"
    provenance.generated_at = provenance.generated_at or dt.datetime.now(dt.UTC).isoformat(
        timespec="seconds"
    )
    path.write_text(json.dumps(asdict(provenance), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def record_application(
    conn,
    *,
    job: dict[str, Any],
    company: str,
    cv_version: str,
    letter_version: str,
    directory: pathlib.Path,
) -> int:
    """Add a SHORTLISTED row so the Applications page sees this immediately.

    `cv_version` and `letter_version` are the generated filenames — the columns
    already exist for exactly this, and a filename is what answers "which CV did
    they get?" in six months.
    """
    cursor = conn.execute(
        "INSERT INTO applications (cluster_id, job_id, company_name, company_norm, title, "
        "location, status, cv_version, cover_letter_version, application_url, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, 'SHORTLISTED', ?, ?, ?, ?)",
        (
            job.get("cluster_id"),
            job.get("id"),
            company,
            (company or "").strip().lower(),
            job.get("title") or "",
            job.get("location") or "",
            cv_version,
            letter_version,
            job.get("url") or "",
            f"generated into {directory.name}",
        ),
    )
    application_id = int(cursor.lastrowid or 0)
    conn.execute(
        "INSERT INTO application_events (application_id, from_status, to_status, note) "
        "VALUES (?, NULL, 'SHORTLISTED', 'generated by pitwall apply')",
        (application_id,),
    )
    return application_id
