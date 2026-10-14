"""The CSV you work through when applying.

One row per application, in export order, with the apply link and the
absolute path of every file to upload. The paths must still be true after
`apply tidy` runs — that is the whole point of pointing at files rather than
copying them.
"""

from __future__ import annotations

import csv
import io
import json
import pathlib

import pytest

from pitwall.jobsearch import tracker
from pitwall.jobsearch.export import BATCH_COLUMNS, batch_csv, write_batch_csv
from pitwall.jobsearch.store import load_inbox

from .test_jobsearch_pipeline import (  # noqa: F401, TID252
    Stub,
    arbeitnow_postings,
    js_project,
    search,
)


def _folder(root: pathlib.Path, name: str, job_id: int, *, state="clean") -> pathlib.Path:
    folder = root / "1_ready_to_send" / name
    folder.mkdir(parents=True)
    (folder / "provenance.json").write_text(json.dumps({
        "generated_at": "2026-09-29T09:00:00+00:00",
        "job": {"id": job_id, "company": "Apex", "title": name, "prompt_version": "v8"},
        "letter": {"state": state},
    }), encoding="utf-8")
    for suffix in ("_CV.pdf", "_CV.docx", "_Cover_Letter.txt", "_Cover_Letter.pdf"):
        (folder / f"Simon_Sazonov_{name}{suffix}").write_bytes(b"x")
    return folder


@pytest.fixture()
def batch(search, tmp_path):  # noqa: F811 - the imported fixture, requested by name
    search.run(sources=[Stub("arbeitnow", "job_board", arbeitnow_postings())])
    conn = search.conn
    ranked = sorted(load_inbox(conn).view("all"), key=lambda c: -(c["score"] or 0))[:2]
    out = tmp_path / "output"
    apps = []
    for i, job in enumerate(ranked):
        folder = _folder(out, f"Job_{i}", int(job["id"]))
        apps.append(tracker.record_generation(conn, job_id=int(job["id"]),
                                              directory=str(folder.resolve()),
                                              letter_state="clean"))
    batch_id = tracker.create_export_batch(conn, apps, name="2026-09-29 batch 1")
    return conn, batch_id, apps, out, ranked


def _rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def test_one_row_per_application_in_export_order_with_every_column(batch):
    conn, batch_id, apps, _, ranked = batch
    rows = _rows(batch_csv(conn, batch_id))
    assert list(rows[0]) == list(BATCH_COLUMNS)
    assert [r["#"] for r in rows] == ["1", "2"]
    assert [int(r["application_id"]) for r in rows] == apps
    assert rows[0]["apply_link"] == ranked[0]["url"]
    assert rows[0]["status"] == "IN_PROCESS"
    assert float(rows[0]["score"]) == pytest.approx(ranked[0]["score"], abs=0.05)
    assert rows[0]["why_matched"] and not rows[0]["why_matched"].startswith("+")


def test_every_file_is_an_absolute_path_that_exists(batch):
    conn, batch_id, *_ = batch
    for row in _rows(batch_csv(conn, batch_id)):
        for column in ("cv_pdf", "letter_pdf", "letter_txt", "cv_docx", "folder"):
            path = pathlib.Path(row[column])
            assert path.is_absolute() and path.exists(), (column, row[column])
        assert row["letter_state"] == "clean"


def test_the_csv_is_written_with_a_bom_and_recorded_on_the_batch(batch, tmp_path):
    conn, batch_id, *_ = batch
    target = write_batch_csv(conn, batch_id, directory=str(tmp_path / "exports"))
    assert target.name == "2026_09_29_Batch_1.csv"
    assert target.read_bytes().startswith(b"\xef\xbb\xbf"), "Excel needs the BOM for accents"
    assert tracker.batches(conn)[0]["csv_path"] == str(target)


def test_the_paths_are_still_true_after_apply_tidy(batch):
    from pitwall.apply.tidy import apply_plan, build_plan

    conn, batch_id, _, out, ranked = batch
    # A newer run of the first job arrives; normally the older one is archived.
    _folder(out.parent / "newer", "Job_0", int(ranked[0]["id"]))
    (out.parent / "newer" / "1_ready_to_send" / "Job_0").rename(out / "Job_0_v2")
    plan = build_plan(out, pinned=tracker.exported_dirs(conn))
    apply_plan(plan, out)
    for row in _rows(batch_csv(conn, batch_id)):
        assert pathlib.Path(row["cv_pdf"]).exists(), "tidy moved a file the CSV points at"


def test_a_mycareersfuture_posting_is_flagged_in_concerns():
    """MyCareersFuture applies through Singpass only, so its link is a dead end
    from abroad; the CSV says so before anyone clicks it."""
    from pitwall.jobsearch.export import _route_concern

    mcf = "https://www.mycareersfuture.gov.sg/job/engineering/data-scientist-x-1"
    assert "Singpass" in _route_concern(mcf, mcf)
    assert "company's own route" in _route_concern(mcf, "https://agilent.wd5.myworkdayjobs.com/x")
    assert _route_concern("https://job-boards.greenhouse.io/anthropic/jobs/1", "") == ""
