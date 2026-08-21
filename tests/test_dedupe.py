"""Slug canonicalization, merge policy, fuzzy flagging, blocklist (§8.3, §12.1)."""

from __future__ import annotations

import pytest

from pitwall.extract.dedupe import (
    FUZZY_FLAG_THRESHOLD,
    find_possible_duplicates,
    match_slugless,
    merge_candidates,
    merge_fields,
    same_person,
    slug_key,
)
from pitwall.extract.normalize import canonicalize_url, extract_linkedin_slug


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://www.linkedin.com/in/jane-doe-1a2b3c", "jane-doe-1a2b3c"),
        ("https://uk.linkedin.com/in/Jane-Doe-1a2b3c/", "jane-doe-1a2b3c"),
        ("http://linkedin.com/in/jane-doe-1a2b3c?trk=public", "jane-doe-1a2b3c"),
        ("https://de.linkedin.com/in/j%C3%BCrgen-m%C3%BCller", "jürgen-müller"),
        ("https://www.linkedin.com/company/mclaren", None),
    ],
)
def test_slug_is_the_canonical_key(url, slug):
    assert extract_linkedin_slug(url) == slug
    assert slug_key(url) == slug


def test_country_mirrors_collapse_to_one_canonical_url():
    a = canonicalize_url("https://uk.linkedin.com/in/Jane-Doe/?trk=x&utm_source=y")
    b = canonicalize_url("https://www.linkedin.com/in/jane-doe")
    assert a == b == "https://linkedin.com/in/jane-doe"


def test_tracking_parameters_are_stripped_from_ordinary_urls():
    assert canonicalize_url("https://Example.com/A/?utm_source=x&id=7") == (
        "https://example.com/a?id=7"
    )


def test_merge_keeps_the_longest_non_empty_value():
    existing = {
        "linkedin_slug": "jane", "full_name": "J Doe", "headline": "short",
        "current_title": "Analyst", "current_company": None, "location": None,
    }
    fresh = {
        "linkedin_slug": "jane", "full_name": "Jane Doe", "headline": "a much longer headline",
        "current_title": "", "current_company": "McLaren Racing",
        "location": "Woking, England, United Kingdom",
    }
    merged = merge_fields(existing, fresh)
    assert merged["full_name"] == "Jane Doe"
    assert merged["headline"] == "a much longer headline"
    assert merged["current_title"] == "Analyst"  # a blank must never overwrite
    assert merged["current_company"] == "McLaren Racing"
    assert merged["location"] == "Woking, England, United Kingdom"


def test_merge_never_drops_the_slug():
    merged = merge_fields({"linkedin_slug": "jane", "full_name": "J"}, {"full_name": "Jane Doe"})
    assert merged["linkedin_slug"] == "jane"


def test_same_person_needs_both_the_name_and_the_employer():
    a = {"full_name": "Jane Doe", "current_company": "McLaren Racing"}
    assert same_person(a, {"full_name": "Doe Jane", "current_company": "McLaren F1"})
    assert not same_person(a, {"full_name": "Jane Doe", "current_company": "Red Bull Racing"})
    assert not same_person(a, {"full_name": "John Smith", "current_company": "McLaren Racing"})


def test_possible_duplicates_are_flagged_not_merged():
    people = [
        {"id": 1, "linkedin_slug": "a", "full_name": "Jane Doe", "current_company": "McLaren Racing"},
        {"id": 2, "linkedin_slug": "b", "full_name": "Doe Jane", "current_company": "McLaren F1"},
        {"id": 3, "linkedin_slug": "c", "full_name": "Tom Green", "current_company": "McLaren Racing"},
    ]
    pairs = find_possible_duplicates(people)
    assert len(pairs) == 1
    a, b, ratio = pairs[0]
    assert {a["linkedin_slug"], b["linkedin_slug"]} == {"a", "b"}
    assert ratio >= FUZZY_FLAG_THRESHOLD


def test_identical_names_at_different_employers_are_not_duplicates():
    people = [
        {"id": 1, "linkedin_slug": "a", "full_name": "Marco Rossi", "current_company": "AF Corse"},
        {"id": 2, "linkedin_slug": "b", "full_name": "Marco Rossi", "current_company": "PREMA Racing"},
    ]
    assert find_possible_duplicates(people) == []


def test_slugless_records_can_auto_match_but_slugged_ones_cannot():
    pool = [{"linkedin_slug": "jane-doe", "full_name": "Jane Doe", "current_company": "McLaren Racing"}]
    hit = match_slugless(
        {"linkedin_slug": None, "full_name": "Jane Doe", "current_company": "McLaren F1"}, pool
    )
    assert hit is not None and hit["linkedin_slug"] == "jane-doe"
    assert match_slugless({"linkedin_slug": "other", "full_name": "Jane Doe",
                           "current_company": "McLaren Racing"}, pool) is None


# ─────────────────────────── DB-level behaviour ──────────────────────────


def test_upsert_on_slug_never_duplicates(repos):
    fields = {
        "linkedin_slug": "jane-doe", "profile_url": "https://www.linkedin.com/in/jane-doe",
        "full_name": "Jane Doe", "current_company": "McLaren Racing",
    }
    first_id, is_new = repos.candidates.upsert(fields)
    assert is_new
    second_id, is_new2 = repos.candidates.upsert({**fields, "headline": "longer headline"})
    assert second_id == first_id
    assert not is_new2
    assert repos.candidates.count() == 1
    assert repos.candidates.get_by_slug("jane-doe")["headline"] == "longer headline"


def test_blocklisted_slugs_are_reported(repos):
    repos.crm.block("bad-actor", "asked to be removed")
    assert repos.candidates.is_blocked("bad-actor")
    assert not repos.candidates.is_blocked("someone-else")
    repos.crm.unblock("bad-actor")
    assert not repos.candidates.is_blocked("bad-actor")


def test_evidence_count_counts_distinct_queries(repos):
    conn = repos.conn
    cid, _ = repos.candidates.upsert(
        {"linkedin_slug": "jane", "profile_url": "https://www.linkedin.com/in/jane"}
    )
    for q in (1, 1, 2):
        conn.execute(
            "INSERT INTO queries (id, template_id, query_text, query_hash) VALUES (?,?,?,?)"
            " ON CONFLICT DO NOTHING",
            (q, "t", f"q{q}", f"h{q}"),
        )
    for i, q in enumerate((1, 1, 2), start=1):
        conn.execute(
            "INSERT INTO search_results (id, query_id, provider, rank, url, canonical_url)"
            " VALUES (?,?,?,?,?,?)",
            (i, q, "ddgs", i, f"https://x/{i}", f"https://x/{i}"),
        )
        repos.candidates.link_source(cid, i)
    assert repos.candidates.refresh_evidence_count(cid) == 2  # two distinct queries


def test_manual_merge_moves_evidence_and_keeps_the_better_status(repos):
    conn = repos.conn
    keep, _ = repos.candidates.upsert(
        {"linkedin_slug": "a", "profile_url": "u1", "full_name": "J Doe"}
    )
    drop, _ = repos.candidates.upsert(
        {"linkedin_slug": "b", "profile_url": "u2", "full_name": "Jane Doe",
         "current_company": "McLaren Racing"}
    )
    conn.execute("INSERT INTO queries (id, template_id, query_text, query_hash) VALUES (1,'t','q','h')")
    conn.execute(
        "INSERT INTO search_results (id, query_id, provider, rank, url, canonical_url)"
        " VALUES (1,1,'ddgs',1,'u','u')"
    )
    repos.candidates.link_source(drop, 1)
    repos.crm.update(drop, status="saved", note="worth a message")
    repos.crm.update(keep, status="new")

    merge_candidates(conn, keep, drop)

    assert repos.candidates.get_by_slug("b") is None
    survivor = repos.candidates.get_by_slug("a")
    assert survivor["full_name"] == "Jane Doe"          # longest wins
    assert survivor["current_company"] == "McLaren Racing"
    assert survivor["evidence_count"] == 1              # evidence moved across
    assert repos.crm.get(keep)["status"] == "saved"     # better status wins
    assert "worth a message" in repos.crm.get(keep)["note"]
