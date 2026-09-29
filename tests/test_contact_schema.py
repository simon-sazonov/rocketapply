"""Schema 4 -> 5, the contact repositories, and the three §6.1 guarantees.

The guarantees, and why each one has a test rather than a comment:

* **the blocklist erases** — it is the GDPR path, so "the cascade probably
  covers it" is not good enough;
* **retention reaches contact data** — an address that outlives its purpose is
  the thing this project is least entitled to keep;
* **export excludes addresses by default** — a CSV leaves every control this
  database has, and a silent default is how that data leaks.
"""

from __future__ import annotations

import sqlite3

import pytest

from pitwall.crm import CRM
from pitwall.db import SCHEMA_VERSION, ContactRepo, init_schema, open_repos


@pytest.fixture()
def seeded(repos):
    """One company, one candidate, ready to hang contact rows off."""
    repos.conn.execute("INSERT INTO companies (name) VALUES ('Alpine')")
    company_id = int(repos.conn.execute("SELECT id FROM companies").fetchone()["id"])
    cid, _ = repos.candidates.upsert(
        {
            "linkedin_slug": "jane-doe",
            "profile_url": "https://uk.linkedin.com/in/jane-doe",
            "full_name": "Jane Doe",
            "current_company": "Alpine",
        }
    )
    return repos, company_id, cid


# ───────────────────────────── the migration ─────────────────────────────


def test_schema_version_is_current(repos):
    # 5 added contact enrichment; 6 (the job search), 7 (source facts and
    # search cursors), 8 (grading facts), 9 (rating reasons) and 10 (RocketApply's
    # export batches and notes) build on it unchanged.
    row = repos.conn.execute("SELECT MAX(version) v FROM schema_version").fetchone()
    assert row["v"] == SCHEMA_VERSION == 10


def test_the_four_tables_exist(repos):
    names = {
        r["name"]
        for r in repos.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "contact_emails", "company_domains", "domain_address_samples", "enrichment_log"
    } <= names


def test_migration_from_v4_is_clean_and_idempotent(tmp_path):
    """A real 4 -> 5 upgrade: build a v4 file, migrate it, check it twice.

    `init_schema` is the only migration entry point, so running it repeatedly is
    what an existing database actually experiences every time the app starts.
    """
    path = tmp_path / "v4.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # A minimal v4-shaped file: the tables a real one would have, and its
    # version pin. The `quota` table is the one the v5 migration ALTERs.
    conn.executescript(
        """
        CREATE TABLE candidates (id INTEGER PRIMARY KEY, linkedin_slug TEXT UNIQUE,
                                 profile_url TEXT, full_name TEXT,
                                 last_seen_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE quota (provider TEXT NOT NULL, day TEXT NOT NULL,
                            requests INTEGER DEFAULT 0, credits INTEGER,
                            PRIMARY KEY (provider, day));
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        INSERT INTO schema_version(version) VALUES (4);
        INSERT INTO candidates (linkedin_slug, profile_url, full_name)
             VALUES ('kept', 'https://x/in/kept', 'Kept Person');
        INSERT INTO quota (provider, day, requests, credits)
             VALUES ('serper', '2026-08-01', 3, 2400);
        """
    )
    conn.commit()

    assert init_schema(conn) == SCHEMA_VERSION
    assert init_schema(conn) == SCHEMA_VERSION  # idempotent

    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # Nothing was dropped or rewritten.
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1
    assert conn.execute("SELECT credits FROM quota").fetchone()[0] == 2400
    assert "reset_at" in {r["name"] for r in conn.execute("PRAGMA table_info(quota)")}
    conn.close()


def test_open_repos_exposes_the_new_repositories(repos):
    assert repos.contacts is not None
    assert repos.domains is not None
    assert repos.enrichment is not None


# ─────────────────────────── contact_emails ──────────────────────────────


def test_contact_upsert_and_read_back(seeded):
    repos, _, cid = seeded
    repos.contacts.upsert(
        cid, "Jane.Doe@alpine.com", status="probable", source="pattern",
        evidence={"pattern": "first.last", "sample_size": 3}, mx_valid=True,
    )
    row = repos.contacts.best(cid)
    assert row["email"] == "jane.doe@alpine.com"      # lowercased on the way in
    assert row["status"] == "probable"
    assert row["evidence"]["sample_size"] == 3
    assert row["mx_valid"] is True


def test_a_guess_never_overwrites_a_verified_address(seeded):
    """§4's status ladder is enforced in the repository, not at each caller."""
    repos, _, cid = seeded
    repos.contacts.upsert(
        cid, "jane.doe@alpine.com", status="verified", source="corroborated",
        evidence={"corroborating_url": "https://alpine.com/team"},
    )
    repos.contacts.upsert(
        cid, "jane.doe@alpine.com", status="guess", source="pattern",
        evidence={"pattern": "first.last", "sample_size": 1},
    )
    row = repos.contacts.best(cid)
    assert row["status"] == "verified"
    assert row["source"] == "corroborated"


def test_a_better_finding_does_replace_a_worse_one(seeded):
    repos, _, cid = seeded
    repos.contacts.upsert(cid, "j.doe@alpine.com", status="guess", source="pattern")
    repos.contacts.upsert(
        cid, "j.doe@alpine.com", status="verified", source="hunter_finder", confidence=97.0
    )
    row = repos.contacts.best(cid)
    assert row["status"] == "verified"
    assert row["confidence"] == 97.0


def test_best_ranks_by_evidence_not_by_recency(seeded):
    repos, _, cid = seeded
    repos.contacts.upsert(cid, "verified@alpine.com", status="verified", source="corroborated")
    repos.contacts.upsert(cid, "later-guess@alpine.com", status="guess", source="pattern")
    assert repos.contacts.best(cid)["email"] == "verified@alpine.com"
    assert [r["email"] for r in repos.contacts.for_candidate(cid)][0] == "verified@alpine.com"


def test_confidence_is_only_ever_the_providers_own_number(seeded):
    """A locally-derived address stores NULL, never an invented percentage."""
    repos, _, cid = seeded
    repos.contacts.upsert(
        cid, "local@alpine.com", status="probable", source="pattern",
        evidence={"pattern": "first.last", "sample_size": 4},
    )
    assert repos.contacts.best(cid)["confidence"] is None


def test_for_candidates_batches_without_n_queries(seeded):
    repos, _, cid = seeded
    other, _ = repos.candidates.upsert(
        {"linkedin_slug": "b", "profile_url": "https://x/in/b", "full_name": "B"}
    )
    repos.contacts.upsert(cid, "a@alpine.com", status="guess", source="pattern")
    repos.contacts.upsert(other, "b@alpine.com", status="probable", source="pattern")
    batch = repos.contacts.for_candidates([cid, other])
    assert set(batch) == {cid, other}
    assert batch[other][0]["email"] == "b@alpine.com"
    assert repos.contacts.for_candidates([]) == {}


# ───────────────────────── company_domains ───────────────────────────────


def test_domain_upsert_normalises_and_dedupes(seeded):
    repos, company_id, _ = seeded
    first = repos.domains.upsert(company_id, "  Alpine.COM ", source="careers_url")
    again = repos.domains.upsert(company_id, "@alpine.com", source="serp")
    assert first == again
    assert repos.domains.get(company_id, "alpine.com")["domain"] == "alpine.com"
    # The first source wins; a later automatic guess does not relabel it.
    assert repos.domains.get(company_id, "alpine.com")["source"] == "careers_url"


def test_manual_override_wins_and_is_unique(seeded):
    repos, company_id, _ = seeded
    repos.domains.upsert(company_id, "alpine.com", source="serp")
    repos.domains.set_override(company_id, "alpinecars.com")
    repos.domains.set_override(company_id, "alpine-racing.com")
    rows = repos.domains.for_company(company_id)
    assert rows[0]["domain"] == "alpine-racing.com"
    assert sum(int(r["manual_override"]) for r in rows) == 1
    assert repos.domains.preferred(company_id)["domain"] == "alpine-racing.com"


def test_samples_report_whether_they_were_new(seeded):
    repos, company_id, _ = seeded
    did = repos.domains.upsert(company_id, "alpine.com")
    assert repos.domains.add_sample(did, "A.Person@alpine.com", person_name="A Person") is True
    assert repos.domains.add_sample(did, "a.person@alpine.com") is False
    assert repos.domains.sample_count(did) == 1
    assert repos.domains.add_sample(did, "") is False


def test_set_pattern_records_the_sample_size(seeded):
    repos, company_id, _ = seeded
    did = repos.domains.upsert(company_id, "alpine.com")
    repos.domains.set_pattern(did, "first.last", 3, 1.0)
    row = repos.domains.by_id(did)
    assert (row["pattern"], row["pattern_sample_size"], row["pattern_agreement"]) == (
        "first.last", 3, 1.0
    )


# ────────────────────────── enrichment_log ───────────────────────────────


def test_log_records_credits_as_a_real_number(seeded):
    """Hunter's verifier costs 0.5 credits; an INTEGER ledger would lose it."""
    repos, company_id, cid = seeded
    repos.enrichment.record(
        "hunter.verify", candidate_id=cid, provider="hunter", credits_spent=0.5,
        outcome="deliverable",
    )
    assert repos.enrichment.credits_spent("hunter") == 0.5


def test_the_ledger_survives_erasing_the_person(seeded):
    """Erasure removes the personal data; the budget history is not personal."""
    repos, _, cid = seeded
    repos.enrichment.record("hunter.finder", candidate_id=cid, provider="hunter",
                            credits_spent=1.0, outcome="found")
    CRM(repos.conn).delete_and_block(cid)
    rows = repos.enrichment.recent()
    assert len(rows) == 1
    assert rows[0]["candidate_id"] is None
    assert repos.enrichment.credits_spent("hunter") == 1.0


# ───────────────────────────── quota reuse ───────────────────────────────


def test_hunter_credits_reuse_the_quota_table(repos):
    repos.quota.set_credits("hunter", 34, reset_at="2026-10-01")
    state = repos.quota.credit_state("hunter")
    assert state["credits"] == 34
    assert state["reset_at"] == "2026-10-01"


def test_a_local_spend_does_not_erase_the_reset_date(repos):
    repos.quota.set_credits("hunter", 34, reset_at="2026-10-01")
    assert repos.quota.spend_credits("hunter", 1) == 33
    assert repos.quota.reset_at("hunter") == "2026-10-01"


def test_a_half_credit_spend_rounds_the_remainder_down(repos):
    """Never promise a credit the provider is about to refuse."""
    repos.quota.set_credits("hunter", 50)
    assert repos.quota.spend_credits("hunter", 0.5) == 49


def test_credits_never_go_negative(repos):
    repos.quota.set_credits("hunter", 1)
    assert repos.quota.spend_credits("hunter", 5) == 0


# ─────────────────────── §6.1: the blocklist erases ──────────────────────


def test_blocklisting_hard_deletes_contact_rows(seeded):
    repos, _, cid = seeded
    repos.contacts.upsert(cid, "jane.doe@alpine.com", status="verified", source="corroborated")
    slug = CRM(repos.conn).delete_and_block(cid, "not interested")

    assert slug == "jane-doe"
    assert repos.contacts.for_candidate(cid) == []
    assert repos.conn.execute("SELECT COUNT(*) FROM contact_emails").fetchone()[0] == 0
    assert repos.candidates.is_blocked("jane-doe") is True


def test_erasure_holds_even_with_foreign_keys_off(seeded):
    """The cascade is real, but the promise must not depend on a pragma."""
    repos, _, cid = seeded
    repos.contacts.upsert(cid, "jane.doe@alpine.com", status="probable", source="pattern")
    repos.conn.execute("PRAGMA foreign_keys = OFF")
    try:
        CRM(repos.conn).delete_and_block(cid)
        assert repos.conn.execute("SELECT COUNT(*) FROM contact_emails").fetchone()[0] == 0
    finally:
        repos.conn.execute("PRAGMA foreign_keys = ON")


def test_erase_contacts_keeps_the_person(seeded):
    repos, _, cid = seeded
    repos.contacts.upsert(cid, "jane.doe@alpine.com", status="probable", source="pattern")
    assert CRM(repos.conn).erase_contacts(cid) == 1
    assert repos.contacts.for_candidate(cid) == []
    assert repos.candidates.get_by_slug("jane-doe") is not None


# ─────────────────────── §6.1: retention reaches it ──────────────────────


def test_purge_deletes_addresses_of_people_never_acted_on(seeded):
    from scripts.purge_stale import stale_contacts

    repos, _, cid = seeded
    repos.contacts.upsert(cid, "old@alpine.com", status="guess", source="pattern")
    repos.conn.execute(
        "UPDATE contact_emails SET retrieved_at = datetime('now', '-200 days')"
    )
    stale = stale_contacts(repos.conn, "-180 days")
    assert [s["candidate_id"] for s in stale] == [cid]


def test_purge_spares_addresses_of_people_you_saved(seeded):
    """Your own judgement beats the timer — the same rule the candidate purge
    already applies, extended to the address."""
    from scripts.purge_stale import stale_contacts

    repos, _, cid = seeded
    repos.contacts.upsert(cid, "old@alpine.com", status="verified", source="corroborated")
    repos.conn.execute(
        "UPDATE contact_emails SET retrieved_at = datetime('now', '-200 days')"
    )
    CRM(repos.conn).save(cid)
    assert stale_contacts(repos.conn, "-180 days") == []


def test_purge_spares_a_recent_address(seeded):
    from scripts.purge_stale import stale_contacts

    repos, _, cid = seeded
    repos.contacts.upsert(cid, "fresh@alpine.com", status="guess", source="pattern")
    assert stale_contacts(repos.conn, "-180 days") == []


# ───────────────────── §6.1: export excludes by default ──────────────────


@pytest.fixture()
def leads_with_contact(seeded, settings):
    """A scored candidate carrying one address, as a leads DataFrame."""
    from pitwall.export import leads_frame

    repos, company_id, cid = seeded
    repos.conn.execute(
        "INSERT INTO scores (candidate_id, company_id, final_score, heuristic_score,"
        " verdict, prompt_version) VALUES (?,?,?,?,?,?)",
        (cid, company_id, 80.0, 70.0, "strong", settings.prompt_version),
    )
    repos.contacts.upsert(
        cid, "jane.doe@alpine.com", status="probable", source="pattern",
        evidence={"pattern": "first.last", "sample_size": 3, "mx_host": "aspmx.l.google.com"},
        mx_valid=True,
    )
    return leads_frame(repos.conn, settings)


def test_leads_frame_carries_the_address_for_the_ui(leads_with_contact):
    row = leads_with_contact.iloc[0]
    assert row["contact_email"] == "jane.doe@alpine.com"
    assert row["contact_status"] == "probable"
    assert row["contact_evidence"]["sample_size"] == 3
    assert row["contact_count"] == 1


def test_csv_export_omits_addresses_by_default(leads_with_contact, tmp_path, settings):
    from pitwall.export import to_csv

    path = to_csv(leads_with_contact, tmp_path / "leads.csv", settings)
    text = path.read_text(encoding="utf-8")
    assert "jane.doe@alpine.com" not in text
    assert "contact_email" not in text
    assert "Jane Doe" in text          # the rest of the export is unchanged


def test_csv_export_includes_addresses_only_on_request(leads_with_contact, tmp_path, settings):
    from pitwall.export import to_csv

    path = to_csv(leads_with_contact, tmp_path / "leads.csv", settings, include_contacts=True)
    text = path.read_text(encoding="utf-8")
    assert "jane.doe@alpine.com" in text
    # The evidence travels with the address, so a spreadsheet cannot read a
    # pattern guess as a fact.
    assert "pattern first.last (n=3)" in text
    assert "probable" in text


def test_including_contacts_warns(leads_with_contact, tmp_path, settings, caplog):
    from pitwall.export import to_csv

    with caplog.at_level("WARNING"):
        to_csv(leads_with_contact, tmp_path / "leads.csv", settings, include_contacts=True)
    assert any("contact export" in r.message for r in caplog.records)


def test_markdown_export_omits_addresses_by_default(leads_with_contact):
    from pitwall.export import markdown_text

    assert "jane.doe@alpine.com" not in markdown_text(leads_with_contact)


def test_markdown_export_labels_the_address_with_its_status(leads_with_contact):
    from pitwall.export import markdown_text

    out = markdown_text(leads_with_contact, include_contacts=True)
    assert "jane.doe@alpine.com" in out
    assert "**probable**" in out
    assert "pattern first.last (n=3)" in out


def test_xlsx_export_omits_addresses_by_default(leads_with_contact, tmp_path, settings):
    import pandas as pd

    from pitwall.export import to_xlsx

    path = to_xlsx(leads_with_contact, tmp_path / "leads.xlsx", settings)
    assert "contact_email" not in pd.read_excel(path).columns


def test_evidence_text_never_renders_a_bare_number():
    """§4: "never render a bare confidence percentage for a pattern guess"."""
    from pitwall.export import evidence_text

    assert evidence_text({"pattern": "flast", "sample_size": 2}) == "pattern flast (n=2)"
    assert evidence_text({"pattern": "flast"}) == "pattern flast (n=0)"
    assert evidence_text(
        {"corroborating_url": "https://x/y", "corroborating_domain": "alpine.com"}
    ) == "published: alpine.com"
    assert evidence_text({}) == ""
    assert evidence_text(None) == ""


def test_leads_frame_is_unchanged_for_a_database_with_no_contacts(repos, settings):
    """The join must not drop or duplicate rows when nothing has been enriched."""
    from pitwall.export import leads_frame

    for i in range(3):
        repos.candidates.upsert(
            {"linkedin_slug": f"p{i}", "profile_url": f"https://x/in/p{i}", "full_name": f"P{i}"}
        )
    df = leads_frame(repos.conn, settings)
    assert len(df) == 3
    assert set(df["contact_status"]) == {"not_found"}
    assert set(df["contact_count"]) == {0}


def test_contact_repo_rejects_an_empty_address(seeded):
    repos, _, cid = seeded
    with pytest.raises(ValueError):
        ContactRepo(repos.conn).upsert(cid, "   ", status="guess", source="pattern")


def test_reopening_the_database_keeps_the_contact_rows(seeded, settings):
    """A round-trip through a real file, not just the in-memory fixture."""
    repos, _, cid = seeded
    repos.contacts.upsert(cid, "jane.doe@alpine.com", status="probable", source="pattern")
    from pitwall.db import close_all

    close_all()
    again = open_repos(settings)
    assert again.contacts.best(cid)["email"] == "jane.doe@alpine.com"
