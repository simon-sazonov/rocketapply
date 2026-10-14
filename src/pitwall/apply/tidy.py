"""One folder per job in `output/`, and an index saying what is in each.

`output/` accumulates a folder per RUN, not per job. Six attempts at the same
Predium posting sat beside one attempt at Helsing, all named alike, and the only
way to tell which was worth sending was to open them. The risk that creates is
specific and expensive: sending the wrong one.

So this does two things, both reversible:

* **sort** — three folders, and every application inside one of them:

      output/
        1_ready_to_send/   complete, every gate passed, current prompt
        2_needs_changes/   the best run for a job that is not sendable yet
        3_archive/         superseded runs, kept
        4_already_sent/    applications marked applied, out of the way

  For each job the best run goes to one of the first two; the rest go to the
  archive. Nothing is deleted, because the older letters are the record of how
  the prompt got better and their grades in `run/voice_eval.csv` name them.
* **index** — `output/README.md`, one row per job, saying plainly whether each
  is ready, what is missing, and which need regenerating.

A FRESH RUN LANDS AT THE TOP LEVEL, not in a bucket: `packager.output_dir`
claims its directory before the letter is written — that ordering is deliberate,
so a rejected letter cannot destroy the CV — and the bucket is not known until
after. `tidy` files it. `history.scan` therefore looks two levels deep, or the
Studio would lose its history the first time this ran.

WHY COMPLETENESS OUTRANKS A CLEAN LETTER. A letter with no CV is not an
application, however well it reads. Five of the six Predium runs were `clean`
and were generated with `--no-cv`, so the best SENDABLE version of that job is
an older one the auditor flagged. The index says so rather than quietly
promoting a folder with half an application in it.
"""

from __future__ import annotations

import pathlib
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field

from .history import Generation, scan
from .letter import PROMPT_VERSION

__all__ = [
    "READY_DIR", "CHANGES_DIR", "ARCHIVE_DIR", "SENT_DIR", "BUCKETS", "TidyPlan", "rank",
    "build_plan", "apply_plan", "archived", "index_markdown", "bucket_for",
    "file_generation", "file_sent", "move_into", "supersede_job",
    "complete", "current", "sendable",
]

#: The three piles, and the whole point of this module: opening `output/`
#: should answer "what can I send" before anything is read. Numbered because
#: Finder and `ls` sort alphabetically, and "archive" first is the wrong
#: answer to that question.
READY_DIR = "1_ready_to_send"
CHANGES_DIR = "2_needs_changes"
ARCHIVE_DIR = "3_archive"
#: Where an application goes once it is marked applied, so `1_ready_to_send/`
#: only ever holds what is still to be sent.
SENT_DIR = "4_already_sent"
BUCKETS = (READY_DIR, CHANGES_DIR, ARCHIVE_DIR, SENT_DIR)

#: Directories that are not applications and never were — `apply preview` output
#: and leftovers from development runs. They have no `provenance.json`, so
#: `history.scan` already ignores them; they are listed so the index can say
#: what they are instead of leaving them looking like abandoned applications.
NOT_APPLICATIONS = ("preview", "exports")


def _version(generation: Generation) -> int:
    """`"v8"` -> 8. Unparseable or absent sorts lowest."""
    raw = (generation.prompt_version or "").strip().lstrip("vV")
    return int(raw) if raw.isdigit() else 0


def complete(generation: Generation) -> bool:
    """A CV and a letter. Either alone cannot be sent."""
    return bool(generation.cv_pdf and generation.letter_path)


def current(generation: Generation) -> bool:
    """Written by the prompt in use today.

    An older prompt carries whatever that prompt got wrong, and the letters
    are the evidence: every one generated before v8 holds 16-37 non-breaking
    hyphens that break an ATS keyword search, and closes with the exact
    sentence v8 was changed to stop producing.
    """
    return _version(generation) >= _version_of(PROMPT_VERSION)


def sendable(generation: Generation) -> bool:
    """Complete, passed every gate, and not written by a superseded prompt.

    ONE PREDICATE, used by the table and the index alike. They disagreed for a
    while — the table called a v5 letter "ready" because it was clean, while
    the index flagged it as stale — and a folder that two of our own views
    describe differently is precisely how the wrong one gets sent.
    """
    return complete(generation) and generation.state == "clean" and current(generation)


def rank(generation: Generation) -> tuple:
    """Sort key for "which of these would I send". Larger is better.

    Completeness first, for the reason in the module docstring. Then the state
    the gates gave it, then the prompt version — a letter written by an older
    prompt carries whatever that prompt got wrong — then recency as the
    tie-break.
    """
    state_rank = {"clean": 3, "review": 2, "unsent": 1}.get(generation.state, 0)
    return (complete(generation), state_rank, _version(generation), generation.generated_at)


@dataclass
class TidyPlan:
    keep: list[Generation] = field(default_factory=list)
    supersede: list[Generation] = field(default_factory=list)
    #: Directories with no `provenance.json` — not applications.
    other: list[pathlib.Path] = field(default_factory=list)
    #: Runs an exported CSV points at. Left exactly where they are: the CSV
    #: holds their absolute paths, and "upload the file in column J" must still
    #: find it after a tidy.
    pinned: list[Generation] = field(default_factory=list)

    @property
    def jobs(self) -> int:
        return len(self.keep)


def build_plan(output_dir: pathlib.Path | str = "output", *,
               pinned: Iterable[str] = ()) -> TidyPlan:
    """What would move, without moving anything.

    `pinned` are application folders (paths) an exported CSV refers to; they
    are reported but never moved or archived.
    """
    root = pathlib.Path(output_dir)
    plan = TidyPlan()
    if not root.exists():
        return plan
    held = {str(pathlib.Path(p).resolve()) for p in pinned if p}

    archive = (root / ARCHIVE_DIR).resolve()
    sent = (root / SENT_DIR).resolve()

    by_job: dict[object, list[Generation]] = {}
    for generation in scan(root):
        # ARCHIVED RUNS ARE OUT OF SCOPE. `scan` looks two levels deep so the
        # Studio can see into the buckets, which means it also returns what is
        # already in the archive — and without this an archived run could win
        # its group and be promoted back out.
        if archive in generation.directory.resolve().parents:
            continue
        # SENT RUNS STAY SENT, for the same reason: a clean, current letter in
        # `4_already_sent/` would otherwise be filed back into "ready to send".
        if sent in generation.directory.resolve().parents:
            continue
        if str(generation.directory.resolve()) in held:
            plan.pinned.append(generation)
            continue
        # A run whose provenance carries no job id cannot be grouped with
        # anything, so it is its own group and always kept.
        key = generation.job_id or f"~{generation.name}"
        by_job.setdefault(key, []).append(generation)

    for group in by_job.values():
        best, *rest = sorted(group, key=rank, reverse=True)
        plan.keep.append(best)
        plan.supersede.extend(rest)
    plan.keep.sort(key=lambda g: (g.company.lower(), g.title.lower()))
    plan.supersede.sort(key=lambda g: g.generated_at)

    known = {g.directory.name for g in plan.keep + plan.supersede + plan.pinned}
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name in BUCKETS or directory.name in known:
            continue
        plan.other.append(directory)
    return plan


def bucket_for(generation: Generation) -> str:
    """Which of the three folders this run belongs in."""
    return READY_DIR if sendable(generation) else CHANGES_DIR


def apply_plan(
    plan: TidyPlan, output_dir: pathlib.Path | str = "output", *, move_other: bool = True,
) -> list[tuple[pathlib.Path, pathlib.Path]]:
    """File every run into its bucket. Returns `(from, to)` pairs.

    A run already sitting in the right bucket is left alone, so this is safe to
    run repeatedly and only reports what actually moved.

    A name already taken in the destination gets a numeric suffix rather than
    overwriting, because two runs of the same job on different days can produce
    the same directory name and losing one to the other would defeat the point
    of archiving instead of deleting.
    """
    root = pathlib.Path(output_dir)
    archive = root / ARCHIVE_DIR
    moves: list[tuple[pathlib.Path, pathlib.Path]] = []

    targets = [(g.directory, root / bucket_for(g)) for g in plan.keep]
    targets += [(g.directory, archive) for g in plan.supersede]
    if move_other:
        targets += [(d, archive / "_not_applications") for d in plan.other
                    if d.name not in NOT_APPLICATIONS]

    for source, into in targets:
        moved = move_into(source, into)
        if moved is not None:
            moves.append((source, moved))
    return moves


def move_into(source: pathlib.Path, into: pathlib.Path) -> pathlib.Path | None:
    """Move a directory into `into`, never overwriting. `None` if it did not move.

    The suffix rather than an overwrite because two runs of the same job on
    different days produce the same directory name, and losing one to the other
    would defeat the point of archiving instead of deleting.
    """
    if not source.exists() or source.parent == into:
        return None
    into.mkdir(parents=True, exist_ok=True)
    destination = into / source.name
    suffix = 2
    while destination.exists():
        destination = into / f"{source.name}__{suffix}"
        suffix += 1
    shutil.move(str(source), str(destination))
    return destination


def file_generation(
    directory: pathlib.Path, *, state: str, prompt_version: str, has_cv: bool,
    has_letter: bool, root: pathlib.Path | str = "output",
    job_id: int | str | None = None,
) -> pathlib.Path:
    """File one finished run into its bucket. Returns where it ended up.

    ONE FOLDER PER JOB. With a `job_id`, every other run of that job still in
    `1_ready_to_send/` or `2_needs_changes/` goes to the archive first. Without
    this, regenerating a job left the old run beside the new one — a batch of
    49 jobs produced 50 folders in `1_ready_to_send/`, two of them the same
    posting — which is the "which one do I send" problem the buckets exist
    to remove. The newest run wins, not the best-ranked: the Studio's
    application row points at the run it just made, and the folder must agree.

    Called at the very end of `generate_application`, once the letter's state is
    known — `packager.output_dir` claims the directory BEFORE the letter exists,
    deliberately, so that a letter the gates reject cannot take the CV with it.
    The bucket simply is not decidable at claim time.

    Judged from the values passed rather than by re-reading `provenance.json`,
    because the caller already holds them and a second read is a second thing
    that can disagree with the first. The rule itself is `sendable`'s, so a run
    filed here and a run filed by `apply tidy` land in the same place.
    """
    root = pathlib.Path(root)
    if directory.parent.name == SENT_DIR:
        return directory
    if job_id:
        supersede_job(root, job_id, keep=directory)
    ready = (
        has_cv and has_letter and state == "clean"
        and _version_of(prompt_version) >= _version_of(PROMPT_VERSION)
    )
    return move_into(directory, root / (READY_DIR if ready else CHANGES_DIR)) or directory


def file_sent(directory: pathlib.Path | str) -> pathlib.Path | None:
    """The application in `directory` went out: move it to `4_already_sent/`.

    Returns where it ended up, or `None` when nothing moved — the folder is
    gone, is already there, or sits in the archive (a superseded run is not the
    one that was sent). Never raises on a missing folder: marking a job applied
    must not fail because its documents were moved or deleted by hand.
    """
    directory = pathlib.Path(directory)
    if not directory.is_dir() or directory.parent.name in (ARCHIVE_DIR, SENT_DIR):
        return None
    root = directory.parent.parent if directory.parent.name in BUCKETS else directory.parent
    return move_into(directory, root / SENT_DIR)


def supersede_job(root: pathlib.Path | str, job_id: int | str,
                  keep: pathlib.Path) -> list[pathlib.Path]:
    """Archive every run of `job_id` in the live buckets except `keep`."""
    root = pathlib.Path(root)
    moved: list[pathlib.Path] = []
    for bucket in (READY_DIR, CHANGES_DIR):
        for generation in scan(root / bucket):
            if (str(generation.job_id) == str(job_id)
                    and generation.directory.resolve() != keep.resolve()):
                done = move_into(generation.directory, root / ARCHIVE_DIR)
                if done is not None:
                    moved.append(done)
    return moved


def _needs(generation: Generation) -> str:
    """What stands between this folder and being sent, in a few words."""
    problems: list[str] = []
    if not generation.cv_pdf:
        problems.append("no CV")
    if not generation.letter_path:
        problems.append("no letter")
    if generation.state == "unsent":
        problems.append("a claim the profile cannot support")
    elif generation.state == "review":
        problems.append("read REVIEW.md first")
    if _version(generation) and not current(generation):
        problems.append(f"written by {generation.prompt_version}, now {PROMPT_VERSION} — regenerate")
    return "; ".join(problems)


def _version_of(value: str) -> int:
    raw = (value or "").strip().lstrip("vV")
    return int(raw) if raw.isdigit() else 0


def archived(output_dir: pathlib.Path | str = "output") -> list[Generation]:
    """What is already in the archive, oldest first.

    Read from the archive rather than taken from `TidyPlan.supersede`, because
    the index is written AFTER the move and by then `build_plan` — which scans
    one level — no longer sees them. The first version reported "0 older runs"
    on the same call that moved nine.
    """
    return sorted(scan(pathlib.Path(output_dir) / ARCHIVE_DIR),
                  key=lambda g: g.generated_at)


def index_markdown(plan: TidyPlan, *, output_dir: pathlib.Path | str = "output") -> str:
    """`output/README.md` — one row per job, readiness first.

    Written for the question actually being asked when this folder is opened:
    which of these can I send right now, and what is wrong with the rest.
    """
    # Pinned runs are applications too — held in place for the CSV, not
    # hidden. Leaving them out made a batch of 49 exported jobs index as
    # "2 job(s) · 0 ready to send".
    listed = plan.keep + [g for g in plan.pinned
                          if g.directory.parent.name != ARCHIVE_DIR]
    ready = [g for g in listed if sendable(g)]
    older = plan.supersede or archived(output_dir)
    lines = [
        "# Applications",
        "",
        f"{len(listed)} job(s) · **{len(ready)} ready to send** · "
        f"{len(older)} older run(s) in `{ARCHIVE_DIR}/`",
        "",
        f"- `{READY_DIR}/` — complete, every gate passed, written by the current "
        f"prompt ({PROMPT_VERSION}). Send these.",
        f"- `{CHANGES_DIR}/` — the best run for a job that is not sendable yet. "
        f"The last column says why.",
        f"- `{ARCHIVE_DIR}/` — superseded runs, kept.",
        f"- `{SENT_DIR}/` — applications marked applied. Nothing left to do.",
        "",
        "Regenerate with `pitwall apply generate --job <id>`, or from the "
        "Applications Studio. Rerun `pitwall apply tidy` to refile and rebuild "
        "this file.",
        "",
    ]

    for folder, group in (
        (READY_DIR, ready),
        (CHANGES_DIR, [g for g in listed if not sendable(g)]),
    ):
        if not group:
            continue
        lines += [
            f"## `{folder}/`", "",
            "| | job | company | role | made | letter | what it needs |",
            "|---|---|---|---|---|---|---|",
        ]
        for generation in sorted(group, key=lambda g: g.company.lower()):
            needs = _needs(generation)
            mark = "✅" if not needs else ("⛔" if generation.state == "unsent" else "⚠️")
            letter = generation.letter_path.name if generation.letter_path else "—"
            lines.append(
                f"| {mark} | `{generation.job_id or '—'}` | {generation.company} | "
                f"{generation.title} | {generation.when} | `{letter}` | "
                f"{needs or 'nothing'} |"
            )
        lines.append("")

    if plan.other or any(g.directory.parent == pathlib.Path(output_dir) for g in plan.keep):
        loose = [g for g in plan.keep if g.directory.parent == pathlib.Path(output_dir)]
        if loose:
            lines += [
                "## Not filed yet", "",
                "Generated since the last `pitwall apply tidy`. Run it to file them.",
                "",
            ]
            lines += [f"- `{g.name}` — {g.company}" for g in loose]
            lines.append("")

    if older:
        lines += [
            f"## `{ARCHIVE_DIR}/`", "",
            "Kept, not deleted — they are the record of how the letters got better, "
            "and their grades in `run/voice_eval.csv` still point at them by name.",
            "",
        ]
        for generation in older:
            lines.append(
                f"- `{generation.name}` — {generation.company}, {generation.prompt_version}, "
                f"{generation.when}, {generation.state or 'no letter'}"
            )

    if plan.other:
        lines += ["", "## Not applications", "",
                  "No `provenance.json`, so nothing here was a generated "
                  "application.", ""]
        for directory in plan.other:
            why = (" — loose CVs from `apply preview`, rewritten on each run"
                   if directory.name in NOT_APPLICATIONS else "")
            lines.append(f"- `{directory.name}`{why}")
    return "\n".join(lines) + "\n"
