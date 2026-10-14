"""Save a letter you edited by hand — through the same gates a generated one passed.

An edit is not trusted more than a model's draft: the deterministic gates run
on the new text exactly as they ran on the original (the truth layer — no
unsupported numbers, no claims the profile contradicts, no borrowed work —
and the banned phrases). Their verdict decides the file's name and the
folder's bucket, the same way it does for a generated letter:

* clean  → `_Cover_Letter.txt`, typeset PDF/DOCX, `1_ready_to_send/`
* review → `_Cover_Letter_NEEDS_REVIEW.txt`, typeset, `2_needs_changes/`
* unsent → `_Cover_Letter_DO_NOT_SEND.txt` with the banner, NOT typeset

The text the model wrote is kept in `run/edits/<folder>/` before anything is
overwritten, and the provenance records that a human changed it.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import shutil
from dataclasses import dataclass, field
from typing import Any

__all__ = ["EditResult", "save_edited_letter", "gate_text"]

EDITS_ROOT = pathlib.Path("run") / "edits"


@dataclass
class EditResult:
    directory: pathlib.Path
    state: str
    letter_path: pathlib.Path
    letter_pdf: pathlib.Path | None = None
    violations: list[str] = field(default_factory=list)
    banned: list[str] = field(default_factory=list)
    kept_original: pathlib.Path | None = None


def _analysed_gaps(job: dict[str, Any], profile: Any, router: Any = None) -> list[str]:
    """What the posting asks for that the profile lacks, as generation saw it.

    The analysis is cached by prompt, so for a job that was generated this is a
    cache hit and costs nothing. Without it an edit would be gated more loosely
    than the draft was. Never fatal: no model, no cache — the scorer's gaps
    still apply.
    """
    try:
        from .analyzer import analyze_job
        from .matcher import match

        if router is None:
            from ..config import get_settings
            from .llm import TaskRouter

            settings = get_settings()
            router = TaskRouter(settings=settings,
                                cache_dir=pathlib.Path(settings.cache_dir) / "apply")
        return list(match(analyze_job(job, router), profile).missing_terms)
    except Exception:  # noqa: BLE001 - see docstring
        return []


def gate_text(text: str, *, profile: Any, job: dict[str, Any] | None = None,
              router: Any = None) -> tuple[str, list[str], list[str]]:
    """(state, violations, banned) for a letter's text — the pipeline's rule,
    with the same do-not-claim list the pipeline built for this posting."""
    from .letter import banned_phrase_hits
    from .truth import build_truth, severity

    scored = (job or {}).get("match") or {}
    truth = build_truth(
        profile,
        missing_requirements=[*(_analysed_gaps(job, profile, router) if job else []),
                              *(scored.get("missing_skills") or []),
                              *(scored.get("missing_languages") or [])],
        job=job,
    )
    violations = truth.check(text)
    banned = banned_phrase_hits(text, list(profile.style.banned_phrases))
    blocking = [v for v in violations if severity(v.code) in ("claim", "policy")]
    state = "unsent" if blocking else "review" if (violations or banned) else "clean"
    return state, [str(v) for v in violations], banned


def _root_of(directory: pathlib.Path) -> pathlib.Path:
    from .tidy import BUCKETS

    return directory.parent.parent if directory.parent.name in BUCKETS else directory.parent


def save_edited_letter(directory: pathlib.Path | str, text: str, *, profile: Any = None,
                       job: dict[str, Any] | None = None) -> EditResult:
    """Replace the letter in an application folder with `text`, gated and re-filed."""
    from .history import read
    from .layout import Frame
    from .loader import load_profile
    from .pipeline import DO_NOT_SEND_BANNER, LETTER_SUFFIX
    from .render_letter import LETTER_DOCX_SUFFIX, LETTER_PDF_SUFFIX, render_letter_files
    from .tidy import file_generation

    directory = pathlib.Path(directory)
    generation = read(directory)
    if generation is None:
        raise ValueError(f"{directory} is not an application folder")
    profile = profile or load_profile()
    if job is None and generation.job_id:
        try:
            from .jobs import load_job

            job = load_job(generation.job_id)
        except Exception:  # noqa: BLE001 - gate without the posting's own gaps
            job = None
    text = text.replace("\r\n", "\n").strip() + "\n"
    if text.startswith(DO_NOT_SEND_BANNER.strip()[:20]):
        text = text[len(DO_NOT_SEND_BANNER):] if text.startswith(DO_NOT_SEND_BANNER) else text
    state, violations, banned = gate_text(text, profile=profile, job=job)

    stem = (generation.cv_pdf.name.removesuffix("_CV.pdf") if generation.cv_pdf
            else (generation.letter_path.name.split("_Cover_Letter")[0]
                  if generation.letter_path else directory.name))

    kept = None
    if generation.letter_path and generation.letter_path.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        keep_dir = EDITS_ROOT / directory.name
        keep_dir.mkdir(parents=True, exist_ok=True)
        kept = keep_dir / f"{stamp}_{generation.letter_path.name}"
        shutil.copy2(generation.letter_path, kept)
    for suffix in (*LETTER_SUFFIX.values(), LETTER_PDF_SUFFIX, LETTER_DOCX_SUFFIX):
        (directory / f"{stem}{suffix}").unlink(missing_ok=True)

    letter_path = directory / f"{stem}{LETTER_SUFFIX[state]}"
    letter_path.write_text((DO_NOT_SEND_BANNER + text) if state == "unsent" else text,
                           encoding="utf-8")
    letter_pdf = None
    if state in ("clean", "review"):
        typeset = render_letter_files(directory, stem, text.strip(), profile=profile,
                                      frame=Frame.load(), company=generation.company,
                                      title=generation.title)
        letter_pdf = typeset.pdf

    manifest = directory / "provenance.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    letter = data.setdefault("letter", {})
    letter.update({
        "state": state, "file": letter_path.name, "edited": True,
        "edited_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "edit_violations": violations, "edit_banned": banned,
    })
    manifest.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    review = directory / "REVIEW.md"
    if review.exists():
        note = (f"\n\n## Edited by hand, {dt.date.today():%d %b %Y}\n\n"
                f"Re-checked with the same deterministic gates: **{state}**."
                + ("".join(f"\n- {v}" for v in violations + [f"banned: {b}" for b in banned])
                   or " Nothing to object to.") + "\n")
        review.write_text(review.read_text(encoding="utf-8") + note, encoding="utf-8")

    filed = file_generation(
        directory, state=state, prompt_version=generation.prompt_version,
        has_cv=generation.cv_pdf is not None, has_letter=True, root=_root_of(directory),
        job_id=generation.job_id,
    )
    moved = filed != directory
    return EditResult(
        directory=filed, state=state,
        letter_path=(filed / letter_path.name) if moved else letter_path,
        letter_pdf=((filed / letter_pdf.name) if moved else letter_pdf) if letter_pdf else None,
        violations=violations, banned=banned, kept_original=kept,
    )
