"""One folder per job in `output/`, and an index that cannot contradict itself.

The risk this defends against is narrow and expensive: six folders for one
posting, named alike, and the wrong one gets sent.
"""

from __future__ import annotations

import json

import pytest

from pitwall.apply.letter import PROMPT_VERSION
from pitwall.apply.tidy import (
    ARCHIVE_DIR,
    CHANGES_DIR,
    READY_DIR,
    apply_plan,
    build_plan,
    index_markdown,
    rank,
    sendable,
)


def make(root, name, *, job_id=1, version=PROMPT_VERSION, state="clean",
         cv=True, letter=True, company="Predium", title="Data Scientist",
         when="2026-09-25T12:00:00+00:00"):
    """A directory shaped like a real run: provenance plus the files it names."""
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "provenance.json").write_text(json.dumps({
        "generated_at": when,
        "job": {"id": job_id, "company": company, "title": title,
                "url": "", "prompt_version": version},
        "letter": {"state": state},
    }), encoding="utf-8")
    if cv:
        (directory / f"Simon_Sazonov_{name}_CV.pdf").write_text("pdf", encoding="utf-8")
    if letter:
        suffix = {"clean": "_Cover_Letter.txt",
                  "review": "_Cover_Letter_NEEDS_REVIEW.txt",
                  "unsent": "_Cover_Letter_DO_NOT_SEND.txt"}[state]
        (directory / f"Simon_Sazonov_{name}{suffix}").write_text("body", encoding="utf-8")
    return directory


def test_each_job_lands_in_one_of_three_folders(tmp_path):
    """Opening `output/` should answer "what can I send" before anything is
    read: the best run per job goes to ready or needs-changes, the rest to the
    archive."""
    out = tmp_path / "output"
    make(out, "predium_a", job_id=5499, state="review", when="2026-09-25T10:00:00+00:00")
    make(out, "predium_b", job_id=5499, state="clean", when="2026-09-25T18:00:00+00:00")
    make(out, "helsing", job_id=13, state="review")

    plan = build_plan(out)
    assert {g.job_id for g in plan.keep} == {5499, 13}
    assert [g.name for g in plan.supersede] == ["predium_a"]

    apply_plan(plan, out)
    assert (out / READY_DIR / "predium_b").is_dir()
    assert (out / CHANGES_DIR / "helsing").is_dir()
    # Moved, not deleted — the older letters are how the prompt got better.
    assert (out / ARCHIVE_DIR / "predium_a").is_dir()
    assert not (out / "predium_a").exists()


def test_running_it_twice_moves_nothing_the_second_time(tmp_path):
    out = tmp_path / "output"
    make(out, "a", job_id=1, state="clean")
    make(out, "b", job_id=1, state="review", when="2026-09-20T10:00:00+00:00")

    assert apply_plan(build_plan(out), out)          # first run files them
    assert apply_plan(build_plan(out), out) == []    # second finds nothing to do
    assert (out / READY_DIR / "a").is_dir()
    assert (out / ARCHIVE_DIR / "b").is_dir()


def test_an_archived_run_is_never_promoted_back_out(tmp_path):
    """`scan` looks two levels deep so the Studio can see into the buckets,
    which means it also returns what is already archived. Without an explicit
    exclusion the archived run wins its group again and comes back."""
    out = tmp_path / "output"
    make(out, "good", job_id=1, version=PROMPT_VERSION, state="clean")
    make(out / ARCHIVE_DIR, "banished", job_id=1, version=PROMPT_VERSION, state="clean",
         when="2030-01-01T00:00:00+00:00")   # newer, and would otherwise win

    plan = build_plan(out)
    assert [g.name for g in plan.keep] == ["good"]
    assert (out / ARCHIVE_DIR / "banished").is_dir()


def test_a_letter_with_no_cv_never_beats_a_complete_application(tmp_path):
    """Five of the six real Predium runs were `clean` and had no CV, because
    they were generated with --no-cv. A clean letter is not an application."""
    out = tmp_path / "output"
    letter_only = make(out, "clean_no_cv", job_id=7, state="clean", cv=False)
    whole = make(out, "review_with_cv", job_id=7, state="review", cv=True)

    plan = build_plan(out)
    assert [g.directory for g in plan.keep] == [whole]
    assert [g.directory for g in plan.supersede] == [letter_only]


def test_a_letter_from_an_older_prompt_is_not_called_ready(tmp_path):
    """It passed every gate its prompt knew about. Every letter written before
    v8 still carries 16-37 non-breaking hyphens that break an ATS search."""
    out = tmp_path / "output"
    make(out, "old", job_id=1, version="v5", state="clean")
    make(out, "new", job_id=2, version=PROMPT_VERSION, state="clean")

    by_name = {g.name: g for g in build_plan(out).keep}
    assert not sendable(by_name["old"])
    assert sendable(by_name["new"])
    assert "regenerate" in index_markdown(build_plan(out))


def test_the_table_and_the_index_cannot_disagree(tmp_path):
    """They did, for a while: the table called a v5 clean letter "ready" while
    the index flagged it stale. Two of our own views describing one folder
    differently is how the wrong one gets sent."""
    out = tmp_path / "output"
    make(out, "old", job_id=1, version="v5", state="clean")
    plan = build_plan(out)
    text = index_markdown(plan)
    assert "**0 ready to send**" in text
    assert not any(sendable(g) for g in plan.keep)


def test_nothing_is_overwritten_when_two_runs_share_a_name(tmp_path):
    out = tmp_path / "output"
    (out / ARCHIVE_DIR).mkdir(parents=True)
    (out / ARCHIVE_DIR / "dup").mkdir()
    (out / ARCHIVE_DIR / "dup" / "keep.txt").write_text("the earlier one", encoding="utf-8")
    make(out, "dup", job_id=9, state="review", when="2026-09-20T10:00:00+00:00")
    make(out, "better", job_id=9, state="clean", when="2026-09-26T10:00:00+00:00")

    apply_plan(build_plan(out), out)
    assert (out / ARCHIVE_DIR / "dup" / "keep.txt").read_text(encoding="utf-8") == "the earlier one"
    assert (out / ARCHIVE_DIR / "dup__2").is_dir()


def test_a_directory_that_was_never_a_run_is_named_not_archived_silently(tmp_path):
    out = tmp_path / "output"
    make(out, "real", job_id=1)
    (out / "stage3_scratch").mkdir()

    plan = build_plan(out)
    assert [d.name for d in plan.other] == ["stage3_scratch"]
    assert "Not applications" in index_markdown(plan)


def test_the_studio_still_sees_every_run_after_they_are_filed(tmp_path):
    """The folder is for sending; the Studio's history is for grading, and a
    grade belongs to the run that earned it. `history.scan` therefore looks two
    levels deep — one level would have emptied the history the first time
    anything was tidied."""
    from pitwall.apply.history import scan

    out = tmp_path / "output"
    make(out, "old", job_id=1, version="v5", state="review")
    make(out, "new", job_id=1, version=PROMPT_VERSION, state="clean")
    apply_plan(build_plan(out), out)
    assert sorted(g.name for g in scan(out)) == ["new", "old"]


def test_a_freshly_generated_run_is_found_before_it_is_filed(tmp_path):
    """`packager.output_dir` claims its directory before the letter exists, so
    a new run lands at the top level and is not in a bucket yet."""
    from pitwall.apply.history import scan

    out = tmp_path / "output"
    make(out, "filed", job_id=1, state="clean")
    apply_plan(build_plan(out), out)
    make(out, "brand_new", job_id=2, state="clean")

    assert sorted(g.name for g in scan(out)) == ["brand_new", "filed"]
    assert {g.name for g in build_plan(out).keep} == {"brand_new", "filed"}


@pytest.mark.parametrize("worse,better", [
    ({"state": "unsent"}, {"state": "review"}),
    ({"state": "review"}, {"state": "clean"}),
    ({"version": "v5"}, {"version": "v8"}),
    ({"cv": False}, {"cv": True}),
])
def test_the_ranking_prefers_what_a_person_would_send(tmp_path, worse, better):
    out = tmp_path / "output"
    a = make(out, "worse", job_id=1, **{"state": "clean", "version": PROMPT_VERSION, **worse})
    b = make(out, "better", job_id=2, **{"state": "clean", "version": PROMPT_VERSION, **better})
    found = {g.directory: g for g in build_plan(out).keep}
    assert rank(found[b]) > rank(found[a])


def test_the_index_counts_the_archive_after_the_move_not_before(tmp_path):
    """Written after the move, so `build_plan` — which scans one level — no
    longer sees the superseded runs. The first version reported "0 older runs"
    on the same call that moved nine."""
    out = tmp_path / "output"
    make(out, "old", job_id=1, version="v5", state="review")
    make(out, "new", job_id=1, version=PROMPT_VERSION, state="clean")
    apply_plan(build_plan(out), out)

    text = index_markdown(build_plan(out), output_dir=out)
    assert "1 older run(s)" in text
    assert "`old`" in text


# ── filed on the way out, not later ───────────────────────────────────────────


def test_a_finished_run_files_itself(tmp_path):
    """The Studio saved into `output/` and left it there, so a folder sat at
    the top level until someone remembered `apply tidy`. That pile is the thing
    the buckets exist to remove."""
    from pitwall.apply.tidy import file_generation

    out = tmp_path / "output"
    loose = out / "Acme_Data_Scientist"
    loose.mkdir(parents=True)

    filed = file_generation(
        loose, state="clean", prompt_version=PROMPT_VERSION,
        has_cv=True, has_letter=True, root=out,
    )
    assert filed == out / READY_DIR / "Acme_Data_Scientist"
    assert filed.is_dir() and not loose.exists()


@pytest.mark.parametrize("state,cv,version,bucket", [
    ("clean", True, PROMPT_VERSION, READY_DIR),
    ("review", True, PROMPT_VERSION, CHANGES_DIR),
    ("unsent", True, PROMPT_VERSION, CHANGES_DIR),
    ("clean", False, PROMPT_VERSION, CHANGES_DIR),   # a letter with no CV
    ("clean", True, "v5", CHANGES_DIR),              # written by an old prompt
])
def test_filing_follows_the_same_rule_as_tidy(tmp_path, state, cv, version, bucket):
    """One rule, two callers. If `file_generation` and `sendable` ever drift, a
    run lands in `1_ready_to_send/` and the index calls it unsendable."""
    from pitwall.apply.tidy import file_generation

    out = tmp_path / "output"
    loose = out / "Acme_Role"
    loose.mkdir(parents=True)
    filed = file_generation(
        loose, state=state, prompt_version=version,
        has_cv=cv, has_letter=True, root=out,
    )
    assert filed.parent.name == bucket


def test_filing_never_overwrites_an_earlier_run_of_the_same_job(tmp_path):
    from pitwall.apply.tidy import file_generation

    out = tmp_path / "output"
    (out / READY_DIR / "Acme_Role").mkdir(parents=True)
    (out / READY_DIR / "Acme_Role" / "first.txt").write_text("keep me", encoding="utf-8")
    loose = out / "Acme_Role"
    loose.mkdir(parents=True, exist_ok=True)

    filed = file_generation(
        loose, state="clean", prompt_version=PROMPT_VERSION,
        has_cv=True, has_letter=True, root=out,
    )
    assert filed.name == "Acme_Role__2"
    assert (out / READY_DIR / "Acme_Role" / "first.txt").read_text(encoding="utf-8") == "keep me"


def test_a_run_already_in_its_bucket_is_left_alone(tmp_path):
    from pitwall.apply.tidy import file_generation

    out = tmp_path / "output"
    settled = out / READY_DIR / "Acme_Role"
    settled.mkdir(parents=True)
    filed = file_generation(
        settled, state="clean", prompt_version=PROMPT_VERSION,
        has_cv=True, has_letter=True, root=out,
    )
    assert filed == settled


def test_two_runs_of_one_job_in_different_buckets_get_distinct_widget_keys(tmp_path):
    """Streamlit refuses a duplicate widget key and takes the whole page down
    with it. `apply tidy` makes same-named folders in two buckets ordinary, so
    the Studio cannot key on the directory name."""
    from pitwall.apply.history import scan

    out = tmp_path / "output"
    make(out / READY_DIR, "Acme_Role", job_id=1, state="clean")
    make(out / ARCHIVE_DIR, "Acme_Role", job_id=1, state="review")

    generations = scan(out)
    assert len(generations) == 2
    assert len({g.key for g in generations}) == 2
    assert len({g.name for g in generations}) == 1   # the collision itself
