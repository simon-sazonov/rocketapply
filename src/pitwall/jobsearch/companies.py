"""Target employers, and the name normalisation dedupe depends on — §5, §5.2.

`config/target_companies.csv` is the job-search list. It is deliberately not
`config/companies.csv`: that one drives the LinkedIn people sweep, and a job
target is not automatically somebody to search for people at.

Normalisation is intentionally plain. "BMW", "BMW Group" and "BMW AG" all
become `bmw` because legal suffixes and "group" carry no identity; anything
subtler — "Amazon" vs "AWS" — is what the `aliases` column is for, because a
rule that guessed there would be wrong somewhere else.
"""

from __future__ import annotations

import csv
import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import config_dir
from ..db import loads
from ..models import ValidationReport
from .locations import country_code

TARGETS_FILE = "target_companies.csv"

TARGET_COLUMNS = (
    "name", "aliases", "country", "cities", "industry", "priority", "careers_url",
    "ats_kind", "ats_token", "sponsorship", "visa_notes", "tags", "active",
)
PRIORITIES = ("A", "B", "C")
SPONSORSHIP_VALUES = ("yes", "sometimes", "no", "unknown")

#: Legal forms and filler that never distinguish one employer from another.
_SUFFIXES = {
    "ag", "gmbh", "se", "kg", "kgaa", "mbh", "ug", "co", "inc", "incorporated", "corp",
    "corporation", "ltd", "limited", "llc", "llp", "plc", "lp", "bv", "nv", "sa", "sas",
    "sarl", "spa", "srl", "sl", "oy", "oyj", "ab", "as", "asa", "aps", "sp", "zoo", "pty",
    "group", "holding", "holdings", "company", "the", "international", "global",
    "technologies", "technology", "labs", "lab", "ai",
}
# "labs", "technologies" and a trailing "AI" are stripped too: "Wayve
# Technologies" and "Wayve", "Mistral AI" and "Mistral.ai" are one employer
# each, and no two employers worth tracking differ only by that word.


def _ascii_fold(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def normalize_company(name: str | None) -> str:
    """`"BMW Group"`, `"BMW AG"`, `"bmw"` -> `"bmw"`. Empty in, empty out."""
    if not name:
        return ""
    text = _ascii_fold(name).lower().replace("&", " and ")
    # "Mistral.ai", "Booking.com": the domain is the brand.
    text = re.sub(r"\.(?:com|ai|io|co|net|org|de|fr|nl|uk|eu|tech)\b", " ", text)
    # "B.V.", "S.p.A.": dotted abbreviations are one word.
    text = re.sub(r"\b([a-z])\.", r"\1", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    words = text.split()
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    while len(words) > 1 and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def company_norms(name: str | None, aliases: Iterable[str] = ()) -> set[str]:
    """Every normalised spelling that identifies one employer: its name and aliases."""
    out = {normalize_company(name)}
    out.update(normalize_company(a) for a in aliases)
    return {n for n in out if n}


#: "(m/w/d)", "(f/m/x)", "(all genders)", "(H/F)", "m/f/d", "(Mensch)" …
_GENDER_RE = re.compile(
    r"\(\s*(?:[mwfdhxn]\s*[/|,*]\s*){1,3}[mwfdhxn]\s*\)|\b(?:[mwfdh]\s*/\s*){2}[mwfdxn]\b|"
    r"\((?:all genders?|alle geschlechter|mensch|gn|genderneutral|w/m/d|m/w/d|x/f/m)\)",
    re.I,
)
_TITLE_ABBREV = {
    "sr": "senior", "jr": "junior", "ml": "machine learning", "ai": "ai",
    "eng": "engineer", "engr": "engineer", "dev": "developer", "mgr": "manager",
    "ds": "data scientist", "swe": "software engineer",
    "optimisation": "optimization", "modelling": "modeling", "analyse": "analyst",
}
#: Words that decorate a title without changing the vacancy.
_TITLE_NOISE = {"full", "time", "fulltime", "permanent", "remote", "hybrid", "onsite",
                "and", "the", "a", "an", "of", "for", "in", "at", "to"}


def normalize_title(title: str | None) -> str:
    """A title with the noise off, words in their original order — for display
    and for fuzzy comparison. `"Sr. ML Engineer (m/w/d)"` -> `"senior machine
    learning engineer"`."""
    if not title:
        return ""
    text = _GENDER_RE.sub(" ", title)
    text = _ascii_fold(text).lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9+#]+", " ", text)
    words = []
    for word in text.split():
        words.extend(_TITLE_ABBREV.get(word, word).split())
    return " ".join(words)


def title_key(title: str | None) -> str:
    """Order-free identity of a title: the SET of its meaningful words.

    "Graduate Machine Learning Engineer" and "Machine Learning Engineer –
    Graduate" are the same vacancy written two ways (§11); their keys match.
    """
    words = {w for w in normalize_title(title).split() if w not in _TITLE_NOISE}
    return " ".join(sorted(words))


# ───────────────────────────── the target list ───────────────────────────


@dataclass
class TargetCompany:
    name: str
    aliases: list[str] = field(default_factory=list)
    country: str | None = None
    cities: list[str] = field(default_factory=list)
    industry: str | None = None
    priority: str = "B"
    careers_url: str | None = None
    ats_kind: str | None = None
    ats_token: str | None = None
    sponsorship: str = "unknown"
    visa_notes: str | None = None
    tags: list[str] = field(default_factory=list)
    active: bool = True

    @property
    def norms(self) -> set[str]:
        """Every normalised spelling that identifies this employer."""
        return company_norms(self.name, self.aliases)

    def as_row(self) -> dict[str, str]:
        return {
            "name": self.name,
            "aliases": "|".join(self.aliases),
            "country": self.country or "",
            "cities": "|".join(self.cities),
            "industry": self.industry or "",
            "priority": self.priority,
            "careers_url": self.careers_url or "",
            "ats_kind": self.ats_kind or "",
            "ats_token": self.ats_token or "",
            "sponsorship": self.sponsorship,
            "visa_notes": self.visa_notes or "",
            "tags": "|".join(self.tags),
            "active": "true" if self.active else "false",
        }


def verify_board(name: str, aliases: Iterable[str], token: str, postings: list[Any],
                 *, alias_tokens: bool = True) -> tuple[bool, str]:
    """Is this ATS board really the company's? `(ok, reason)`. Pure.

    It takes a name and aliases rather than a row so that both company lists
    can use it: the job search's `TargetCompany` and the people search's
    `companies` rows, which `scripts/discover_ats.py` probes the same way.

    "It answered with postings" is not enough, and the first live discovery
    run showed why: `recruitee:google` is an empty account whose one posting is
    "Senior Marketer (Sample)", `lever:aleph` is an ad-sales agency rather than
    Aleph Alpha, and `ashby:novo` is a New York bank rather than Novo Nordisk.
    Accepted, each would have filed a stranger's jobs under a priority target.

    So a board must either NAME the company (Greenhouse, Workable,
    SmartRecruiters and Recruitee put a company name on every posting), or —
    for boards that name nobody (Lever, Ashby) — sit at a token that is the
    company's whole name, not a fragment of it.

    `alias_tokens=False` lets only the name itself vouch for a board that
    names nobody; aliases still count when the postings name the company. The
    people-search list needs that, because its aliases are the short forms
    people type on LinkedIn, chosen for recall ("Genesis", "Carlin", "VAR").
    Its first live run adopted `lever:genesis` for Genesis Magma Racing, and
    that board belongs to Genesis Global, a capital-markets software firm.
    """
    if not postings:
        return False, "no postings"
    titles = [str(getattr(p, "title", "")) for p in postings]
    if all("(sample)" in t.lower() for t in titles):
        return False, "a placeholder board (only sample postings)"
    norms = company_norms(name, aliases)
    names = {normalize_company(getattr(p, "company_name", None)) for p in postings}
    names.discard("")
    if names:
        if names & norms:
            return True, "the postings name this company"
        return False, "the postings name " + ", ".join(sorted(names)[:3])
    def whole(spellings: set[str]) -> set[str]:
        return {re.sub(r"[^a-z0-9]", "", n) for n in spellings}

    if token.lower() in whole(norms if alias_tokens else company_norms(name)):
        return True, "the board is at the company's own name"
    what = "only an alias" if token.lower() in whole(norms) else "only part of the name"
    return False, (f"the board names no company and {token!r} is {what} — "
                   "add it by hand if it really is theirs")


def targets_csv() -> Path:
    return config_dir() / TARGETS_FILE


def _split(value: Any) -> list[str]:
    return [p.strip() for p in str(value or "").split("|") if p.strip()]


def load_targets(path: Path | str | None = None) -> tuple[list[TargetCompany], ValidationReport]:
    """Read and validate the target list. A missing file is an empty list."""
    from ..inputs import _as_bool, _read_rows
    from ..sources.jobs import ATS_KINDS

    p = Path(path) if path else targets_csv()
    report = ValidationReport()
    if not p.exists():
        return [], report
    rows, fields = _read_rows(p)
    if "name" not in fields:
        report.add(p.name, 0, "the header must include a `name` column", column="name")
        return [], report
    out: list[TargetCompany] = []
    seen: set[str] = set()
    for i, row in enumerate(rows, start=2):
        report.rows_read += 1
        name = (row.get("name") or "").strip()
        if not name:
            continue
        key = normalize_company(name)
        if key in seen:
            report.add(p.name, i, f"{name!r} duplicates an earlier row — skipped",
                       column="name", value=name)
            continue
        seen.add(key)
        priority = (row.get("priority") or "B").strip().upper() or "B"
        if priority not in PRIORITIES:
            report.add(p.name, i, f"priority must be A, B or C — {priority!r} read as B",
                       column="priority", level="warning", value=priority)
            priority = "B"
        raw_country = (row.get("country") or "").strip()
        country = country_code(raw_country) if raw_country else None
        if raw_country and not country and raw_country.lower() not in {"multi", "multiple",
                                                                       "global", "europe"}:
            report.add(p.name, i, f"country {raw_country!r} is not recognised — left blank",
                       column="country", level="warning", value=raw_country)
        ats_kind = (row.get("ats_kind") or "").strip().lower() or None
        if ats_kind and ats_kind not in ATS_KINDS:
            report.add(p.name, i, f"ats_kind must be one of {sorted(ATS_KINDS)}",
                       column="ats_kind", value=ats_kind)
            ats_kind = None
        sponsorship = (row.get("sponsorship") or "unknown").strip().lower() or "unknown"
        if sponsorship not in SPONSORSHIP_VALUES:
            report.add(p.name, i, f"sponsorship must be one of {SPONSORSHIP_VALUES}",
                       column="sponsorship", level="warning", value=sponsorship)
            sponsorship = "unknown"
        out.append(TargetCompany(
            name=name,
            aliases=_split(row.get("aliases")),
            country=country,
            cities=_split(row.get("cities")),
            industry=(row.get("industry") or "").strip() or None,
            priority=priority,
            careers_url=(row.get("careers_url") or "").strip() or None,
            ats_kind=ats_kind,
            ats_token=(row.get("ats_token") or "").strip() or None,
            sponsorship=sponsorship,
            visa_notes=(row.get("visa_notes") or "").strip() or None,
            tags=_split(row.get("tags")),
            active=_as_bool(row.get("active"), True),
        ))
        report.rows_ok += 1
    return out, report


def save_targets(targets: list[TargetCompany], path: Path | str | None = None) -> Path:
    """Write the list back, keeping the `#` documentation block at the top —
    it is the only place a column's meaning is written down next to the data."""
    p = Path(path) if path else targets_csv()
    p.parent.mkdir(parents=True, exist_ok=True)
    header: list[str] = []
    if p.exists():
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            if not line.lstrip().startswith("#"):
                break
            header.append(line)
    with p.open("w", newline="", encoding="utf-8") as fh:
        for line in header:
            fh.write(line + "\n")
        writer = csv.DictWriter(fh, fieldnames=list(TARGET_COLUMNS))
        writer.writeheader()
        for target in targets:
            writer.writerow(target.as_row())
    return p


def sync_targets(conn: sqlite3.Connection, targets: list[TargetCompany]) -> int:
    """Mirror the CSV into `target_companies`. The CSV is the truth.

    A company removed from the CSV is marked inactive, not deleted: its
    applications still point at it. The link to the people-search list is
    recomputed each time, by normalised name and aliases.
    """
    import json

    networking = [dict(r) for r in conn.execute("SELECT id, name, aliases FROM companies")]
    net_index: dict[str, int] = {}
    for row in networking:
        for spelling in [row["name"], *loads(row.get("aliases"))]:
            key = normalize_company(spelling)
            if key:
                net_index.setdefault(key, int(row["id"]))

    names = []
    for t in targets:
        names.append(t.name)
        link = next((net_index[n] for n in t.norms if n in net_index), None)
        conn.execute(
            """
            INSERT INTO target_companies (name, aliases, country, cities, industry, priority,
                careers_url, ats_kind, ats_token, sponsorship, visa_notes, tags, active,
                networking_company_id, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
            ON CONFLICT(name) DO UPDATE SET
                aliases=excluded.aliases, country=excluded.country, cities=excluded.cities,
                industry=excluded.industry, priority=excluded.priority,
                careers_url=excluded.careers_url, ats_kind=excluded.ats_kind,
                ats_token=excluded.ats_token, sponsorship=excluded.sponsorship,
                visa_notes=excluded.visa_notes, tags=excluded.tags, active=excluded.active,
                networking_company_id=excluded.networking_company_id,
                updated_at=datetime('now')
            """,
            (t.name, json.dumps(t.aliases), t.country, json.dumps(t.cities), t.industry,
             t.priority, t.careers_url, t.ats_kind, t.ats_token, t.sponsorship, t.visa_notes,
             json.dumps(t.tags), int(t.active), link),
        )
    placeholders = ",".join("?" for _ in names) or "''"
    conn.execute(
        f"UPDATE target_companies SET active = 0 WHERE name NOT IN ({placeholders})", names
    )
    return len(targets)


def target_rows(conn: sqlite3.Connection, *, active_only: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM target_companies"
    if active_only:
        sql += " WHERE active = 1"
    out = []
    for r in conn.execute(sql + " ORDER BY priority, name"):
        row = dict(r)
        for key in ("aliases", "cities", "tags"):
            row[key] = loads(row.get(key))
        out.append(row)
    return out


class TargetIndex:
    """Normalised spelling -> target row, for linking postings to employers."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = {int(r["id"]): r for r in rows}
        self._by_norm: dict[str, int] = {}
        for r in rows:
            for spelling in [r["name"], *(r.get("aliases") or [])]:
                key = normalize_company(spelling)
                if key:
                    self._by_norm.setdefault(key, int(r["id"]))

    def match(self, company_norm: str | None) -> dict[str, Any] | None:
        if not company_norm:
            return None
        target_id = self._by_norm.get(company_norm)
        return self.rows.get(target_id) if target_id is not None else None

    def get(self, target_id: int | None) -> dict[str, Any] | None:
        return self.rows.get(int(target_id)) if target_id is not None else None
