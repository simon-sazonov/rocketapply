"""Many applications in one unattended run, and a verdict on each.

The point of a batch is not speed. It is that Simon should not read a hundred
letters to find the four that are wrong. Every run therefore ends with each job
in exactly one of four piles, and EVERY pile has a folder in `output/` with a CV,
a letter and a `REVIEW.md` in it:

* **ready** — every deterministic check passed. Send it.
* **review** — a named check failed on phrasing, not on fact. The note says which.
* **blocked** — a claim the profile cannot support survived every repair. The
  letter is written as `..._DO_NOT_SEND.txt`, with a banner, because handing back
  nothing at all is how the tool used to waste a run.
* **error** — the stage itself fell over. The CV is still there.

**The pile is decided by the deterministic checks, never by the model audit.**
That is a measured decision, not a preference. Of the first seven letters, six
were stamped `NEEDS_REVIEW` by the local auditor and Simon then graded those same
letters 8.3 to 9.0 out of 10. An auditor that flags six letters in seven turns
triage back into reading everything. The deterministic gates behave the opposite
way: they caught a letter claiming Munich as Berlin, and they have never yet
objected to a sentence Simon defended.

The model audit still runs when asked for, and what it says is recorded. It just
does not decide the pile.

## Shards

`--shards 3 --shard 0` takes every third job. The split is by position in the
sorted id list, so it is the same split on every rerun and two shards can never
be handed the same job. Each shard is a separate process with its own
`GROQ_API_KEY` in its environment, which is all the isolation the router needs —
the key comes from the environment, so nothing in the code has to know that more
than one exists.

Each shard appends to its own ledger. Appending to a shared file from several
processes is safe only for small writes and is not worth relying on; merging at
report time costs nothing.
"""

from __future__ import annotations

import concurrent.futures as cf
import dataclasses
import datetime as dt
import json
import logging
import pathlib
import re
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: Serialises ledger appends and progress callbacks between worker threads.
_WRITE_LOCK = threading.Lock()

__all__ = [
    "BatchRow", "LEDGER_DIR", "shard_of", "split", "load_ledger", "append_row",
    "quality_checks", "duplicate_sentences", "run_batch", "format_report",
]

LEDGER_DIR = pathlib.Path("run") / "batch"

#: From `style.yaml: length.cover_letter_words`. A letter far outside this is a
#: symptom rather than a style quibble: the 164-word Waymo letter had lost a
#: whole project to the rewrite loop.
MIN_WORDS, MAX_WORDS = 250, 360

#: `style.yaml: length.cover_letter_paragraphs`, plus the greeting and sign-off.
EXPECTED_PARAGRAPHS = 4


@dataclass
class BatchRow:
    """One job's outcome, written to the ledger as a single JSON line."""

    job_id: int
    #: ready | review | blocked | error | skipped
    status: str = "error"
    company: str = ""
    title: str = ""
    directory: str = ""
    letter: str = ""
    words: int = 0
    #: Why it is not `ready`. Empty when it is.
    reasons: list[str] = field(default_factory=list)
    #: What the model auditor said, recorded but not used to decide the pile.
    audit_flagged: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    shard: int = 0
    prompt_version: str = ""
    finished_at: str = ""

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __post_init__(self) -> None:
        self.finished_at = self.finished_at or dt.datetime.now(dt.UTC).isoformat(
            timespec="seconds"
        )


def shard_of(job_id: int, job_ids: Sequence[int], shards: int) -> int:
    """Which shard owns this job. By position, so the split is reproducible."""
    if shards <= 1:
        return 0
    ordered = sorted(set(job_ids))
    return ordered.index(job_id) % shards if job_id in ordered else 0


def split(job_ids: Iterable[int], shards: int, index: int) -> list[int]:
    """This shard's jobs, in order.

    Position-based rather than `job_id % shards`, because ids from a job board
    arrive in clumps and modulo would hand one shard most of a batch.
    """
    ordered = sorted({int(j) for j in job_ids})
    if shards <= 1:
        return ordered
    if not 0 <= index < shards:
        raise ValueError(f"shard {index} is not in range 0..{shards - 1}")
    return [job for position, job in enumerate(ordered) if position % shards == index]


def _ledger_path(name: str, index: int) -> pathlib.Path:
    return LEDGER_DIR / name / f"ledger.shard{index}.jsonl"


def append_row(name: str, row: BatchRow) -> pathlib.Path:
    path = _ledger_path(name, row.shard)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dataclasses.asdict(row), ensure_ascii=False) + "\n")
    return path


def load_ledger(name: str) -> list[BatchRow]:
    """Every shard's rows, merged. This is also how resume knows what is done."""
    directory = LEDGER_DIR / name
    if not directory.exists():
        return []
    rows: list[BatchRow] = []
    for path in sorted(directory.glob("ledger.shard*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue  # a line torn by a killed process; the job simply reruns
            known = {f.name for f in dataclasses.fields(BatchRow)}
            rows.append(BatchRow(**{k: v for k, v in payload.items() if k in known}))
    return rows


# ──────────────────────────── the verdict per letter ─────────────────────────


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def quality_checks(
    text: str,
    *,
    job: dict,
    profile,
    truth=None,
    responsibilities: Sequence[str] = (),
) -> list[str]:
    """Everything that can be decided without a model. Empty means ready to send.

    Each reason names the defect, because a report that says "review" without
    saying why costs as much attention as reading the letter.
    """
    from .letter import (
        PLACEHOLDER,
        banned_phrase_hits,
        invented_location_hits,
        overclaim_hits,
        shape_hits,
        stale_availability_hits,
        summarising_close_hits,
    )

    reasons: list[str] = []
    if not text.strip():
        return ["no letter was produced"]

    # ── the gates, the same ones that block a single run ──
    if truth is not None:
        reasons += [f"{v.code.lower()}: {v.span!r}" for v in truth.check(text)]
    reasons += [
        f"banned phrase: {phrase!r}"
        for phrase in banned_phrase_hits(text, profile.style.banned_phrases)
    ]
    reasons += [
        f"{hit.code.lower()}: {hit.span!r}"
        for hit in (
            invented_location_hits(text, job, allowed=[profile.personal.location or ""])
            + stale_availability_hits(text, profile)
            + overclaim_hits(text)
            + summarising_close_hits(text)
            # Shared with the generation gate rather than duplicated. The two
            # had drifted, and the stricter one was this report — which runs
            # after the letter is already written out, so an unsigned letter
            # reached `output/` before they were merged.
            + shape_hits(text, profile)
        )
    ]
    reasons += [f"placeholder left in: {found!r}" for found in PLACEHOLDER.findall(text)]

    # ── shape ──
    words = len(text.split())
    if not MIN_WORDS <= words <= MAX_WORDS:
        reasons.append(f"{words} words, outside {MIN_WORDS}-{MAX_WORDS}")
    blocks = _paragraphs(text)
    # greeting + four paragraphs + sign-off, give or take a split sign-off.
    # Upper bound widened by one on 27 Sep 2026: `letter.repair_mechanics` appends
    # the signature as its own block when the writer forgot it, so a letter that
    # was repaired INTO correctness could otherwise be reported for the shape the
    # repair gave it.
    if not EXPECTED_PARAGRAPHS + 1 <= len(blocks) <= EXPECTED_PARAGRAPHS + 4:
        reasons.append(f"{len(blocks)} blocks, expected about {EXPECTED_PARAGRAPHS + 2}")

    # ── is it actually about this posting? ──
    # Zero overlap with what the posting says the job involves is the signature
    # of a letter that would fit any employer, which is the thing Simon's own
    # reviews complained about in every single letter.
    if responsibilities:
        spoken = text.lower()
        shared = {
            word
            for line in responsibilities
            for word in re.findall(r"[a-z]{6,}", line.lower())
            if word in spoken
        }
        if not shared:
            reasons.append("nothing in it comes from this posting's responsibilities")
    return reasons


def duplicate_sentences(letters: dict[str, str], *, min_words: int = 9) -> dict[str, list[str]]:
    """Sentences that appear verbatim in more than one letter.

    Only a batch can see this, and it is the failure that would make a hundred
    letters worthless: the model settling into one template and re-serving it
    with the company name swapped. Short sentences are ignored because "I would
    be glad to talk it through." is a legitimate house closing, not a collapse.
    """
    seen: dict[str, list[str]] = {}
    for name, text in letters.items():
        for raw in re.split(r"(?<=[.!?])\s+", text):
            sentence = " ".join(raw.split())
            if len(sentence.split()) >= min_words:
                seen.setdefault(sentence, []).append(name)
    return {s: names for s, names in seen.items() if len(set(names)) > 1}


# ──────────────────────────────── the run ────────────────────────────────────


def run_batch(
    job_ids: Iterable[int],
    *,
    name: str,
    shards: int = 1,
    index: int = 0,
    out: str | pathlib.Path = "output",
    cv: bool = True,
    audit: bool = False,
    resume: bool = True,
    workers: int = 1,
    on_row: Callable[[BatchRow], None] | None = None,
    generate: Callable[..., Any] | None = None,
    research: bool = True,
    on_research: Callable[..., None] | None = None,
) -> list[BatchRow]:
    """This shard's jobs, one after another, never stopping for one failure.

    `audit` defaults to **off**, the reverse of the single-job default. The model
    audit is 31 local calls at 9-66 seconds each — about ten minutes a letter on
    this machine, seventeen hours for a hundred — and it does not decide the pile.
    Turn it on for a shortlist once the batch has sorted them.

    A crash on job 57 must not cost jobs 1-56, so every job is wrapped and its
    outcome written to the ledger before the next one starts.

    `workers` runs that many jobs at once in this process, which is how one
    button in the Studio uses three keys: the shared key pool hands each thread
    whichever key has the most room. Set it to the number of keys — more threads
    than keys only queues them behind the same ceilings. This is safe because
    three things were made safe for it first: output directories are claimed
    atomically, cache entries are written and renamed rather than written in
    place, and SQLite is in WAL mode with a thirty-second busy timeout.
    """
    from .batch import quality_checks as _checks  # re-imported for monkeypatching in tests
    from .loader import load_profile
    from .pipeline import generate_application
    from .truth import severity

    generate = generate or generate_application
    mine = split(job_ids, shards, index)
    done = (
        {row.job_id for row in load_ledger(name) if row.status != "error"}
        if resume else set()
    )
    todo = [job for job in mine if job not in done]
    log.info("shard %d/%d: %d job(s), %d already done, %d to do",
             index, shards, len(mine), len(mine) - len(todo), len(todo))

    profile = load_profile()

    if research and todo:
        # A SERIAL pre-pass, outside the pool on purpose. The unit of work in a
        # batch is the job, but what a company builds does not depend on which of
        # its postings is being answered — so two workers on two jobs at one
        # employer would run the same three searches twice and race on the same
        # cache file for the same answer. Once, here, before anything starts.
        #
        # Never fatal. Research is an improvement on the letters, not a
        # dependency of them: a batch that cannot research still writes exactly
        # the letters it writes today.
        try:
            from .research import prepass

            done_research = prepass(todo, profile=profile, on_company=on_research)
            log.info("researched %d compan(ies) before the batch", len(done_research))
        except Exception as exc:  # noqa: BLE001 - see above
            log.warning("company research pre-pass failed, continuing without it: %s", exc)

    def one(job_id: int) -> BatchRow:
        started = time.monotonic()
        row = BatchRow(job_id=job_id, shard=index)
        try:
            result = generate(job_id, out=out, cv=cv, letter=True, audit=audit)
            row.company = result.company
            row.title = result.title
            row.prompt_tokens, row.completion_tokens = (
                result.prompt_tokens, result.completion_tokens
            )
            row.directory = result.directory.name if result.directory else ""
            row.letter = result.letter_path.name if result.letter_path else ""
            if result.error:
                row.status, row.reasons = "error", [result.error]
            elif result.blocked:
                # A CLAIM survived every retry and every repair. The letter IS
                # written, under `..._DO_NOT_SEND.txt` with a banner, and so is the
                # CV and the note — "blocked" now means "written and marked", not
                # "nothing exists". This is still the pile that needs a human, and
                # the reason is named rather than left to be guessed at.
                row.status = "blocked"
                row.words = len(result.letter.text.split()) if result.letter else 0
                row.reasons = [
                    f"{severity(v.code)}/{v.code.lower()}: {v.span!r}" for v in (
                        result.letter.blocking if result.letter else []
                    )
                ] or ["a claim the profile cannot support survived every repair"]
            else:
                text = result.letter.text if result.letter else ""
                row.words = len(text.split())
                row.audit_flagged = len(result.fact.failures) if result.fact else 0
                reasons = _checks(
                    text, job=result.job, profile=profile, truth=result.truth,
                    responsibilities=(
                        result.analysis.responsibilities if result.analysis else ()
                    ),
                )
                row.reasons = reasons
                # `letter_state` has already weighed the violations, the auditor and
                # whether the audit was independent; `_checks` adds the shape and
                # cross-letter opinions. Only an explicit "review" forces the pile,
                # so a caller that leaves the field unset is not downgraded by it.
                row.status = (
                    "review" if reasons or result.letter_state == "review" else "ready"
                )
        except Exception as exc:  # noqa: BLE001 — one bad job must not end the night
            log.exception("job %s failed", job_id)
            row.status, row.reasons = "error", [f"{type(exc).__name__}: {exc}"]
        row.seconds = round(time.monotonic() - started, 1)
        with _WRITE_LOCK:
            # The ledger is one file per shard; concurrent threads inside a shard
            # would interleave their lines without this.
            append_row(name, row)
            if on_row is not None:
                on_row(row)
        return row

    if workers <= 1:
        return [one(job_id) for job_id in todo]
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, todo))


def format_report(rows: Sequence[BatchRow], *, letters: dict[str, str] | None = None) -> str:
    """The thing Simon actually reads instead of a hundred letters."""
    if not rows:
        return "  nothing in the ledger yet"
    order = {"ready": 0, "review": 1, "blocked": 2, "error": 3, "skipped": 4}
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1

    lines = [
        "  " + " · ".join(
            f"{status}: {counts[status]}" for status in sorted(counts, key=lambda s: order.get(s, 9))
        ),
        f"  {sum(r.tokens for r in rows):,} tokens · "
        f"{sum(r.seconds for r in rows) / 60:.0f} min · "
        f"{len(rows)} job(s)",
        "",
        f"  {'job':>6}  {'status':<8}{'words':>6}  {'company':<26}why",
    ]
    for row in sorted(rows, key=lambda r: (order.get(r.status, 9), r.job_id)):
        why = "; ".join(row.reasons)[:74] if row.reasons else ""
        lines.append(
            f"  {row.job_id:>6}  {row.status:<8}{row.words or '':>6}  "
            f"{row.company[:24]:<26}{why}"
        )

    if letters:
        repeated = duplicate_sentences(letters)
        if repeated:
            lines += ["", f"  {len(repeated)} sentence(s) appear in more than one letter:"]
            for sentence, names in list(repeated.items())[:5]:
                lines.append(f"    \"{sentence[:70]}\" — {len(set(names))} letters")
            lines.append("    (a sign the model is reusing a template rather than the posting)")

    ready = counts.get("ready", 0)
    lines += [
        "",
        f"  {ready} letter(s) passed every deterministic check. The rest name their reason above.",
    ]
    return "\n".join(lines)
