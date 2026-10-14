"""The note to a person at a company you applied to — and the links to find them.

Drafts only: a test asserts there is no way to send one. The gates are the
letter's plus the three a public note adds (length, contact details, claiming
an application before it is one).
"""

from __future__ import annotations

import json
import pathlib
from urllib.parse import parse_qs, urlparse

import pytest

from pitwall.apply import llm as outreach_llm
from pitwall.apply import outreach
from pitwall.apply.llm import CAPABLE_ONLY, TASK_ROUTING, TaskRouter
from pitwall.apply.loader import load_profile
from pitwall.apply.outreach import (
    LIMIT,
    check_note,
    draft_note,
    note_facts,
    notes_for,
    save_note,
)
from pitwall.apply.truth import build_truth
from pitwall.jobsearch.linkedin_links import people_search, presets

from .test_apply_pipeline import StubClient  # noqa: TID252

PERSON = {"full_name": "Rita Recruits", "headline": "Technical Recruiter at Apex",
          "profile_url": "https://www.linkedin.com/in/rita-recruits", "group": "recruiter"}
APP = {"id": 1, "company_name": "Apex", "title": "ML Engineer", "status": "IN_PROCESS"}
GOOD = "Hi Rita — I'm interested in the ML Engineer role at Apex. I built an RL environment for ICU sepsis treatment; glad to connect."


@pytest.fixture(scope="module")
def profile():
    return load_profile()


# ── the gates ──


def test_a_good_note_passes(profile):
    assert len(GOOD) <= LIMIT
    assert check_note(GOOD, truth=build_truth(profile)) == []


@pytest.mark.parametrize("text, failure", [
    ("x" * 201, "too long"),
    ("Hi Rita, write to me at simon@example.com", "contact details"),
    ("Hi Rita, see https://github.com/me", "contact details"),
    ("Hi Rita, I've applied for the ML role.", "says you applied"),
    ("", "empty"),
])
def test_each_gate_names_what_is_wrong(text, failure):
    assert any(failure in f for f in check_note(text))


def test_saying_you_applied_is_fine_once_you_have():
    assert check_note("Hi Rita, I've applied for the ML role.", applied=True) == []


def test_banned_phrases_are_caught_whatever_the_spacing():
    assert check_note("I am  passionate   about ML", banned=["passionate about"]) == \
        ["banned phrase: passionate about"]


# ── drafting ──


def _router(*replies):
    client = StubClient(*replies)
    return TaskRouter(clients={"groq": client}), client


def test_two_clean_variants_come_back_in_one_call(profile):
    router, client = _router({"variants": [{"text": GOOD}, {"text": GOOD.replace("Hi", "Hello")}]})
    draft = draft_note(person=PERSON, application=APP, router=router, profile=profile)
    assert draft.variants == [GOOD, GOOD.replace("Hi", "Hello")]
    assert draft.checks == [[], []]
    assert draft.attempts == 1 and len(client.prompts) == 1
    prompt = client.prompts[0]
    assert "Rita Recruits" in prompt and "applied: no" in prompt and "recruiter" in prompt


def test_a_failing_draft_is_retried_once_with_the_rule_named(profile):
    long = "Hi Rita, " + "really " * 40
    router, client = _router({"variants": [{"text": long}, {"text": GOOD}]},
                             {"variants": [{"text": GOOD}, {"text": GOOD}]})
    draft = draft_note(person=PERSON, application=APP, router=router, profile=profile)
    assert draft.attempts == 2 and draft.checks == [[], []]
    assert "too long" in client.prompts[1]


def test_a_draft_that_still_fails_comes_back_with_its_failures(profile):
    router, _ = _router({"variants": [{"text": "x" * 300}, {"text": GOOD}]})
    draft = draft_note(person=PERSON, application=APP, router=router, profile=profile)
    assert draft.attempts == 2
    assert draft.checks[0] and "too long" in draft.checks[0][0]


def test_the_facts_are_the_ones_the_letter_was_planned_on(profile, tmp_path):
    first = profile.evidence[-1]
    (tmp_path / "provenance.json").write_text(json.dumps(
        {"narrative_plan": {"evidence_order": [first.id]}}), encoding="utf-8")
    facts = note_facts(profile, {"generation_dir": str(tmp_path)})
    assert facts[0] == (first.one_line_hook or first.facts[0]).strip()
    assert note_facts(profile, {}), "without a plan, the strongest evidence stands in"


def test_notes_are_prose_so_they_never_fall_back_to_a_small_local_model():
    assert TASK_ROUTING["outreach_note"] == ("groq",)
    assert "outreach_note" in CAPABLE_ONLY


def test_there_is_no_way_to_send_a_note():
    source = pathlib.Path(outreach.__file__).read_text(encoding="utf-8")
    for needle in ("smtplib", "requests.post", "httpx.post", "voyager", "def send"):
        assert needle not in source, needle


# ── storage ──


def test_saving_again_replaces_the_note_for_that_person(repos):
    conn = repos.conn
    conn.execute("INSERT INTO applications (id, company_name, title) VALUES (1, 'Apex', 'ML')")
    first = save_note(conn, 1, PERSON, "one", model="groq/x", tokens=100)
    again = save_note(conn, 1, PERSON, "two", edited=True, tokens=50)
    assert first == again
    [note] = notes_for(conn, 1)
    assert (note["text"], note["edited"], note["model"], note["tokens"]) == \
        ("two", 1, "groq/x", 150)


# ── the links ──


def test_a_people_search_link_carries_its_words_and_nothing_else():
    url = people_search("Apex  recruiter")
    parsed = urlparse(url)
    assert parsed.netloc == "www.linkedin.com" and parsed.path == "/search/results/people/"
    assert parse_qs(parsed.query)["keywords"] == ["Apex recruiter"]


def test_presets_cover_every_group(profile):
    rows = presets("Anthropic", "Research Engineer, Code RL (Reinforcement Learning)",
                   profile=profile)
    assert {r.group for r in rows} == {"recruiter", "hiring", "peer", "alumni"}
    assert any(r.label == "Anthropic Research Engineer" for r in rows)
    assert any("Nova University of Lisbon" in r.label for r in rows)
    assert presets("", "x") == []


# ── runs ──


def test_a_run_of_another_kind_does_not_block(repos):
    run_id = repos.runs.start({}, command="run")
    assert repos.runs.active() is not None
    assert repos.runs.active(commands=("job-search",)) is None
    assert repos.runs.active(commands=("run",))["id"] == run_id


# ── in bulk ──


def _people(*groups):
    return [{"id": i, "full_name": f"P{i}", "group": g, "profile_url": f"https://l.in/p{i}",
             "email": None} for i, g in enumerate(groups, 1)]


def test_picks_take_one_from_each_group_in_turn():
    found = _people("recruiter", "recruiter", "hiring", "peer", "peer", "alumni")
    picked = [p["group"] for p in outreach.pick_people(found, 5)]
    assert picked == ["recruiter", "hiring", "peer", "alumni", "recruiter"]
    assert outreach.pick_people([], 3) == []


def test_a_bulk_run_searches_each_company_once_and_survives_a_failure(repos):
    apps = [{"id": i, "company_name": c, "title": "ML", "status": "APPLIED"}
            for i, c in ((1, "Waymo"), (2, "waymo"), (3, "Wayve"))]
    people = {"waymo": _people("recruiter", "hiring"), "wayve": []}
    searched, looked_up = [], []

    def find(company, job_title="", on_progress=None):
        searched.append(company)
        if company.casefold() == "wayve":
            raise RuntimeError("search engine down")

    plan = outreach.BulkPlan(applications=apps, per_company=2)
    counts = outreach.run_bulk(plan, repos.conn, find=find,
                               people=lambda c: people[c.casefold()],
                               enrich_one=lambda pid: looked_up.append(pid) or True)

    assert searched == ["Wayve"]                       # Waymo already had people
    assert counts["failed"] == 1
    assert looked_up == [1, 2], "each person once, not once per application"


# ── one row per person ──


WAYMO = [{"id": i, "company_name": "Waymo", "status": "APPLIED", "score": s, "title": t}
         for i, s, t in (
             (1, 70, "2027 Summer Intern, MS/PhD, Machine Learning, Simulator Evaluation"),
             (2, 90, "2027 Summer Intern, MS/PhD, Perception, Machine Learning"),
             (3, 80, "Software Engineer, Perception Scaling Evaluation"))]
STAFF = [
    {"id": 1, "full_name": "Katherine Fontana (Ware)", "group": "recruiter",
     "current_title": "Technical Recruiter", "current_company": "Waymo",
     "profile_url": "https://www.linkedin.com/in/kw"},
    {"id": 2, "full_name": "Anthony C", "group": "hiring",
     "current_title": "Senior Engineering Manager", "current_company": "Waymo",
     "profile_url": "https://www.linkedin.com/in/ac"},
    {"id": 3, "full_name": "Andy Warburton", "group": "peer", "current_title": "",
     "headline": "WAYMO · Engineering to make things better. · Experience: Waymo",
     "current_company": "WAYMO", "profile_url": "https://www.linkedin.com/in/aw"},
]


def _rows(conn, apps=WAYMO, staff=STAFF, **kw):
    return outreach.outreach_rows(conn, apps, people=lambda c: staff, **kw)


def test_many_roles_at_one_company_make_one_row_per_person(repos, profile):
    rows = _rows(repos.conn, profile=profile)
    assert [r["name"] for r in rows] == ["Katherine Fontana (Ware)", "Anthony C", "Andy Warburton"]
    assert rows[0]["application_ids"] == "1 2 3"
    assert rows[0]["roles"].count(" | ") == 2
    assert set(outreach.OUTREACH_COLUMNS) <= set(rows[0])


def test_every_person_gets_a_ready_note_and_message_that_fit(repos, profile):
    for row in _rows(repos.conn, profile=profile):
        assert row["note"] and row["message"] and row["note_source"] == "template"
        assert row["note_chars"] == len(row["note"]) <= LIMIT
        assert check_note(row["note"], truth=build_truth(profile, job=WAYMO[1]),
                          applied=True) == [], row["note"]
    recruiter, manager, peer = _rows(repos.conn, profile=profile)
    # the best-fit role by score, said the way a person says it
    assert "summer internship (Perception, ML)" in recruiter["note"]
    assert recruiter["note"].startswith("Hi Katherine,")
    assert "along with a few related roles" in recruiter["message"]
    assert "team" in manager["note"] and "15 minutes" in peer["message"]


def test_a_note_you_saved_replaces_the_template(repos, profile):
    conn = repos.conn
    conn.execute("INSERT INTO applications (id, company_name, title) VALUES (3, 'Waymo', 'SWE')")
    save_note(conn, 3, STAFF[1], "Hi Anthony, mine.", edited=True)
    rows = _rows(conn, profile=profile)
    assert (rows[1]["note"], rows[1]["note_source"]) == ("Hi Anthony, mine.", "your edit")
    assert rows[0]["note_source"] == "template"


def test_a_person_found_at_two_companies_is_written_to_once(repos, profile):
    apps = [*WAYMO[:1], {"id": 9, "company_name": "Waymo LLC", "title": "ML"}]
    rows = outreach.outreach_rows(repos.conn, apps, people=lambda c: STAFF[:1], profile=profile)
    assert len(rows) == 1 and "also picked for Waymo LLC" in rows[0]["check"]


def test_a_garbled_title_is_cleaned_or_flagged():
    assert outreach.clean_title(STAFF[2]) == ("", False)
    title, sure = outreach.clean_title({"current_title": "Talent Acquisition Leader - Helping "
                                        "...shaobin wang - Technical Staff at Syntiant Corp."})
    assert (title, sure) == ("Talent Acquisition Leader", False)
    assert outreach.clean_title({"current_title": "𝐂𝐲𝐛𝐞𝐫𝐬𝐞𝐜𝐮𝐫𝐢𝐭𝐲 𝐑𝐞𝐜𝐫𝐮𝐢𝐭𝐞𝐫"}) == \
        ("Cybersecurity Recruiter", True)
    assert outreach.clean_title({"headline": "Technical Recruiter · Hi Connections, I am"}) == \
        ("Technical Recruiter", True)


def test_concerns_name_what_to_check_and_steer_the_pick():
    elsewhere = {"id": 7, "group": "recruiter", "full_name": "M D",
                 "current_title": "Principal Sourcer - AI Research @ Datadog",
                 "current_company": "Datadog"}
    sales = {"id": 8, "group": "hiring", "full_name": "B P", "current_title": "Account Manager",
             "current_company": "Combuilder Pte Ltd"}
    assert outreach.concerns(elsewhere, "Meetdavis")[0] == \
        ["may not work at Meetdavis: check the profile"]
    assert outreach.concerns(sales, "COMBUILDER PTE LTD")[0] == \
        ["check they lead a technical team"]
    assert outreach.concerns(STAFF[0], "Waymo") == ([], 0)
    clean = {**elsewhere, "id": 9, "current_company": "Meetdavis"}
    assert outreach.pick_people([elsewhere, clean], 1, company="Meetdavis") == [clean]


@pytest.mark.parametrize("title, location, full, short", [
    ("Research Engineer, Code RL (Reinforcement Learning)", "", "Research Engineer, Code RL role",
     "Research Engineer role"),
    ("2027 Summer Intern, MS/PhD, Machine Learning, Simulator Evaluation", "",
     "summer internship (ML, Simulator Evaluation)", "summer internship"),
    ("Intern - ML for Computational Fluid Dynamics", "", "internship (ML for Computational "
     "Fluid Dynamics)", "internship"),
    ("[Expression of Interest] Research Engineer / Scientist, Alignment - London",
     "London, UK", "Research Engineer / Scientist, Alignment role", "Research Engineer / "
     "Scientist role"),
    ("Data Scientist (f/m/d) (Data Scientist)", "", "Data Scientist role", "Data Scientist role"),
])
def test_role_titles_are_said_the_way_a_person_says_them(title, location, full, short):
    assert outreach.short_role(title, location) == (full, short)


@pytest.mark.parametrize("name, said", [
    ("NXP SEMICONDUCTORS SINGAPORE PTE. LTD.", "NXP Semiconductors"),
    ("AGILENT TECHNOLOGIES SINGAPORE (INTERNATIONAL) PTE. LTD.", "Agilent Technologies"),
    ("Forto Logistics SE & Co. KG", "Forto Logistics"),
    ("Lokker.com", "Lokker"),
    ("GRiT Solutions", "GRiT Solutions"),
])
def test_company_names_drop_their_legal_suffixes(name, said):
    assert outreach.company_display(name) == said


def test_first_names():
    assert outreach.first_name("Katherine Fontana (Ware)") == "Katherine"
    assert outreach.first_name("F. RABHI") == "there"
    assert outreach.first_name("LOÏC Le Merlus") == "Loïc"


def test_a_long_role_falls_back_until_the_note_fits(profile):
    app = {"id": 1, "title": "Senior Staff Machine Learning Engineer, Large-Scale Distributed "
           "Training Infrastructure and Evaluation Platforms", "score": 1}
    note, _ = outreach.template_texts({"full_name": "Maximiliana Ostrowska", "group": "hiring"},
                                      "Some Very Long Company Name Holdings", [app],
                                      profile=profile)
    assert len(note) <= LIMIT and note.startswith("Hi Maximiliana,")


def test_alumni_without_a_shared_name_get_the_peer_wording(profile):
    note, _ = outreach.template_texts({"full_name": "Ana Silva", "group": "alumni"}, "Waymo",
                                      WAYMO[:1], profile=profile)
    assert "connection" not in note and "from someone doing it" in note
    note, _ = outreach.template_texts({"full_name": "Ana Silva", "group": "alumni",
                                       "shared": "Nova University of Lisbon"}, "Waymo",
                                      WAYMO[:1], profile=profile)
    assert "a Nova University of Lisbon connection" in note


# ── the drafted note's two failures from the first bulk run ──


def test_a_note_may_name_the_employer_it_is_written_to(profile):
    """"Waymo" is in a letter shown to the writer as one to avoid; a note TO
    Waymo naming Waymo is not contamination."""
    text = "Hi Rita, I've applied for the ML Engineer role at Waymo. Glad to connect."
    app = {**APP, "company_name": "Waymo", "status": "APPLIED"}
    router, _ = _router({"variants": [{"text": text}, {"text": text}]})
    draft = draft_note(person=PERSON, application=app, router=router, profile=profile)
    assert draft.checks == [[], []]


def test_one_rejected_reply_is_retried_not_fatal(profile):
    class Flaky:
        def __init__(self):
            self.asked = 0

        def complete_json(self, *args, **kwargs):
            self.asked += 1
            if self.asked == 1:
                raise outreach_llm.AllProvidersFailed("groq: json_validate_failed")
            return {"variants": [{"text": GOOD}, {"text": GOOD}]}

    router = Flaky()
    draft = draft_note(person=PERSON, application=APP, router=router, profile=profile)
    assert router.asked == 2 and draft.variants == [GOOD, GOOD]
