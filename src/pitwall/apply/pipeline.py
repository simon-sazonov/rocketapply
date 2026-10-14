"""The seven stages, as one callable.

Extracted from the CLI because two front-ends need it and the flow is not a
property of either. `pitwall apply generate` and the Streamlit studio call this
same function, so a letter generated in the browser and one generated in the
terminal are the same letter.

Progress is reported through a callback rather than printed, for the same
reason: the terminal wants a line, the browser wants a progress bar, and the
pipeline should not know which it is talking to.
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["ApplicationResult", "Stage", "generate_application", "STAGES"]

#: (key, human label). The studio draws a bar from these; the CLI prints them.
STAGES: tuple[tuple[str, str], ...] = (
    ("analyse", "reading the posting"),
    ("match", "matching it against the profile"),
    ("truth", "building the do-not-claim list"),
    ("select", "choosing projects and skill rows"),
    ("plan", "planning the letter"),
    # RENDER BEFORE WRITE, since 27 Sep 2026. The CV is deterministic and was
    # already correct; the letter is the part that can fail. Rendering the CV
    # second meant a letter rejected over one typographic space destroyed the CV
    # too, and the run left nothing on disk at all.
    ("render", "rendering the CV"),
    ("write", "writing and checking the letter"),
    ("package", "writing the files"),
)

Stage = Callable[[str, str, float], None]


@dataclass
class ApplicationResult:
    job: dict[str, Any] = field(default_factory=dict)
    company: str = ""
    title: str = ""
    config: Any = None
    analysis: Any = None
    match: Any = None
    truth: Any = None
    plan: Any = None
    letter: Any = None
    fact: Any = None
    directory: pathlib.Path | None = None
    cv_pdf: pathlib.Path | None = None
    cv_docx: pathlib.Path | None = None
    letter_path: pathlib.Path | None = None
    #: The letter on the CV's letterhead — rendered for a clean or a
    #: needs-review letter, never for one marked DO_NOT_SEND.
    letter_pdf: pathlib.Path | None = None
    letter_docx: pathlib.Path | None = None
    provenance_path: pathlib.Path | None = None
    run_dir: pathlib.Path | None = None
    #: True when a CLAIM or POLICY violation survived every repair. The letter IS
    #: written — under `..._DO_NOT_SEND.txt`, with a banner — because handing back
    #: nothing at all was the behaviour Simon described as being interrupted. A
    #: style violation no longer sets this.
    blocked: bool = False
    #: clean | review | unsent | "" (no letter was produced). Drives the
    #: filename, the note's headline and the batch pile. `blocked` is kept as a
    #: name because three readers use it, but it now means `unsent` — a letter was
    #: written and marked, not "nothing exists".
    letter_state: str = ""
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def needs_review(self) -> bool:
        return bool(self.fact is not None and self.fact.needs_review)

    @property
    def ok(self) -> bool:
        return not self.blocked and not self.error


def generate_application(
    job_id: int,
    *,
    out: str | pathlib.Path = "output",
    cv: bool = True,
    letter: bool = True,
    hint: str = "",
    audit: bool = True,
    no_llm: bool = False,
    dry_run: bool = False,
    router: Any = None,
    progress: Stage | None = None,
    on_event: Any = None,
    fresh: bool = False,
) -> ApplicationResult:
    """Read a posting, decide what is true, and write an application.

    Returns rather than raises for the ordinary refusals — a job id that is not
    there, a letter the gates rejected — because both front-ends want to show
    the reason rather than a traceback.

    `on_event` receives a `StageEvent` as each stage starts and finishes, with a
    summary of what it produced (see `events.py`) — the letter drafts included,
    as they arrive. `progress` is unchanged and can be used alongside it.
    """
    from ..config import get_settings
    from . import events as ev
    from .analyzer import analyze_job
    from .families import signal_for
    from .jobs import load_job
    from .layout import Frame
    from .letter import PROMPT_VERSION, draft_and_check
    from .loader import load_profile
    from .matcher import build_signal, match
    from .packager import Provenance, output_dir, slug, write_provenance
    from .planner import plan_letter
    from .render_docx import assert_same_content, render_docx
    from .render_pdf import render_cv
    from .review import review_markdown, write_review
    from .selector import build_config, choose_variants
    from .tidy import file_generation
    from .truth import build_truth

    # Where we are, so a rate-limit message can name the stage it interrupted.
    here: dict[str, Any] = {"key": STAGES[0][0], "fraction": 0.0, "t0": time.monotonic()}
    router_ref: dict[str, Any] = {"router": None}

    def spent() -> int:
        r = router_ref["router"]
        return sum(r.tokens) if r is not None else 0

    def emit(stage: str, status: str, message: str = "", **data: Any) -> None:
        if on_event is None:
            return
        seconds = round(time.monotonic() - here["t0"], 2) if status == "done" else 0.0
        ev.safe_call(on_event, ev.StageEvent(
            job_id=job_id, stage=stage, status=status, message=message,
            fraction=here["fraction"], seconds=seconds, tokens=spent(), data=data,
        ))

    def step(key: str, message: str = "") -> None:
        index = next((i for i, (k, _) in enumerate(STAGES) if k == key), 0)
        label = next((v for k, v in STAGES if k == key), key)
        here["key"], here["fraction"] = key, (index + 1) / len(STAGES)
        here["t0"] = time.monotonic()
        if progress is not None:
            progress(key, message or label, here["fraction"])
        emit(key, "start", message or label)

    result = ApplicationResult()
    row = load_job(job_id)
    if row is None:
        result.error = f"no job {job_id} in the database"
        return result

    profile, frame = load_profile(), Frame.load()
    result.job = row
    result.company = str(row.get("posting_company") or row.get("company_name") or "Unknown")
    result.title = str(row.get("title") or "Role")

    if router is None:
        from .llm import TaskRouter

        settings = get_settings()
        router = TaskRouter(
            settings=settings, cache_dir=pathlib.Path(settings.cache_dir) / "apply"
        )

    router_ref["router"] = router
    if fresh:
        # Regenerate: ask the model again for the letter rather than replaying
        # the cached answer to the identical prompt. The posting's analysis
        # stays cached — it cannot have changed.
        router.fresh_tasks = frozenset(
            router.fresh_tasks | {"narrative_plan", "letter_write", "letter_repair"})
    if (progress is not None or on_event is not None) and router.on_wait is None:
        def waiting(provider: str, seconds: float, attempt: int) -> None:
            # Groq's free tier is 8,000 tokens a minute and the letter prompt is
            # about 5,000, so this fires routinely. Saying so is the difference
            # between "waiting" and "hung".
            current = next((v for k, v in STAGES if k == here["key"]), "working")
            message = (f"{current} · {provider} rate limit, waiting {seconds:.0f}s"
                       f" (attempt {attempt})")
            if progress is not None:
                progress(here["key"], message, here["fraction"])
            emit(here["key"], "wait", message, provider=provider, seconds=seconds,
                 attempt=attempt)
        router.on_wait = waiting

    run_dir = pathlib.Path("run") / f"{pathlib.Path(out).name}_{job_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    result.run_dir = run_dir
    router.trace_path = run_dir / "trace.jsonl"

    provenance = Provenance(
        profile_git_sha=profile.git_sha,
        job={"id": job_id, "company": result.company, "title": result.title,
             "url": row.get("url") or "", "prompt_version": PROMPT_VERSION},
    )

    # ── stages 1-3 ──
    if no_llm:
        step("analyse", "skipping the model — deterministic selection only")
        emit("analyse", "skip", "no model: deterministic selection only")
        emit("match", "skip", "nothing to match without an analysis")
        signal = signal_for("data_science")
        step("truth")
        truth = build_truth(profile, job=row)
    else:
        step("analyse")
        calls_before = len(router.calls)
        analysis = analyze_job(row, router)
        result.analysis = analysis
        last = router.calls[-1] if len(router.calls) > calls_before else None
        emit("analyse", "done", **ev.analysis_payload(
            analysis, cached=bool(last and last.cached),
            model=f"{last.provider}/{last.model}" if last else "",
        ))
        step("match")
        matched = match(analysis, profile)
        result.match = matched
        emit("match", "done", **ev.match_payload(matched))
        signal = build_signal(analysis, profile, row)
        step("truth")
        # Both opinions on what is missing: the analyzer's and the job search's
        # own scorer. `build_truth` verifies each against the profile first.
        # The scorer's languages are read too: before v10 they were stored in
        # `missing_skills`, and that is what kept "fluent German" out of letters.
        scored = row.get("match") or {}
        truth = build_truth(
            profile,
            missing_requirements=[
                *matched.missing_terms,
                *(scored.get("missing_skills") or []),
                *(scored.get("missing_languages") or []),
            ],
            job=row,
        )
        provenance.coverage = {
            "required": matched.coverage_required,
            "preferred": matched.coverage_preferred,
            "overall": matched.overall,
        }
    result.truth = truth
    emit("truth", "done", **ev.truth_payload(truth))

    # ── stage 4a: the CV slots ──
    step("select")
    # `choose_variants` was missing here until 28 Sep 2026, so every application
    # ever generated printed the canonical experience wording and the nine
    # pre-approved variants in `experience.yaml` were read by nothing.
    config = build_config(
        profile, frame, signal, variants=choose_variants(profile, signal),
    )
    result.config = config
    provenance.cv_config = {
        "projects": [{"id": s.id, "bullets": s.bullets, "score": s.score} for s in config.projects],
        "skills": config.skills,
        "experience_variants": config.experience_variants,
        "rationale": config.rationale,
    }
    provenance.overrides += config.overrides
    provenance.used_ids += config.used_ids
    emit("select", "done", **ev.select_payload(config, profile))

    if dry_run:
        # Before anything is claimed: a dry run must not create a directory.
        result.prompt_tokens, result.completion_tokens = router.tokens
        return result

    # ── stage 5: the CV and the folder, BEFORE the letter can fail ──
    step("render")
    # Claimed, not merely chosen. Two reasons, and the second only showed up
    # once a letter finally passed: `claim=True` is atomic, so parallel batch
    # workers cannot both take the same name — and it CREATES the directory,
    # which nothing else did when `--no-cv` skipped the CV render. The letter
    # write then failed on a directory that was never made.
    directory = output_dir(out, result.company, result.title, claim=True, job_id=job_id)
    result.directory = directory
    # The owner's name from the profile, so the app is not one person's. Built
    # from the company and role, not the folder: the folder is named by job id
    # for Simon, while these files go to a recruiter, who should see
    # `Simon_Sazonov_Wayve_Software_Engineer_CV.pdf`, never an id or a `_v2`.
    stem = f"{slug(profile.personal.name)}_{slug(result.company)}_{slug(result.title)}"
    divergences: list[str] = []
    if cv:
        pdf = render_cv(profile, frame, config, directory / f"{stem}_CV.pdf")
        docx = render_docx(profile, config, directory / f"{stem}_CV.docx")
        divergences = assert_same_content(pdf.printed, docx.bullets)
        if divergences:
            # Recorded and carried to the tail rather than returned on. The two
            # files are already written, so returning here would leave them with
            # no provenance and no note explaining what is wrong with them.
            result.error = (
                f"the PDF and the DOCX disagree on {len(divergences)} bullet(s) — one was "
                f"built from something other than the profile ids"
            )
            result.warnings.append(result.error)
        result.cv_pdf, result.cv_docx = pdf.path, docx.path
        provenance.page_count["cv"] = pdf.page_count
        provenance.notes += pdf.notes
        provenance.warnings += pdf.warnings
        result.notes += pdf.notes
        result.warnings += pdf.warnings
        emit("render", "done", **ev.render_payload(
            directory=directory, cv_pdf=pdf.path, pages=pdf.page_count,
            warnings=list(pdf.warnings), divergences=len(divergences),
        ))
    else:
        emit("render", "skip", "no CV asked for", directory=str(directory))

    # ── stages 4b, 5b, 6: the letter ──
    #
    # WRAPPED, because everything above this line is already on disk. A Groq
    # daily-quota death at letter 40 of 100 used to cost all forty CVs; now it
    # costs forty letters and leaves forty folders that each say why.
    try:
        if letter and not no_llm and result.analysis is not None:
            step("plan")
            plan, blocks = plan_letter(
                profile, result.analysis, result.match, truth, router, row, hint=hint
            )
            result.plan = plan
            provenance.narrative_plan = {
                "main_theme": plan.main_theme, "secondary_theme": plan.secondary_theme,
                "evidence_order": plan.evidence_order, "company_angle": plan.company_angle,
                # v6's two new fields. `role_attraction` is what the closing
                # paragraph is built from, so a letter whose ending reads flat is
                # diagnosed here rather than guessed at.
                "role_attraction": plan.role_attraction,
                "domain_bridge": plan.domain_bridge,
                "opening_strategy": plan.opening_strategy, "language": plan.language,
                "target_words": plan.target_words,
                "company_notes_source": plan.company_notes_source,
            }
            provenance.company_research = _company_provenance(plan, result.company)
            # Also to the run directory, because provenance is only written on a run
            # that reaches packaging. A rejected letter used to take its plan with
            # it, and the plan is the first thing worth reading when prose is wrong.
            (run_dir / "04b_plan.json").write_text(
                json.dumps(provenance.narrative_plan, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            provenance.used_ids += plan.evidence_order
            emit("plan", "done", **ev.plan_payload(plan))

            if not dry_run:
                step("write")
                write_index = next(i for i, (k, _) in enumerate(STAGES) if k == "write")

                def letter_progress(message: str, fraction: float) -> None:
                    # The letter owns one slot in the bar; sub-progress moves inside it.
                    if progress is not None:
                        progress(
                            "write", message,
                            (write_index + max(0.0, min(fraction, 1.0))) / len(STAGES),
                        )

                def keep_draft(round_index: int, attempt: int, text: str) -> None:
                    """Every draft on disk the moment the model returns it — and on
                    screen, for a watcher."""
                    (run_dir / f"05b_letter_r{round_index}a{attempt}.txt").write_text(
                        text, encoding="utf-8"
                    )
                    emit("write", "draft", f"draft · round {round_index}, attempt {attempt}",
                         round=round_index, attempt=attempt, text=text,
                         words=len(text.split()))

                result.letter, result.fact = draft_and_check(
                    profile, plan, blocks, truth, router, row, written_by="groq",
                    audit=audit, progress=letter_progress, on_draft=keep_draft,
                )
                (run_dir / "05b_letter_draft.txt").write_text(result.letter.text, encoding="utf-8")
                if result.fact is not None:
                    provenance.fact_check = {
                        "sentences": len(result.fact.verdicts),
                        "supported": len(result.fact.verdicts) - len(result.fact.failures),
                        "write_retries": result.letter.attempts - 1,
                        "check_rounds": result.fact.rounds,
                        "independent": result.fact.independent,
                        "auditor": result.fact.auditor,
                        # WHICH sentences, not just how many. Until this was added
                        # the objections existed only in the browser tab that
                        # generated them: the file got a NEEDS_REVIEW suffix, the
                        # provenance got a count, and the trace got token totals.
                        # Six letters shipped flagged with no record of what for,
                        # which is also why "does a rewrite round help?" could not
                        # be answered from disk.
                        "flagged": [
                            {"verdict": v.verdict, "span": v.span, "sentence": v.sentence}
                            for v in result.fact.failures
                        ],
                    }
                    result.notes += result.fact.notes
                emit("write", "done", **ev.write_payload(result.letter, result.fact),
                     text=result.letter.text)
        else:
            reason = ("no letter asked for" if not letter else
                      "no model: the letter needs one" if no_llm else "no posting analysis")
            emit("plan", "skip", reason)
            emit("write", "skip", reason)
    except Exception as exc:  # noqa: BLE001 - the CV is on disk; the note says why
        log.warning("the letter stage failed for job %s: %s", job_id, exc)
        result.warnings.append(f"the letter stage stopped: {exc}")
        emit(here["key"], "error", f"the letter stage stopped: {exc}", error=str(exc))


    # ── stage 7: the tail, reached on EVERY path that claimed a directory ──
    #
    # There is no `return` between the CV render and this block, and that is the
    # whole design. The old code had three: a dry run, a gate rejection, and a
    # PDF/DOCX divergence, and each of them left a folder with no note or no
    # folder at all.
    step("package")
    result.letter_state = _letter_state(result)
    result.blocked = result.letter_state == "unsent"

    if result.letter is not None:
        result.letter_path = _write_letter_file(directory, stem, result)
        provenance.page_count["letter"] = 1
        if result.letter_state != "clean":
            (run_dir / "REJECTED_letter.txt").write_text(
                result.letter.text, encoding="utf-8"
            )
        if result.letter_state in ("clean", "review"):
            # Typeset for the upload field. Never for DO_NOT_SEND: a finished-
            # looking PDF of an unsupported claim is the file sent by mistake.
            # Never fatal either — the .txt is already written.
            try:
                from .render_letter import render_letter_files

                typeset = render_letter_files(
                    directory, stem, result.letter.text, profile=profile, frame=frame,
                    company=result.company, title=result.title,
                )
                result.letter_pdf, result.letter_docx = typeset.pdf, typeset.docx
                provenance.page_count["letter"] = typeset.page_count
                result.warnings += typeset.warnings
            except Exception as exc:  # noqa: BLE001 - see above
                log.warning("could not typeset the letter for job %s: %s", job_id, exc)
                result.warnings.append(f"the letter PDF was not made: {exc}")

    provenance.llm_calls = [
        {"stage": c.task, "provider": c.provider, "model": c.model,
         "prompt_tokens": c.prompt_tokens, "completion_tokens": c.completion_tokens,
         "ms": c.ms, "cached": c.cached}
        for c in router.calls
    ]
    provenance.letter = {
        "state": result.letter_state,
        "file": result.letter_path.name if result.letter_path else "",
        "repairs": [
            {"code": r.code, "tier": r.tier, "what": r.what, "by": r.by}
            for r in (getattr(result.letter, "repairs", None) or [])
        ],
    }
    result.prompt_tokens, result.completion_tokens = router.tokens

    write_review(directory, review_markdown(
        company=result.company, title=result.title, url=str(row.get("url") or ""),
        state=result.letter_state or "clean",
        letter=result.letter, fact=result.fact, plan=result.plan,
        company_research=provenance.company_research,
        repairs=list(getattr(result.letter, "repairs", None) or []),
        warnings=result.warnings + list(
            getattr(result.letter, "repair_notes", None) or []
        ),
        notes=result.notes,
        cv_pdf=str(result.cv_pdf or ""), letter_file=str(result.letter_path or ""),
        tokens=result.tokens, prompt_version=PROMPT_VERSION,
        profile_git_sha=profile.git_sha, generated_at=provenance.generated_at,
    ))
    result.provenance_path = write_provenance(directory, provenance)

    # ── filed, last of all ──
    #
    # `output/` is three folders — ready, needs-changes, archive — and a run
    # that stayed at the top level until someone remembered to run
    # `apply tidy` is exactly the pile this was meant to replace. It happens
    # HERE and not at `output_dir` because the bucket depends on
    # `letter_state`, which does not exist until the letter has been written
    # and gated.
    #
    # Every path on the result is repointed afterwards. They are absolute and
    # were captured before the move, so a caller that opens `result.cv_pdf` —
    # the Studio's preview does, and so does the batch report — would be
    # reading a file that is no longer there.
    filed = file_generation(
        directory,
        state=result.letter_state,
        prompt_version=PROMPT_VERSION,
        has_cv=result.cv_pdf is not None,
        has_letter=result.letter_path is not None,
        root=out,
        job_id=job_id,
    )
    if filed != directory:
        for field in ("directory", "cv_pdf", "cv_docx", "letter_path", "letter_pdf",
                      "letter_docx", "provenance_path"):
            path = getattr(result, field)
            if path is not None:
                setattr(result, field, filed / path.relative_to(directory)
                        if path != directory else filed)
    # After filing, so every path in it is where the file now is.
    emit("package", "done", **ev.package_payload(result))
    return result


def _letter_state(result: ApplicationResult) -> str:
    """`clean`, `review`, `unsent`, or `""` when no letter was produced.

    `clean` deliberately requires NOTHING outstanding — not merely "no claims".
    Under the new shipping rule a letter can survive with a banned phrase or a
    flat closing, and such a letter must not read as verified.
    """
    if result.letter is None:
        return ""
    if result.letter.blocking:
        return "unsent"
    if result.letter.violations or result.letter.banned:
        return "review"
    if result.fact is not None and (result.fact.needs_review or not result.fact.independent):
        return "review"
    return "clean"


#: Prepended to a letter that must not be sent, so it cannot be scrolled past. The
#: NEEDS_REVIEW letter deliberately gets nothing: six of the first seven flagged
#: letters scored 8.3-9.0 out of 10, so they are usually fine and Simon asked to
#: be able to send a file without deleting anything out of it first.
DO_NOT_SEND_BANNER = (
    "DO NOT SEND — one claim in this letter is not supported by the profile.\n"
    "See REVIEW.md in this folder. Delete these three lines and fix the sentence\n"
    "named there before sending.\n\n"
)

LETTER_SUFFIX = {
    "clean": "_Cover_Letter.txt",
    "review": "_Cover_Letter_NEEDS_REVIEW.txt",
    "unsent": "_Cover_Letter_DO_NOT_SEND.txt",
}


def _write_letter_file(
    directory: pathlib.Path, stem: str, result: ApplicationResult
) -> pathlib.Path:
    """The letter, under the name its state earns, and otherwise untouched.

    The review block that used to be appended under the signature is gone — it
    lives in REVIEW.md now. Simon wants to send the file without deleting anything
    out of it, and a letter carrying its own critique cannot be sent as it stands.
    """
    path = directory / f"{stem}{LETTER_SUFFIX.get(result.letter_state, LETTER_SUFFIX['review'])}"
    body = result.letter.text
    if result.letter_state == "unsent":
        body = DO_NOT_SEND_BANNER + body
    path.write_text(body, encoding="utf-8")
    return path




def _company_provenance(plan: Any, company: str) -> dict[str, Any]:
    """Where the "why this company" facts came from, for `provenance.json`.

    Reads the cache a second time rather than threading the record through the
    planner, because the planner's job is to return a plan and this is
    bookkeeping. A miss here costs the URLs, not the letter.
    """
    if plan.company_notes_source == "profile":
        return {"source": "profile", "urls": list(plan.company_sources)}
    if plan.company_notes_source != "research":
        return {}
    from .research_cache import ResearchCache

    try:
        record = ResearchCache().get(company)
    except Exception:  # noqa: BLE001 - provenance never fails a run
        record = None
    if record is None:
        return {"source": "research", "urls": list(plan.company_sources)}
    return {
        "source": "research",
        "fetched_at": record.fetched_at,
        "verified": record.verified,
        "independent": record.independent,
        "model": record.model,
        "queries": list(record.queries),
        "urls": [s.get("source", "") for s in record.sentences] or list(record.sources),
    }
