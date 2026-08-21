"""Status transitions, note append, follow-ups, delete+block (§11, §12.1)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from pitwall.crm import CRM, InvalidTransition, can_transition


@pytest.fixture()
def person(repos):
    cid, _ = repos.candidates.upsert(
        {
            "linkedin_slug": "jane-doe",
            "profile_url": "https://www.linkedin.com/in/jane-doe",
            "full_name": "Jane Doe",
            "current_title": "Data Scientist",
            "current_company": "McLaren Racing",
        }
    )
    return cid


@pytest.fixture()
def crm(repos):
    return CRM(repos.conn)


def test_new_is_the_default_status(crm, person):
    assert crm.status(person) == "new"


@pytest.mark.parametrize(
    ("start", "target", "allowed"),
    [
        ("new", "saved", True),
        ("new", "ignored", True),
        ("saved", "contacted", True),
        ("contacted", "replied", True),
        ("replied", "meeting", True),
        ("meeting", "closed", True),
        ("new", "replied", False),
        ("new", "meeting", False),
        ("ignored", "contacted", False),
        ("saved", "meeting", False),
    ],
)
def test_the_documented_state_machine(start, target, allowed):
    assert can_transition(start, target) is allowed


def test_illegal_transitions_raise_unless_forced(crm, person):
    with pytest.raises(InvalidTransition):
        crm.set_status(person, "meeting")
    assert crm.set_status(person, "meeting", force=True) == "meeting"


def test_the_happy_path_walks_the_whole_machine(crm, person):
    for target in ("saved", "contacted", "replied", "meeting", "closed"):
        assert crm.set_status(person, target) == target


def test_marking_contacted_stamps_the_time(crm, person, repos):
    crm.mark_contacted(person, note="sent a message about tyre-deg modelling", followup_days=14)
    row = repos.crm.get(person)
    assert row["status"] == "contacted"
    assert row["contacted_at"] is not None
    assert row["followup_at"] == (date.today() + timedelta(days=14)).isoformat()
    assert "tyre-deg" in row["note"]


def test_notes_are_appended_and_timestamped_never_overwritten(crm, person):
    crm.add_note(person, "First contact at the F1 data hackathon.",
                 when=datetime(2026, 8, 20, 14, 3))
    crm.add_note(person, "No reply yet. Follow up after Zandvoort.",
                 when=datetime(2026, 8, 27, 9, 12))
    note = crm.get(person)["note"]
    assert note.splitlines() == [
        "[2026-08-20 14:03] First contact at the F1 data hackathon.",
        "[2026-08-27 09:12] No reply yet. Follow up after Zandvoort.",
    ]


def test_blank_notes_are_ignored(crm, person):
    crm.add_note(person, "real note")
    crm.add_note(person, "   ")
    assert crm.get(person)["note"].count("\n") == 0


def test_tags_are_a_deduplicated_lowercase_set(crm, person):
    crm.add_tags(person, "F1, Strategy")
    crm.add_tags(person, ["strategy", "alumni-tudelft"])
    assert crm.get(person)["tags"] == ["f1", "strategy", "alumni-tudelft"]
    assert crm.remove_tag(person, "f1") == ["strategy", "alumni-tudelft"]


def test_followups_can_be_set_cleared_and_snoozed(crm, person):
    assert crm.set_followup(person, 7) == (date.today() + timedelta(days=7)).isoformat()
    assert crm.snooze(person, 7) == (date.today() + timedelta(days=14)).isoformat()
    assert crm.set_followup(person, None) is None
    assert crm.get(person)["followup_at"] is None


def test_due_followups_surface_overdue_people_only(crm, person, repos):
    other, _ = repos.candidates.upsert(
        {"linkedin_slug": "later", "profile_url": "u", "full_name": "Later Person"}
    )
    crm.set_status(person, "contacted", force=True)
    crm.set_followup(person, on=date.today() - timedelta(days=1))
    crm.set_status(other, "contacted", force=True)
    crm.set_followup(other, on=date.today() + timedelta(days=30))

    due = crm.due_followups()
    assert [d["full_name"] for d in due] == ["Jane Doe"]


def test_ignored_and_closed_people_never_appear_as_due(crm, person):
    crm.set_followup(person, on=date.today() - timedelta(days=5))
    crm.set_status(person, "ignored", force=True)
    assert crm.due_followups() == []


def test_delete_and_block_removes_and_prevents_reingestion(crm, person, repos):
    slug = crm.delete_and_block(person, "asked to be removed")
    assert slug == "jane-doe"
    assert repos.candidates.get_by_slug("jane-doe") is None
    assert repos.candidates.is_blocked("jane-doe")


def test_reply_rate_counts_replies_and_meetings(crm, repos):
    ids = []
    for i, status in enumerate(["contacted", "contacted", "replied", "meeting"]):
        cid, _ = repos.candidates.upsert(
            {"linkedin_slug": f"p{i}", "profile_url": f"u{i}", "full_name": f"P{i}"}
        )
        crm.set_status(cid, status, force=True)
        ids.append(cid)
    assert crm.reply_rate() == pytest.approx(2 / 4)


def test_contacted_since_counts_a_rolling_window(crm, person):
    crm.mark_contacted(person)
    assert crm.contacted_since(7) == 1
    assert crm.contacted_since(0) >= 0


def test_bulk_status_edit_writes_only_valid_rows(crm, repos):
    a, _ = repos.candidates.upsert({"linkedin_slug": "a", "profile_url": "u"})
    b, _ = repos.candidates.upsert({"linkedin_slug": "b", "profile_url": "u2"})
    assert crm.bulk_set_status({a: "saved", b: "ignored"}) == 2
    assert crm.status(a) == "saved"
    assert crm.status(b) == "ignored"


def test_counts_cover_every_status(crm, person):
    counts = crm.counts()
    assert set(counts) == {"new", "saved", "ignored", "contacted", "replied", "meeting", "closed"}
    assert counts["new"] == 1
