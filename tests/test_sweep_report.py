"""The sweep report — §1.5's "record the result".

Coverage before precision. v1 had searched 7 of 49 companies, so the next volume
of leads was in the 42 nobody had looked at, not in a better threshold. This is
the one command that says which of those two you are actually short of.
"""

from __future__ import annotations

from pitwall.export import sweep_report
from pitwall.models import Company, Role
from pitwall.pipeline import RunParams


def test_an_empty_database_reports_zero_not_an_error(repos, settings):
    report = sweep_report(repos.conn, settings)
    assert report["companies_searched"] == 0
    assert report["candidates"] == 0
    assert report["templates_fired"] == []


def test_the_report_counts_what_a_run_actually_covered(pipeline, repos, settings):
    pipeline.run(RunParams(max_queries=4, max_results=10, use_llm=False))
    report = sweep_report(repos.conn, settings)

    assert report["companies_active"] == 1
    assert report["companies_searched"] == 1
    assert report["companies_with_people"] == 1
    assert report["queries_done"] == 4
    assert report["serp_rows"] > 0
    assert report["candidates"] > 0


def test_unsearched_companies_are_visible_as_the_gap(pipeline, repos, settings, write_inputs):
    """The whole point: 7 of 49 is a coverage problem, not a scoring one."""
    write_inputs(
        [
            Company(name="McLaren Racing", aliases="McLaren F1", tier=1),
            Company(name="Never Searched Ltd", tier=3),
            Company(name="Also Never Searched", tier=3),
        ],
        [Role(title="Data Scientist", family="data_science", priority=1)],
    )
    pipeline.run(RunParams(companies=["McLaren Racing"], max_queries=2, use_llm=False))
    report = sweep_report(repos.conn, settings)
    assert report["companies_active"] == 3
    assert report["companies_searched"] == 1


def test_the_templates_that_fired_are_reported(pipeline, repos, settings):
    pipeline.run(RunParams(max_queries=4, use_llm=False))
    fired = sweep_report(repos.conn, settings)["templates_fired"]
    assert fired == sorted(fired)
    assert all(isinstance(t, str) for t in fired)
    assert fired, "a completed run fired at least one template"


def test_the_extraction_gaps_are_counted(pipeline, repos, settings):
    pipeline.run(RunParams(max_queries=4, use_llm=False))
    gaps = sweep_report(repos.conn, settings)["extraction"]
    assert set(gaps) == {"no_company", "no_title", "no_location", "single_source"}
    total = sweep_report(repos.conn, settings)["candidates"]
    assert all(0 <= v <= total for v in gaps.values())


def test_the_verdict_spread_is_included(pipeline, repos, settings):
    pipeline.run(RunParams(max_queries=4, use_llm=False))
    verdicts = sweep_report(repos.conn, settings)["verdicts"]
    assert sum(verdicts.values()) > 0
