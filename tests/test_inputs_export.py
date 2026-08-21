"""CSV validation, DB sync, the joined leads view and the exporters."""

from __future__ import annotations

import pandas as pd
import pytest

from pitwall.export import (
    EXPORT_COLUMNS,
    coverage_matrix,
    export_view,
    filter_frame,
    leads_frame,
    markdown_text,
    zero_result_companies,
)
from pitwall.inputs import (
    load_companies,
    load_negative_keywords,
    load_roles,
    merge_negative_keywords,
    save_companies,
    sync_to_db,
)
from pitwall.models import Company, Role, Score

# ────────────────────────────── CSV loading ──────────────────────────────


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_a_clean_file_loads_with_only_warnings(project):
    companies, report = load_companies(project / "config" / "companies.csv")
    assert [c.name for c in companies] == ["McLaren Racing"]
    assert report.ok
    assert companies[0].aliases == ["McLaren F1", "McLaren Applied", "MTC"]
    assert "Health Care" in companies[0].negative_keywords


def test_a_missing_file_is_reported_not_raised(tmp_path):
    companies, report = load_companies(tmp_path / "nope.csv")
    assert companies == []
    assert not report.ok
    assert "not found" in report.errors[0].message


def test_a_missing_required_column_is_reported(tmp_path):
    path = write(tmp_path / "c.csv", "aliases,tier\nfoo,1\n")
    companies, report = load_companies(path)
    assert companies == []
    assert any("'name' is missing" in e.message for e in report.errors)


def test_duplicate_names_are_skipped_and_reported(tmp_path):
    path = write(
        tmp_path / "c.csv",
        "name,tier\nMcLaren Racing,1\nmclaren racing,2\nAF Corse,2\n",
    )
    companies, report = load_companies(path)
    assert [c.name for c in companies] == ["McLaren Racing", "AF Corse"]
    assert any("duplicate company name" in e.message for e in report.errors)


def test_an_out_of_range_tier_warns_and_defaults(tmp_path):
    path = write(tmp_path / "c.csv", "name,tier\nX,9\n")
    companies, report = load_companies(path)
    assert companies[0].tier == 2
    assert any("tier must be" in w.message for w in report.warnings)


def test_missing_aliases_and_negatives_warn_because_they_cost_you_recall(tmp_path):
    path = write(tmp_path / "c.csv", "name,aliases,negative_keywords\nX,,\n")
    _, report = load_companies(path)
    messages = " ".join(w.message for w in report.warnings)
    assert "no aliases" in messages
    assert "negative_keywords" in messages


def test_a_bad_role_family_is_an_error_that_lists_the_allowed_values(tmp_path):
    path = write(tmp_path / "r.csv", "title,family\nData Scientist,astrology\n")
    roles, report = load_roles(path)
    assert roles == []
    assert any("allowed families" in e.message for e in report.errors)


def test_a_bad_seniority_is_an_error(tmp_path):
    path = write(tmp_path / "r.csv", "title,family,seniority\nX,data_science,demigod\n")
    roles, report = load_roles(path)
    assert roles == []
    assert report.errors


def test_inactive_rows_load_but_are_marked(tmp_path):
    path = write(tmp_path / "c.csv", "name,active\nOn,true\nOff,false\n")
    companies, _ = load_companies(path)
    assert [c.active for c in companies] == [True, False]


def test_unknown_columns_warn_rather_than_break(tmp_path):
    path = write(tmp_path / "c.csv", "name,favourite_colour\nX,orange\n")
    companies, report = load_companies(path)
    assert companies[0].name == "X"
    assert any("unknown column" in w.message for w in report.warnings)


def test_a_utf8_bom_and_padded_headers_are_tolerated(tmp_path):
    path = tmp_path / "c.csv"
    path.write_text("﻿ name , tier \nX,1\n", encoding="utf-8")
    companies, report = load_companies(path)
    assert companies and companies[0].name == "X"


def test_semicolons_and_pipes_both_split_multi_value_cells(tmp_path):
    path = write(tmp_path / "c.csv", "name,aliases\nX,a|b; c\n")
    companies, _ = load_companies(path)
    assert companies[0].aliases == ["a", "b", "c"]


def test_yaml_negatives_merge_into_the_company(project):
    data = load_negative_keywords(project / "config" / "negative_keywords.yaml")
    assert "per_company" in data
    companies = [Company(name="McLaren Racing", negative_keywords="Automotive")]
    merge_negative_keywords(companies, data)
    assert "Automotive" in companies[0].negative_keywords
    assert "health care" in companies[0].negative_keywords


def test_round_tripping_through_the_csv_writer(tmp_path):
    original = [
        Company(name="X", aliases="a|b", negative_keywords="n1|n2", tier=1, country="UK")
    ]
    path = save_companies(original, tmp_path / "out.csv")
    reloaded, report = load_companies(path)
    assert report.ok
    assert reloaded[0].aliases == ["a", "b"]
    assert reloaded[0].negative_keywords == ["n1", "n2"]


# ─────────────────────────────── DB syncing ──────────────────────────────


def test_sync_upserts_rather_than_duplicating(repos):
    company = Company(name="X", aliases="a", negative_keywords="", tier=3)
    role = Role(title="R", family="strategy", aliases="", seniority="", keywords="")
    sync_to_db(repos.conn, [company], [role])
    company.tier = 1
    company.aliases = ["a", "b"]
    sync_to_db(repos.conn, [company], [role])

    rows = repos.companies.all()
    assert len(rows) == 1
    assert rows[0]["tier"] == 1
    assert rows[0]["aliases"] == ["a", "b"]


def test_rows_removed_from_the_csv_are_deactivated_not_deleted(repos):
    a = Company(name="A", aliases="", negative_keywords="")
    b = Company(name="B", aliases="", negative_keywords="")
    role = Role(title="R", family="strategy", aliases="", seniority="", keywords="")
    sync_to_db(repos.conn, [a, b], [role])
    sync_to_db(repos.conn, [a], [role])

    assert [c["name"] for c in repos.companies.all(active_only=True)] == ["A"]
    assert {c["name"] for c in repos.companies.all(active_only=False)} == {"A", "B"}


# ───────────────────────── the joined leads view ─────────────────────────


@pytest.fixture()
def populated(repos, settings):
    company = Company(name="McLaren Racing", aliases="McLaren F1", negative_keywords="Health Care",
                      series="F1", tier=1)
    role = Role(title="Data Scientist", family="data_science", aliases="", seniority="",
                keywords="")
    sync_to_db(repos.conn, [company], [role])
    people = [
        ("jane", "Jane Doe", "Data Scientist", "McLaren Racing", 92.0, "strong", "saved"),
        ("tom", "Tom Green", "Data Engineer", "McLaren Racing", 61.0, "possible", "new"),
        ("dave", "David Brooks", "Data Analyst", "McLaren Health Care", 8.0, "reject", "ignored"),
    ]
    for slug, name, title, employer, score, verdict, status in people:
        cid, _ = repos.candidates.upsert(
            {"linkedin_slug": slug, "profile_url": f"https://www.linkedin.com/in/{slug}",
             "full_name": name, "current_title": title, "current_company": employer,
             "location": "Woking, England, United Kingdom", "headline": f"{title} at {employer}"}
        )
        repos.scores.upsert(
            Score(candidate_id=cid, company_id=company.id, role_id=role.id,
                  heuristic_score=score, llm_score=score, final_score=score, verdict=verdict,
                  is_current_employee=verdict != "reject", company_match="exact",
                  role_match="exact", seniority="mid", confidence=0.9,
                  reasons=[f"reason for {name}"], red_flags=[],
                  prompt_version=settings.prompt_version)
        )
        repos.crm.update(cid, status=status, tags=["f1"])
    return repos


def test_the_leads_view_joins_everything_the_ui_needs(populated, settings):
    df = leads_frame(populated.conn, settings)
    assert len(df) == 3
    assert list(df["name"]) == ["Jane Doe", "Tom Green", "David Brooks"]  # score desc
    row = df.iloc[0]
    assert row["target_company"] == "McLaren Racing"
    assert row["target_role"] == "Data Scientist"
    assert row["role_family"] == "data_science"
    assert row["status"] == "saved"
    assert row["reasons"] == ["reason for Jane Doe"]
    assert row["tags"] == ["f1"]
    assert bool(row["is_current_employee"]) is True


def test_an_empty_database_returns_a_typed_empty_frame(repos, settings):
    df = leads_frame(repos.conn, settings)
    assert df.empty
    assert "score" in df.columns and "status" in df.columns
    assert filter_frame(df).empty


def test_only_the_best_scoring_pairing_shows_per_person(populated, settings):
    conn = populated.conn
    jane = populated.candidates.get_by_slug("jane")
    conn.execute("INSERT INTO companies (name, tier) VALUES ('Other', 2)")
    other = conn.execute("SELECT id FROM companies WHERE name='Other'").fetchone()["id"]
    populated.scores.upsert(
        Score(candidate_id=jane["id"], company_id=other, role_id=None, heuristic_score=10,
              llm_score=10, final_score=10, verdict="weak", prompt_version=settings.prompt_version)
    )
    df = leads_frame(conn, settings)
    assert len(df[df["name"] == "Jane Doe"]) == 1
    assert df[df["name"] == "Jane Doe"].iloc[0]["score"] == 92.0


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, ["Jane Doe", "Tom Green"]),                            # ignored hidden by default
        ({"hide_ignored": False}, ["Jane Doe", "Tom Green", "David Brooks"]),
        ({"score_range": (70.0, 100.0)}, ["Jane Doe"]),
        ({"verdicts": ["possible"]}, ["Tom Green"]),
        ({"statuses": ["saved"]}, ["Jane Doe"]),
        ({"text": "green"}, ["Tom Green"]),
        ({"text": "mclaren racing"}, ["Jane Doe", "Tom Green"]),
        ({"families": ["data_science"]}, ["Jane Doe", "Tom Green"]),
        ({"families": ["strategy"]}, []),
        ({"tags": ["f1"]}, ["Jane Doe", "Tom Green"]),
        ({"current_only": True}, ["Jane Doe", "Tom Green"]),
        ({"seniorities": ["mid"]}, ["Jane Doe", "Tom Green"]),
    ],
)
def test_the_sidebar_filters(populated, settings, kwargs, expected):
    df = leads_frame(populated.conn, settings)
    assert list(filter_frame(df, **kwargs)["name"]) == expected


# ────────────────────────────── the exporters ────────────────────────────


def test_csv_export_flattens_list_columns(populated, settings, tmp_path):
    df = leads_frame(populated.conn, settings)
    path = export_view(df, "csv", tmp_path / "out.csv", settings)
    written = pd.read_csv(path)
    assert len(written) == 3
    assert set(EXPORT_COLUMNS) <= set(written.columns)
    assert written.iloc[0]["tags"] == "f1"


def test_xlsx_export_writes_a_readable_workbook(populated, settings, tmp_path):
    df = leads_frame(populated.conn, settings)
    path = export_view(df, "xlsx", tmp_path / "out.xlsx", settings)
    assert pd.read_excel(path).shape[0] == 3


def test_the_markdown_contact_sheet_groups_by_company(populated, settings):
    text = markdown_text(leads_frame(populated.conn, settings))
    assert "# PitWall contact sheet" in text
    assert "## McLaren Racing" in text
    assert "**Jane Doe**" in text
    assert "https://www.linkedin.com/in/jane" in text


def test_an_unknown_export_format_is_rejected(populated, settings):
    with pytest.raises(ValueError, match="unknown export format"):
        export_view(leads_frame(populated.conn, settings), "pdf")


# ────────────────────────────── analytics ────────────────────────────────


def test_the_coverage_matrix_counts_strong_matches(populated, settings):
    cov = coverage_matrix(leads_frame(populated.conn, settings), strong_at=75.0)
    row = cov[cov["target_company"] == "McLaren Racing"].iloc[0]
    assert row["role_family"] == "data_science"
    assert row["strong"] == 1
    assert row["total"] == 3


def test_zero_result_companies_flag_alias_problems(repos):
    """§12.3: a misspelled company produces 0 results and is flagged."""
    conn = repos.conn
    sync_to_db(
        conn,
        [Company(name="Mclarn Racing", aliases="", negative_keywords="")],
        [Role(title="R", family="strategy", aliases="", seniority="", keywords="")],
    )
    cid = conn.execute("SELECT id FROM companies").fetchone()["id"]
    conn.execute(
        "INSERT INTO queries (company_id, template_id, query_text, query_hash, status,"
        " results_count) VALUES (?,'t','q','h1','done',0)",
        (cid,),
    )
    flagged = zero_result_companies(conn)
    assert [f["name"] for f in flagged] == ["Mclarn Racing"]
