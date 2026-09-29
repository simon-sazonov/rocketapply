"""Public registers of employers licensed to sponsor work visas.

For someone who needs sponsorship everywhere but Portugal, "does this employer
sponsor?" is the question most postings leave unanswered. Three governments
publish the answer, free:

* **UK**: the Register of Licensed Sponsors (Workers), a CSV on gov.uk
  updated daily (~11 MB, 28 Sep 2026): Organisation Name, Town/City, County,
  Type & Rating, Route. The link changes daily, so it is read from the
  publication page each time.
* **NL**: the IND's public register of recognised sponsors for regular labour
  and highly skilled migrants, one HTML table (12,985 rows, 28 Sep 2026):
  Organisation, KVK number.
* **US**: USCIS's H-1B Employer Data Hub. It refuses this client's honest
  User-Agent (403), so the owner downloads the CSV in a browser into
  `data/sponsors/` and it is read from there. The column names differ between
  years, so they are found by what they contain.

The registers are refreshed weekly and applied at SCORING time, not when a
posting is normalised. Scoring reruns over every stored job, so a register
update reaches all of them without re-reading anything.

Matching is on the normalised company name, exact. It is tried once more with
country and group words removed from both sides, so "Google UK Limited" meets
"Google". There is no fuzzy matching: a wrong "licensed sponsor" is worse than
a missing one.
"""

from __future__ import annotations

import csv
import html
import io
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .companies import normalize_company

log = logging.getLogger(__name__)

UK_PAGE = "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers"
NL_PAGE = ("https://ind.nl/en/public-register-recognised-sponsors/"
           "public-register-regular-labour-and-highly-skilled-migrants")
REFRESH_DAYS = 7

UK_FILE = "uk_licensed_sponsors.csv"
NL_FILE = "nl_recognised_sponsors.csv"

#: Words that say where or how big an employer is, not who it is.
_NOISE = frozenset({
    "uk", "gb", "united", "kingdom", "england", "london", "europe", "emea", "eu",
    "international", "global", "group", "holding", "holdings", "nederland", "netherlands",
    "benelux", "the", "services", "usa", "us", "america", "americas", "north",
    # US filing forms: "ANTHROPIC PBC", "OPENAI OPCO LLC".
    "pbc", "opco", "llc", "inc", "corp", "corporation", "co",
})


def _loose(name: str | None) -> str:
    norm = normalize_company(name)
    return " ".join(w for w in norm.split() if w not in _NOISE)


@dataclass
class Register:
    country: str
    label: str
    #: normalised name -> what the register says about it
    exact: dict[str, str] = field(default_factory=dict)
    loose: dict[str, str] = field(default_factory=dict)

    def add(self, name: str, detail: str) -> None:
        norm = normalize_company(name)
        if norm:
            self.exact.setdefault(norm, detail)
        lo = _loose(name)
        if lo and len(lo) >= 3:
            self.loose.setdefault(lo, detail)

    def find(self, company: str | None) -> str | None:
        if not company:
            return None
        return self.exact.get(normalize_company(company)) or self.loose.get(_loose(company))

    def __len__(self) -> int:
        return len(self.exact)


class SponsorIndex:
    """Every loaded register, looked up by the job's country."""

    def __init__(self, registers: list[Register] | None = None) -> None:
        self.registers = {r.country: r for r in registers or [] if len(r)}

    def lookup(self, company: str | None, countries: list[str]) -> str | None:
        """What a register says about `company` in one of the job's countries."""
        for country in countries:
            register = self.registers.get(country)
            hit = register.find(company) if register else None
            if hit:
                return f"{register.label}: {hit}" if register else hit
        return None

    def __bool__(self) -> bool:
        return bool(self.registers)

    def summary(self) -> str:
        return ", ".join(f"{c} {len(r):,}" for c, r in sorted(self.registers.items()))


# ────────────────────────────── the parsers ──────────────────────────────


def parse_uk(text: str) -> Register:
    register = Register("GB", "UK register of licensed sponsors")
    for row in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        name = (row.get("Organisation Name") or "").strip()
        route = (row.get("Route") or "").strip()
        rating = (row.get("Type & Rating") or "").strip()
        if not name or "skilled worker" not in route.lower() and "global business" not in \
                route.lower():
            continue
        register.add(name, ", ".join(p for p in (route, rating) if p))
    return register


def parse_nl(page: str) -> Register:
    register = Register("NL", "IND recognised sponsor")
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, flags=re.S):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip().strip('"')
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)]
        if cells and cells[0]:
            register.add(cells[0].replace('""', '"'), "regular labour and highly skilled migrants")
    return register


def read_text(path: Path) -> str:
    """The Data Hub download is UTF-16 with a byte-order mark (checked on the
    file downloaded on 28 Sep 2026); other exports are UTF-8."""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8", errors="replace")


def parse_us(text: str) -> Register:
    """USCIS H-1B Employer Data Hub export, any year's layout: comma- or
    tab-separated, with the approval columns found by name."""
    register = Register("US", "USCIS H-1B employer data")
    text = text.lstrip("\ufeff")
    first = text.split("\n", 1)[0]
    delimiter = "\t" if first.count("\t") > first.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = next(reader, None) or []
    lower = [h.strip().lower() for h in header]

    def col(*needles: str) -> int | None:
        for i, h in enumerate(lower):
            if all(n in h for n in needles):
                return i
        return None

    name_i = col("employer") or col("petitioner", "name")
    year_i = col("fiscal", "year") or col("year")
    approvals = [i for i, h in enumerate(lower) if "approv" in h]
    if name_i is None:
        return register
    best: dict[str, tuple[int, str]] = {}
    for row in reader:
        if len(row) <= name_i:
            continue
        name = row[name_i].strip()
        count = 0
        for i in approvals:
            try:
                count += int(float((row[i] if i < len(row) else "0").replace(",", "") or 0))
            except ValueError:
                continue
        year = row[year_i].strip() if year_i is not None and year_i < len(row) else ""
        if name and count > 0:
            prev = best.get(name)
            if prev is None or count > prev[0]:
                best[name] = (count, year)
    for name, (count, year) in best.items():
        register.add(name, f"{count} H-1B approval(s)" + (f" in FY{year}" if year else ""))
    return register


# ─────────────────────────── refresh and load ────────────────────────────


def sponsors_dir(settings: Any) -> Path:
    return Path(settings.db_file).parent / "sponsors"


def _stale(path: Path) -> bool:
    return not path.exists() or time.time() - path.stat().st_mtime > REFRESH_DAYS * 86400


def refresh(settings: Any, client: Any, *, force: bool = False) -> list[str]:
    """Download the UK and NL registers when older than a week. Returns notes.

    Goes through the polite client, so robots.txt and the User-Agent rules
    are the same as for any job board. A failure keeps the previous copy.
    """
    folder = sponsors_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    uk = folder / UK_FILE
    if force or _stale(uk):
        try:
            page = client.get(UK_PAGE, no_cache=True)
            links = re.findall(r"https://assets\.publishing\.service\.gov\.uk/[^\"']+\.csv", page)
            if not links:
                raise ValueError("no CSV link on the publication page")
            uk.write_text(client.get(links[0], no_cache=True), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - a register is a bonus, never a failure
            notes.append(f"sponsors: the UK register could not be refreshed ({exc})")
    nl = folder / NL_FILE
    if force or _stale(nl):
        try:
            register = parse_nl(client.get(NL_PAGE, no_cache=True))
            if not len(register):
                raise ValueError("the page had no table rows")
            with nl.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["Organisation"])
                writer.writerows([[name] for name in register.exact])
        except Exception as exc:  # noqa: BLE001
            notes.append(f"sponsors: the NL register could not be refreshed ({exc})")
    return notes


def load(settings: Any) -> SponsorIndex:
    """Whatever registers are on disk. Missing files are simply absent."""
    folder = sponsors_dir(settings)
    registers: list[Register] = []
    uk = folder / UK_FILE
    if uk.exists():
        registers.append(parse_uk(uk.read_text(encoding="utf-8", errors="replace")))
    nl = folder / NL_FILE
    if nl.exists():
        reg = Register("NL", "IND recognised sponsor")
        for row in csv.DictReader(nl.open(encoding="utf-8")):
            reg.add(row.get("Organisation") or "", "regular labour and highly skilled migrants")
        registers.append(reg)
    us_files = sorted(p for p in folder.glob("*.csv")
                      if p.name not in {UK_FILE, NL_FILE})
    if us_files:
        us = Register("US", "USCIS H-1B employer data")
        for path in us_files:
            part = parse_us(read_text(path))
            for key, value in part.exact.items():
                us.exact.setdefault(key, value)
            for key, value in part.loose.items():
                us.loose.setdefault(key, value)
        registers.append(us)
    return SponsorIndex(registers)
