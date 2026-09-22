"""Importing a company list — §5 and §78 of the spec ("import or manually
maintain a list of approximately 100+ companies").

Forgiving about shape, strict about content. Accepted:

* a CSV with the full `target_companies.csv` columns, or any subset of them;
* a CSV whose company column is called `company`, `employer`,
  `organisation` … instead of `name`;
* a plain list, one company per line, no header at all.

Every row then goes through the same validation as the file itself
(`load_targets`), so an upload can never put something into the list that the
list would reject on its own.
"""

from __future__ import annotations

import csv
import io
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .companies import TARGET_COLUMNS, TargetCompany, load_targets, normalize_company

#: Header spellings accepted for the company-name column.
_NAME_HEADERS = ("name", "company", "company name", "company_name", "employer",
                 "organisation", "organization", "firm")
#: Friendlier spellings for the other columns.
_ALIASES = {
    "alias": "aliases", "other names": "aliases", "city": "cities", "location": "cities",
    "sector": "industry", "careers": "careers_url", "careers page": "careers_url",
    "url": "careers_url", "website": "careers_url", "ats": "ats_kind", "board": "ats_token",
    "visa": "sponsorship", "sponsors": "sponsorship", "notes": "visa_notes", "tag": "tags",
}


@dataclass
class ImportResult:
    companies: list[TargetCompany] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def parse_upload(data: bytes | str) -> tuple[list[TargetCompany], list[str]]:
    """The companies in an uploaded file, validated. `(companies, problems)`."""
    text = data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else data
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return [], ["the file is empty"]

    reader = csv.reader(io.StringIO("\n".join(lines)))
    rows = list(reader)
    header = [h.strip().lower() for h in rows[0]]
    name_col = next((i for i, h in enumerate(header) if h in _NAME_HEADERS), None)

    records: list[dict[str, str]] = []
    if name_col is None:
        # No recognisable header: one company per line (first cell).
        records = [{"name": row[0].strip()} for row in rows if row and row[0].strip()]
    else:
        columns = []
        for i, h in enumerate(header):
            key = "name" if i == name_col else _ALIASES.get(h, h)
            columns.append(key if key in TARGET_COLUMNS else None)
        for row in rows[1:]:
            record = {key: (row[i].strip() if i < len(row) else "")
                      for i, key in enumerate(columns) if key}
            if record.get("name"):
                records.append(record)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "upload.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(TARGET_COLUMNS))
            writer.writeheader()
            for record in records:
                writer.writerow({c: record.get(c, "") for c in TARGET_COLUMNS})
        companies, report = load_targets(path)
    problems = [f"row {i.row}: {i.message}" for i in report.issues]
    return companies, problems


def merge(existing: list[TargetCompany], incoming: list[TargetCompany], *,
          replace: bool = False) -> ImportResult:
    """Combine an upload with the current list.

    Add (the default): new companies are appended; a company already on the
    list keeps everything you set, and only its EMPTY fields are filled from
    the upload. Replace: the upload becomes the list — but a company that was
    already there keeps its discovered ATS board, which took a network probe
    to find.
    """
    result = ImportResult()
    by_norm = {normalize_company(c.name): c for c in existing}
    if replace:
        for company in incoming:
            old = by_norm.get(normalize_company(company.name))
            if old and not company.ats_kind:
                company.ats_kind, company.ats_token = old.ats_kind, old.ats_token
            (result.updated if old else result.added).append(company.name)
            result.companies.append(company)
        return result

    result.companies = list(existing)
    for company in incoming:
        old = by_norm.get(normalize_company(company.name))
        if old is None:
            result.companies.append(company)
            by_norm[normalize_company(company.name)] = company
            result.added.append(company.name)
            continue
        changed = False
        for field_name in ("country", "industry", "careers_url", "ats_kind", "ats_token",
                           "visa_notes"):
            if not getattr(old, field_name) and getattr(company, field_name):
                setattr(old, field_name, getattr(company, field_name))
                changed = True
        for list_field in ("aliases", "cities", "tags"):
            extra = [v for v in getattr(company, list_field) if v not in getattr(old, list_field)]
            if extra:
                getattr(old, list_field).extend(extra)
                changed = True
        if old.sponsorship == "unknown" and company.sponsorship != "unknown":
            old.sponsorship = company.sponsorship
            changed = True
        if changed:
            result.updated.append(old.name)
    return result
