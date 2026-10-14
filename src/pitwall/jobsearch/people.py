"""People at the companies you applied to — PitWall's engine, kept out of PitWall.

RocketApply's "Applied" view offers, per job, the people worth a note: the
recruiters, the hiring manager or team lead, peers in the same role, and
alumni who share a school or a past employer. PitWall already finds people
(web search → LinkedIn profile → score) and finds their email addresses; this
module points that engine at one employer at a time.

It does so in a DATABASE OF ITS OWN, `data/pitwall_employers.db`. Every PitWall
view reads `candidates` unfiltered — the People page, Openings, the funnel, the
CRM — so contacts at Anthropic saved into the main file would appear in the
motorsport lead list. A separate file needs no view to filter anything. Two
things stay shared, deliberately: the `quota` table (search-provider credits
and Hunter credits are one budget, whichever app spends them) and the SERP
cache.

The groups come from which search found a person, not from a guess about their
title: every role row written for a run carries its group, and a candidate
inherits the group of the query that surfaced them.
"""

from __future__ import annotations

import csv
import json
import pathlib
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..db import QuotaRepo, Repos, get_conn, init_schema, open_repos
from .companies import normalize_company

__all__ = [
    "GROUPS", "GROUP_LABELS", "ContactRole", "employer_settings", "employer_repos", "group_for",
    "contact_roles", "roles_for_job", "upsert_employer", "find_people", "people_at",
    "known_in_pitwall", "set_employer_domain", "employer_domain",
]

GROUPS = ("recruiter", "hiring", "peer", "alumni")
GROUP_LABELS = {
    "recruiter": "Recruiters",
    "hiring": "Hiring manager / team lead",
    "peer": "Peers in the role",
    "alumni": "Alumni & shared background",
}

#: The shipped defaults for who to look for; `config/contact_roles.csv`
#: overrides them. Families must be legal in the active people profile.
DEFAULT_ROLES = (
    ("Technical Recruiter", "Talent Acquisition Partner|Recruiter|Talent Partner|"
     "University Recruiter|Campus Recruiter", "operations", "recruiter"),
    ("Engineering Manager", "Hiring Manager|Head of Engineering|Director of Engineering",
     "leadership", "hiring"),
    ("Team Lead", "Tech Lead|Head of Data Science|Head of AI|Head of Research|Research Lead",
     "leadership", "hiring"),
)

_PARENS = re.compile(r"\s*[\(\[].*?[\)\]]\s*")

#: A person's own title outranks the search that found them: web search is
#: loose (without operators especially), and "Engineering Manager at Waymo"
#: turns up the SWE-II who reports to one. Alumni stay alumni — a shared school
#: is the stronger opening whatever their title.
_TITLE_GROUPS = (
    ("recruiter", re.compile(r"recruit|talent|sourc(er|ing)|people partner|\bhr\b", re.I)),
    # "Inventor, Entrepreneur, CEO" came up in a recruiter search and stayed a
    # recruiter until CEO/CTO counted here.
    ("hiring", re.compile(r"\b(manager|head|director|lead|vp|vice president|chief|founder|"
                          r"ceo|cto|coo)\b", re.I)),
    ("peer", re.compile(r"engineer|scientist|researcher|developer|\bswe\b|analyst|"
                        r"member of technical staff|\bmts\b", re.I)),
)


def group_for(title: str | None, found_by: set[str]) -> str:
    """Which group a person belongs in: alumni if an alumni search found them,
    else what their own title says, else the search that found them."""
    if "alumni" in found_by:
        return "alumni"
    for group, pattern in _TITLE_GROUPS:
        if title and pattern.search(title):
            return group
    return next((g for g in GROUPS if g in found_by), "hiring")


@dataclass
class ContactRole:
    title: str
    aliases: list[str] = field(default_factory=list)
    family: str = "data_science"
    group: str = "hiring"
    priority: int = 1


# ────────────────────────────── the database ──────────────────────────────


def employer_settings(settings: Settings | None = None) -> Settings:
    """The same settings, pointed at the employer database beside the main one.
    Follows `DB_PATH`, so tests stay in their scratch folder."""
    settings = settings or get_settings()
    main = settings.db_file
    return settings.model_copy(update={"db_path": main.with_name(f"{main.stem}_employers.db")})


def employer_repos(settings: Settings | None = None) -> Repos:
    """Repositories over the employer database — except `quota`, which is the
    main database's: credits are one budget, whichever app spends them."""
    settings = settings or get_settings()
    repos = open_repos(employer_settings(settings))
    main = get_conn(settings)
    init_schema(main)
    repos.quota = QuotaRepo(main)
    return repos


def _folder(settings: Settings) -> pathlib.Path:
    folder = settings.db_file.parent / "employers"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ────────────────────────────── who to look for ─────────────────────────────


def contact_roles(settings: Settings | None = None) -> list[ContactRole]:
    """Recruiters and hiring managers: `config/contact_roles.csv` if it exists,
    the shipped defaults otherwise."""
    from ..config import config_dir

    path = config_dir() / "contact_roles.csv"
    rows: list[ContactRole] = []
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                title = (row.get("title") or "").strip()
                group = (row.get("group") or "hiring").strip()
                if not title or group not in GROUPS or str(row.get("active", "true")).lower() \
                        in ("false", "0", "no"):
                    continue
                rows.append(ContactRole(
                    title=title,
                    aliases=[a.strip() for a in (row.get("aliases") or "").split("|") if a.strip()],
                    family=(row.get("family") or "leadership").strip(),
                    group=group, priority=int(row.get("priority") or 1),
                ))
    return rows or [ContactRole(t, a.split("|"), f, g) for t, a, f, g in DEFAULT_ROLES]


def short_title(title: str) -> str:
    """"Research Engineer, Code RL (Reinforcement Learning)" → "Research Engineer".
    The searchable core of a job title, for finding peers."""
    core = _PARENS.sub(" ", title or "")
    core = re.split(r"\s[-–|/]\s|,", core)[0]
    core = re.sub(r"\b(senior|junior|jr|sr|lead|principal|staff|intern(ship)?|graduate|"
                  r"working student|m/w/d|f/m/d|w/m/d)\b\.?", " ", core, flags=re.I)
    return re.sub(r"\s+", " ", core).strip(" -–") or (title or "").strip()


def alumni_roles(profile: Any = None) -> list[ContactRole]:
    """Schools and past employers from the profile, as searchable "roles": the
    query `"Anthropic" "Nova University of Lisbon"` is how an alumnus is found."""
    if profile is None:
        try:
            from ..apply.loader import load_profile

            profile = load_profile()
        except Exception:  # noqa: BLE001 - no profile: no alumni search
            return []
    out: list[ContactRole] = []
    for entry in getattr(profile, "education", None) or []:
        institution = str((entry or {}).get("institution") or "")
        if not institution or re.search(r"lyceum|high school|gymnasium", institution, re.I):
            continue
        parts = [_PARENS.sub(" ", p).strip() for p in institution.split("|") if p.strip()]
        out.append(ContactRole(parts[0], parts[1:], "data_science", "alumni", 2))
    for entry in getattr(profile, "experience", None) or []:
        org = str(getattr(entry, "org", "") or "").strip()
        if org:
            out.append(ContactRole(org, [], "data_science", "alumni", 3))
    return out


def roles_for_job(job_title: str, *, profile: Any = None,
                  settings: Settings | None = None,
                  groups: tuple[str, ...] = GROUPS) -> list[ContactRole]:
    """Every role row one employer search runs, each tagged with its group."""
    rows = [r for r in contact_roles(settings) if r.group in groups]
    if "peer" in groups and job_title:
        rows.append(ContactRole(short_title(job_title), [], "data_science", "peer", 1))
    if "alumni" in groups:
        rows += alumni_roles(profile)
    seen: set[str] = set()
    unique = []
    for row in rows:
        key = row.title.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def _groups_path(settings: Settings) -> pathlib.Path:
    return _folder(settings) / "groups.json"


def _remember_groups(settings: Settings, rows: list[ContactRole]) -> None:
    path = _groups_path(settings)
    known = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    known.update({r.title.casefold(): r.group for r in rows})
    path.write_text(json.dumps(known, indent=2, ensure_ascii=False), encoding="utf-8")


def _groups(settings: Settings) -> dict[str, str]:
    path = _groups_path(settings)
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


# ───────────────────────────── inputs on disk ──────────────────────────────

COMPANY_HEADER = ("name", "aliases", "negative_keywords", "linkedin_slug", "domain",
                  "series", "country", "tier", "active")
ROLE_HEADER = ("title", "aliases", "family", "seniority", "keywords", "priority", "active")


def upsert_employer(company: str, *, domain: str = "", aliases: tuple[str, ...] = (),
                    settings: Settings | None = None) -> pathlib.Path:
    """Add an employer to `data/employers/companies.csv` (never config/)."""
    settings = settings or get_settings()
    path = _folder(settings) / "companies.csv"
    rows: dict[str, dict[str, str]] = {}
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            rows = {r["name"].casefold(): r for r in csv.DictReader(fh) if r.get("name")}
    key = company.strip().casefold()
    existing = rows.get(key, {})
    rows[key] = {
        "name": company.strip(),
        "aliases": "|".join(dict.fromkeys(
            [a for a in (existing.get("aliases") or "").split("|") if a] + list(aliases))),
        "negative_keywords": existing.get("negative_keywords", ""),
        "linkedin_slug": existing.get("linkedin_slug", ""),
        "domain": domain or existing.get("domain", ""),
        "series": "job application", "country": existing.get("country", ""),
        "tier": "1", "active": "true",
    }
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(COMPANY_HEADER))
        writer.writeheader()
        writer.writerows(rows.values())
    return path


def _write_roles(rows: list[ContactRole], settings: Settings) -> pathlib.Path:
    path = _folder(settings) / "roles.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(ROLE_HEADER))
        writer.writeheader()
        for r in rows:
            writer.writerow({"title": r.title, "aliases": "|".join(r.aliases), "family": r.family,
                             "seniority": "", "keywords": "", "priority": str(r.priority),
                             "active": "true"})
    _remember_groups(settings, rows)
    return path


# ─────────────────────────────── the search ───────────────────────────────


def find_people(company: str, *, job_title: str = "", domain: str = "",
                groups: tuple[str, ...] = GROUPS, max_queries: int = 12, use_llm: bool = False,
                on_progress: Any = None, settings: Settings | None = None,
                profile: Any = None, pipeline_factory: Any = None) -> Any:
    """Search the web for people at `company`, into the employer database.

    `use_llm` is off by default: the AI judgement shares the Groq budget that
    letters are written from, and a note to a recruiter does not need it.
    Returns PitWall's `RunStats`.
    """
    from ..pipeline import Pipeline, RunParams

    settings = settings or get_settings()
    rows = roles_for_job(job_title, profile=profile, settings=settings, groups=groups)
    companies = upsert_employer(company, domain=domain, settings=settings)
    roles = _write_roles(rows, settings)
    repos = employer_repos(settings)
    factory = pipeline_factory or Pipeline
    pipe = factory(employer_settings(settings), repos, on_progress=on_progress,
                   inputs=(companies, roles))
    return pipe.run(RunParams(companies=[company], max_queries=max_queries, use_llm=use_llm))


def people_at(company: str, *, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Everyone found at this employer, each with the group whose search found
    them, their best score and their best email address, best first."""
    settings = settings or get_settings()
    repos = employer_repos(settings)
    conn = repos.conn
    row = conn.execute("SELECT id FROM companies WHERE lower(name) = lower(?)",
                       (company.strip(),)).fetchone()
    if row is None:
        return []
    groups = _groups(settings)
    people: dict[int, dict[str, Any]] = {}
    for r in conn.execute(
        """
        SELECT c.id, c.full_name, c.headline, c.current_title, c.current_company,
               c.location, c.profile_url, ro.title AS role_title,
               (SELECT MAX(s.final_score) FROM scores s WHERE s.candidate_id = c.id) AS score,
               (SELECT s.verdict FROM scores s WHERE s.candidate_id = c.id
                 ORDER BY s.final_score DESC LIMIT 1) AS verdict
          FROM candidates c
          JOIN candidate_sources cs ON cs.candidate_id = c.id
          JOIN search_results sr ON sr.id = cs.search_result_id
          JOIN queries q ON q.id = sr.query_id
          LEFT JOIN roles ro ON ro.id = q.role_id
         WHERE q.company_id = ?
        """,
        (int(row["id"]),),
    ):
        person = people.setdefault(int(r["id"]), {**dict(r), "groups": set()})
        group = groups.get(str(r["role_title"] or "").casefold())
        if group:
            person["groups"].add(group)
        if group == "alumni" and r["role_title"]:
            # An alumni search's "role" is the school or employer you share —
            # what an alumni note names.
            person.setdefault("shared", str(r["role_title"]))
    emails = repos.contacts.for_candidates(list(people))
    out = []
    for pid, person in people.items():
        best = (emails.get(pid) or [None])[0]
        found_by = set(person["groups"])
        person["groups"] = [g for g in GROUPS if g in found_by]
        person["group"] = group_for(person.get("current_title") or person.get("headline"),
                                    found_by)
        person["email"] = best.get("email") if best else None
        person["email_status"] = best.get("status") if best else None
        person.pop("role_title", None)
        out.append(person)
    return sorted(out, key=lambda p: (GROUPS.index(p["group"]), -(p.get("score") or 0)))


def known_in_pitwall(conn: sqlite3.Connection, company: str, *, limit: int = 20
                     ) -> list[dict[str, Any]]:
    """People you already have in PitWall who work at this employer. Read-only."""
    target = normalize_company(company)
    if not target:
        return []
    first = target.split()[0]
    rows = conn.execute(
        "SELECT id, full_name, headline, current_title, current_company, profile_url"
        " FROM candidates WHERE lower(current_company) LIKE ? LIMIT 500",
        (f"%{first}%",),
    ).fetchall()
    return [dict(r) for r in rows if normalize_company(r["current_company"] or "") == target
            ][:limit]


def employer_domain(company: str, *, settings: Settings | None = None) -> str:
    """The email domain the free chain will use for this employer, if known."""
    repos = employer_repos(settings)
    row = repos.conn.execute("SELECT id, domain FROM companies WHERE lower(name) = lower(?)",
                             (company.strip(),)).fetchone()
    if row is None:
        return ""
    domains = repos.domains.for_company(int(row["id"]))
    manual = [d["domain"] for d in domains if int(d["manual_override"] or 0)]
    return str((manual or [d["domain"] for d in domains] or [row["domain"] or ""])[0])


def set_employer_domain(company: str, domain: str, *,
                        settings: Settings | None = None) -> str:
    """Tell the free email chain which domain this employer's addresses use.

    Without SearXNG the chain cannot look an employer's site up, and a job
    posting's own host (careers.withwaymo.com) is not where the staff's mail
    lives — so the owner's answer is taken, written to the employer list for
    the next search and, if the employer is already known, set as PitWall's
    manual override (the first thing the domain step reads).
    """
    from ..enrich.domains import set_manual_domain

    settings = settings or get_settings()
    repos = employer_repos(settings)
    row = repos.conn.execute("SELECT id FROM companies WHERE lower(name) = lower(?)",
                             (company.strip(),)).fetchone()
    if row is not None:
        cleaned = set_manual_domain(repos, int(row["id"]), domain)
    else:
        from ..enrich.domains import domain_from_url, registrable_domain

        value = domain.strip()
        cleaned = (domain_from_url(value) if "/" in value else registrable_domain(value)) or ""
        if not cleaned:
            raise ValueError(f"{domain!r} is not a usable domain")
    upsert_employer(company, domain=cleaned, settings=settings)
    return cleaned
