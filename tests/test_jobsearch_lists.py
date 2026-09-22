"""Your own company list, the motorsport list, and the AI's reading of your CV.

The owner asked for three things after the first build: to upload their own
company list, to filter the inbox by list (all / mine / motorsport), and for
the AI to judge fit from their actual CV. These pin each of them down.
"""

# The pipeline fixtures are imported by name, and pytest injects them as
# arguments of the same name — which is exactly what F811 reports.
# ruff: noqa: F811

from __future__ import annotations

import json
from types import SimpleNamespace

from pitwall.jobsearch.companies import TargetCompany, load_targets
from pitwall.jobsearch.importer import merge, parse_upload
from pitwall.jobsearch.store import load_inbox
from pitwall.sources.jobs import ATSSource, JobPosting

from .test_jobsearch_pipeline import Stub, arbeitnow_postings, js_project, search  # noqa: F401

# ─────────────────────────────── uploading ───────────────────────────────


def test_a_plain_list_one_company_per_line_is_a_valid_upload():
    companies, problems = parse_upload(b"Mistral AI\nBMW Group\n\nDeepL\n")
    assert [c.name for c in companies] == ["Mistral AI", "BMW Group", "DeepL"]
    assert problems == []


def test_friendly_column_names_are_understood():
    data = b"Company,City,Priority,Sector\nPicnic,Amsterdam,A,Logistics\n"
    [picnic], problems = parse_upload(data)
    assert (picnic.name, picnic.cities, picnic.priority, picnic.industry) == \
        ("Picnic", ["Amsterdam"], "A", "Logistics")


def test_bad_values_are_reported_not_imported_silently():
    _, problems = parse_upload(b"name,priority\nPicnic,Z\n")
    assert any("priority" in p for p in problems)


def test_adding_keeps_what_you_set_and_only_fills_blanks():
    mine = [TargetCompany(name="Picnic", priority="A", industry="Logistics")]
    upload = [TargetCompany(name="Picnic B.V.", priority="C", country="NL"),
              TargetCompany(name="Wolt")]
    result = merge(mine, upload)
    picnic = result.companies[0]
    assert picnic.priority == "A" and picnic.country == "NL"
    assert result.added == ["Wolt"] and result.updated == ["Picnic"]


def test_replacing_keeps_a_discovered_board():
    mine = [TargetCompany(name="Wolt", ats_kind="greenhouse", ats_token="wolt")]
    result = merge(mine, [TargetCompany(name="Wolt"), TargetCompany(name="DeepL")],
                   replace=True)
    assert [c.name for c in result.companies] == ["Wolt", "DeepL"]
    assert result.companies[0].ats_token == "wolt"


# ──────────────────────────── which list (filter) ────────────────────────


def test_every_vacancy_knows_which_of_your_lists_it_is_on(search, write_inputs):
    from pitwall.inputs import load_and_sync
    from pitwall.models import Company, Role

    write_inputs([Company(name="Prema Racing", tier=1)],
                 [Role(title="Data Scientist", family="data_science")])
    load_and_sync(search.conn)
    postings = [
        JobPosting(title="Data Scientist", url="https://a.test/1", company_name="Apex Mobility",
                   location="Munich", description="Python and ML."),
        JobPosting(title="Data Scientist", url="https://a.test/2", company_name="Prema Racing",
                   location="Munich", description="Python and ML."),
        JobPosting(title="Data Scientist", url="https://a.test/3", company_name="Nobody GmbH",
                   location="Munich", description="Python and ML."),
    ]
    search.run(sources=[Stub("arbeitnow", "job_board", postings)])
    flags = {c["company"]: (c["on_target_list"], c["on_people_list"])
             for c in load_inbox(search.conn).view("all")}
    assert flags["Apex Mobility"] == (True, False)
    assert flags["Prema Racing"] == (False, True)
    assert flags["Nobody GmbH"] == (False, False)


def test_motorsport_teams_with_a_known_board_are_searched_too(search, write_inputs):
    from pitwall.inputs import load_and_sync
    from pitwall.jobsearch.companies import TargetIndex, target_rows
    from pitwall.jobsearch.run import JobSearchStats
    from pitwall.models import Company, Role

    write_inputs([Company(name="Prema Racing", tier=1), Company(name="AF Corse", tier=1)],
                 [Role(title="Data Scientist", family="data_science")])
    load_and_sync(search.conn)
    search.conn.execute("UPDATE companies SET ats_kind='lever', ats_token='prema'"
                        " WHERE name='Prema Racing'")
    search.cfg.sources.people_list_ats = True
    boards = [s for s in search.build_sources(TargetIndex(target_rows(search.conn)),
                                              JobSearchStats())
              if isinstance(s, ATSSource)]
    assert [(b.token, b.company_name) for b in boards] == [("prema", "Prema Racing")]
    assert boards[0].company_id is not None, "linked, so Openings sees its postings too"

    search.cfg.sources.people_list_ats = False
    assert not [s for s in search.build_sources(TargetIndex(target_rows(search.conn)),
                                                JobSearchStats()) if isinstance(s, ATSSource)]


# ─────────────────────────── the CV the AI reads ─────────────────────────


def test_your_own_cv_file_wins_over_the_template(js_project, settings):
    from pitwall.scoring.jobs import load_profile

    config = js_project / "config"
    (config / "profile.md").write_text("<!-- template -->\n# Target role\n", encoding="utf-8")
    assert load_profile(settings) is None, "the template alone is not a profile"
    (config / "my_profile.md").write_text("# What I am looking for\nGraduate ML roles.\n",
                                          encoding="utf-8")
    assert "Graduate ML roles" in load_profile(settings)


def test_the_ai_reads_only_the_best_passing_vacancies_and_its_answer_is_kept(
    js_project, search, monkeypatch,
):
    from pitwall.models import JobRelevance

    (js_project / "config" / "my_profile.md").write_text("MSc student, ML.", encoding="utf-8")
    search.run(sources=[Stub("arbeitnow", "job_board", arbeitnow_postings())])
    seen: list[list[dict]] = []

    class FakeScorer:
        provider, model = "fake", "fake-1"

        def score_jobs(self, profile, jobs):
            seen.append(jobs)
            assert profile == "MSc student, ML."
            return SimpleNamespace(
                provider_error=False, error=None, model="fake-1",
                verdicts=[JobRelevance(job_id=j["id"], score=77, reasons=["fits"],
                                       red_flags=["relocation"]) for j in jobs],
            )

    monkeypatch.setattr(search.pipe, "_open_scorer", lambda stats: FakeScorer())
    monkeypatch.setattr(search.pipe, "_finish_llm", lambda scorer, stats: stats)
    stats = search.run(sources=[], ai_fit=5, poll=False)
    sent = [j["title"] for batch in seen for j in batch]
    assert len(sent) == 2 and "Senior ML Engineer" not in sent, "only jobs that passed"
    assert all("phone" not in json.dumps(batch).lower() for batch in seen)
    passed = load_inbox(search.conn).view("matches")
    assert stats.ai_scored == 2
    assert all(c["ai_score"] == 77 and "⚠ relocation" in c["ai_reasons"] for c in passed)


def test_no_cv_means_no_ai_call_and_a_note_saying_why(search, monkeypatch):
    search.run(sources=[Stub("arbeitnow", "job_board", arbeitnow_postings())])
    called = []
    monkeypatch.setattr(search.pipe, "_open_scorer", lambda stats: called.append(1))
    stats = search.run(poll=False, ai_fit=5)
    assert not called and any("my_profile" in n or "profile.md" in n for n in stats.notes)
    assert load_targets()[0], "the target list is untouched by all this"
