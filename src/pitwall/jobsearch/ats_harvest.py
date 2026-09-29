"""Company job boards, found from the links inside postings.

The aggregators find employers nobody put on a list; their postings often link
straight to the employer's own ATS board. On the first run of the expanded
search, the stored postings already named Lovable, 1Password and Omnea on
Ashby, Netlight on Lever, H&M, Vattenfall and AFRY on SmartRecruiters, and
sixteen Teamtailor boards. A board, once known, lists EVERY opening that
employer has, not just the ones an aggregator happened to carry, and with
the full description.

    postings ─► links ─► (ats, token) candidates ─► verify ─► discovered_boards ─► polled

* **Harvest** scans postings for board URLs. Only postings from non-ATS
  sources are scanned, and only those not scanned before (by id).
* **Verify** reads the board once and keeps it only if `verify_board` agrees
  it belongs to the employer that linked to it: its postings name that
  employer, or it sits at the employer's whole name. A board that fails is
  remembered with the reason and not retried for 30 days.
* **Poll** reads verified boards every run while they keep producing relevant
  jobs (one in the last 60 days), and weekly otherwise. So the list can grow
  without the daily request count growing with it.

Workday links are recorded but not polled: its career sites have no public
API, only per-company internal endpoints, and that adapter comes last.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .companies import normalize_company, verify_board

log = logging.getLogger(__name__)

#: kind -> the pattern whose first group is the board token.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("greenhouse", re.compile(
        r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:v1/boards/|embed/job_board\?for=)?"
        r"([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("recruitee", re.compile(r"\b([a-z0-9-]+)\.recruitee\.com")),
    ("personio", re.compile(r"\b([a-z0-9-]+)\.jobs\.personio\.(?:de|com)")),
    ("teamtailor", re.compile(r"\b([a-z0-9-]+)\.teamtailor\.com")),
    ("workday", re.compile(
        r"\b([a-z0-9-]+\.wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")),
)

#: Cheap substrings checked before any regex runs on a posting.
_NEEDLES = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com",
            "recruitee.com", "personio.", "teamtailor.com", "myworkdayjobs.com")

#: Path words and hosts' own subdomains, not companies.
JUNK_TOKENS = frozenset({
    "j", "app", "api", "v1", "embed", "jobs", "job", "careers", "career", "www", "apply",
    "search", "static", "assets", "cdn", "images", "img", "career-site", "support", "help",
    "blog", "about", "login", "signup", "en", "de", "fr",
})

#: What the job search can poll. The others are recorded with the reason.
POLLABLE = frozenset({"greenhouse", "lever", "ashby", "workable", "recruitee", "personio",
                      "teamtailor"})
NOT_POLLED = {
    "workday": "recorded; this job search cannot read Workday boards yet",
    # Checked 28 Sep 2026: "User-agent: LinkedInBot / Allow: /v1/companies/ /
    # User-agent: * / Disallow: /". Keyless, so the refusal stands.
    "smartrecruiters": "api.smartrecruiters.com/robots.txt disallows every client but "
                       "LinkedInBot",
}

RETRY_FAILED_DAYS = 30
RELEVANT_WINDOW_DAYS = 60
RECHECK_IDLE_DAYS = 7


@dataclass(frozen=True)
class Candidate:
    kind: str
    token: str
    company: str | None      # the employer the linking posting names


def tokens_in(text: str) -> list[tuple[str, str]]:
    """Every (kind, token) a piece of text links to. Pure."""
    if not text or not any(n in text for n in _NEEDLES):
        return []
    out: list[tuple[str, str]] = []
    for kind, pattern in PATTERNS:
        for match in pattern.finditer(text):
            token = match.group(1)
            if kind == "workday":
                token = f"{match.group(1)}/{match.group(2)}"
            token = token.strip(".-_")
            if not token or token.lower() in JUNK_TOKENS or len(token) < 2:
                continue
            if (kind, token) not in out:
                out.append((kind, token))
    return out


def known_boards(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """Boards already on one of the lists: never candidates."""
    known: set[tuple[str, str]] = set()
    for table in ("target_companies", "companies"):
        for row in conn.execute(
            f"SELECT ats_kind, ats_token FROM {table} WHERE ats_kind IS NOT NULL "
            "AND ats_kind != '' AND ats_token IS NOT NULL AND ats_token != ''"
        ):
            known.add((row[0], row[1].lower()))
    return known


def harvest(conn: sqlite3.Connection, *, since_id: int = 0) -> tuple[int, int]:
    """Record every new board linked from postings with id > `since_id`.

    New candidates are stored as pending (`checked_at` NULL) at once, so a run
    that can only verify some of them loses none. Workday links are stored as
    already judged: there is no adapter to verify them with yet.
    Returns (new candidates, the highest id scanned).
    """
    known = known_boards(conn)
    found: dict[tuple[str, str], Candidate] = {}
    top = since_id
    rows = conn.execute(
        """
        SELECT j.id, j.url, j.description, j.raw_json, j.posting_company
          FROM jobs j JOIN job_sources s ON s.id = j.source_id
         WHERE j.id > ? AND s.kind != 'ats_json'
         ORDER BY j.id
        """,
        (since_id,),
    ).fetchall()
    for row in rows:
        top = max(top, int(row[0]))
        blob = " ".join(x or "" for x in (row[1], row[2], row[3]))
        for kind, token in tokens_in(blob):
            key = (kind, token.lower())
            if key not in known and key not in found:
                found[key] = Candidate(kind, token, row[4])
    added = 0
    for c in found.values():
        judged = c.kind not in POLLABLE
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO discovered_boards (kind, token, company, company_norm,
                                                     verified, reason, checked_at)
            VALUES (?, ?, ?, ?, 0, ?, CASE WHEN ? THEN datetime('now') END)
            """,
            (c.kind, c.token, c.company, normalize_company(c.company) or None,
             NOT_POLLED.get(c.kind, "not polled") if judged else "pending", int(judged)),
        )
        added += cur.rowcount or 0
    return added, top


def pending(conn: sqlite3.Connection, limit: int) -> list[Candidate]:
    """Boards to verify this run: never checked first, then failures old
    enough to deserve another look."""
    rows = conn.execute(
        f"""
        SELECT kind, token, company FROM discovered_boards
         WHERE verified = 0 AND (checked_at IS NULL
               OR checked_at < datetime('now', '-{RETRY_FAILED_DAYS} days'))
         ORDER BY checked_at IS NOT NULL, first_seen_at
         LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    return [Candidate(r[0], r[1], r[2]) for r in rows if r[0] in POLLABLE]


def record(conn: sqlite3.Connection, candidate: Candidate, *, verified: bool, reason: str,
           postings: int = 0) -> None:
    conn.execute(
        """
        INSERT INTO discovered_boards (kind, token, company, company_norm, verified, reason,
                                       postings, checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(kind, token) DO UPDATE SET
            company = COALESCE(excluded.company, company),
            company_norm = COALESCE(excluded.company_norm, company_norm),
            verified = excluded.verified, reason = excluded.reason,
            postings = excluded.postings, checked_at = excluded.checked_at
        """,
        (candidate.kind, candidate.token, candidate.company,
         normalize_company(candidate.company) or None, int(verified), reason, postings),
    )


#: Legal forms and filler that differ between a register name and a brand.
_FORMS = re.compile(
    r"\b(?:aktiebolag|ab|gmbh|ag|bv|b v|nv|sa|sas|sarl|ltd|limited|llc|inc|plc|oy|oyj|as|a s|"
    r"aps|spa|srl|sl|group|holding|labs?|hauptsitz|nl|svb)\b")


def _compact(name: str | None) -> str:
    text = re.sub(r"\([^)]*\)", " ", normalize_company(name))
    return re.sub(r"[^a-z0-9]", "", _FORMS.sub(" ", text))


def _same_employer(a: str | None, b: str | None) -> bool:
    """"GREEN 14 AB" ~ "green14", "Silex Microsystems AB" ~ "Silex",
    "Mojang AB" ~ "Mojang Studios". Not "Radar Reticence AB" ~ "Lead"."""
    ca, cb = _compact(a), _compact(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    short, long = sorted((ca, cb), key=len)
    if len(short) >= 4 and long.startswith(short):
        return True
    first_a = normalize_company(a).split()[:1]
    first_b = normalize_company(b).split()[:1]
    return bool(first_a and first_a == first_b and len(first_a[0]) >= 4)


def judge(candidate: Candidate, postings: list[Any]) -> tuple[bool, str]:
    """Is this board the linking employer's?

    First the rule the lists use. A board found here was LINKED from that
    employer's own posting, which is far stronger evidence than a token
    guessed from a name. So name variants also count: a register name against
    a brand ("Silex Microsystems AB" / "silex"), and a token that is the
    first word of the name ("lovable" / Lovable Labs Sweden AB). A board whose
    postings name someone else is still refused.
    """
    if not candidate.company:
        return False, "the linking posting names no employer"
    ok, reason = verify_board(candidate.company, (), candidate.token, postings,
                              alias_tokens=False)
    if ok or not postings:
        return ok, reason
    names = {getattr(p, "company_name", None) for p in postings} - {None, ""}
    if names:
        if any(_same_employer(candidate.company, n) for n in names):
            return True, "the postings name this company (a variant of its name)"
        return False, reason
    token = re.sub(r"[^a-z0-9]", "", candidate.token.lower())
    compact = _compact(candidate.company)
    if len(token) >= 4 and compact and (compact.startswith(token) or token.startswith(compact)):
        return True, "the board is at the start of the company's name"
    return False, reason


def boards_to_poll(conn: sqlite3.Connection, taken: Iterable[tuple[str, str]] = ()
                   ) -> list[dict[str, Any]]:
    """Verified boards due this run: productive ones every run, the rest weekly."""
    skip = {(k, t.lower()) for k, t in taken}
    rows = conn.execute(
        f"""
        SELECT d.*, s.last_polled_at,
               (SELECT COUNT(*) FROM jobs j
                  JOIN job_sources src ON src.id = j.source_id
                  JOIN job_matches m ON m.job_id = j.id
                 WHERE src.name = d.kind || ':' || d.token AND m.passed = 1
                   AND j.first_seen_at > datetime('now', '-{RELEVANT_WINDOW_DAYS} days')
               ) AS relevant
          FROM discovered_boards d
          LEFT JOIN job_sources s ON s.name = d.kind || ':' || d.token
         WHERE d.verified = 1 AND d.active = 1
        """
    ).fetchall()
    out = []
    for row in rows:
        board = dict(row)
        if board["kind"] not in POLLABLE or (board["kind"], board["token"].lower()) in skip:
            continue
        polled = board.get("last_polled_at")
        due = (board["relevant"] or 0) > 0 or not polled or conn.execute(
            f"SELECT ? < datetime('now', '-{RECHECK_IDLE_DAYS} days')", (polled,)
        ).fetchone()[0]
        if due:
            out.append(board)
    return out
