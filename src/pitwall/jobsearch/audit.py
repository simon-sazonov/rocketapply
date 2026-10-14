"""Is the list worth reading? — the job search measured against labels.

Simon asked for a search he can trust without rating jobs himself, so the
labels here are Claude's: each made from the posting against `RUBRIC`, and
frozen before the rules they measure were changed. Three questions, each
answered from a random sample rather than from the top of the list the rules
were tuned on:

* how much of the list breaks a hard requirement — a language he lacks, years
  he does not have, a PhD, a passport (the violation rate);
* how much of it is worth applying to (precision, overall and at the top);
* how many jobs worth applying to the rules threw away (removals).

A sample is drawn once (`sample`), labelled by reading each job's `digest`,
and measured as often as the rules change (`measure`). Because the rules only
ever narrow the list, the sampled jobs still listed after a change are a random
sample of the new list, which is what lets one frozen sample judge every
version of the rules.
"""

from __future__ import annotations

import csv
import json
import random
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .store import JobQuery, search_ids

#: What a label means. Fixed before the first label was written.
RUBRIC = """\
good   eligible, and a role Simon targets: graduate / junior / intern data science,
       machine learning, AI or research engineering, applied science, quant research,
       simulation, reinforcement learning, optimisation, forecasting or risk — the kind
       of work in config/my_profile.md.
maybe  eligible, but a stretch: an adjacent role (analytics, data engineering, ML-heavy
       software), 2 years asked, experience asked without a number at a company he
       rated well, or a partial domain mismatch.
bad    a hard blocker, or not his kind of job: customer-facing, pure software or
       infrastructure with no modelling, computer vision / 3D / graphics as the
       specialism, an unrelated field, a gig or task platform, a paid training scheme.

eligible  yes      nothing in the text read rules him out
          no       a blocker is quoted: a language he lacks (below B1), more than 2
                   years required, senior level, a PhD required, citizenship or
                   clearance, a stated refusal to sponsor where he needs it
          unknown  only a preview was read, and it cannot say
"""

LABEL_VALUES = ("good", "maybe", "bad")
ELIGIBLE_VALUES = ("yes", "no", "unknown")
#: Why a job is bad or ineligible. One code per label, the most decisive.
REASON_CODES = ("language", "years", "senior", "phd", "citizenship", "visa", "gig",
                "training", "agency", "wrong_role", "domain", "avoid_topic", "location",
                "other")

#: The strata a sample is drawn from. A preview is all an aggregator gives:
#: the first ~500 characters, before any requirement is stated.
STRATA = ("full", "preview-level", "preview-other")

#: What RocketApply's Search lists when nothing is touched.
DEFAULT_VIEW = JobQuery()

PREVIEW_CHARS = 600

_SAMPLE_FIELDS = ("cluster_id", "job_id", "set", "stratum", "score", "title", "company")
LABEL_FIELDS = ("cluster_id", "job_id", "set", "stratum", "label", "eligible", "reason",
                "evidence", "note", "text_seen", "title", "company")


@dataclass(frozen=True)
class Label:
    cluster_id: int
    job_id: int | None
    set: str
    stratum: str
    label: str
    eligible: str
    reason: str = ""
    evidence: str = ""
    note: str = ""
    text_seen: str = ""
    title: str = ""
    company: str = ""

    @property
    def worth_it(self) -> bool:
        """Good or maybe: a job Simon could reasonably apply to."""
        return self.label in ("good", "maybe")

    @property
    def violation(self) -> bool:
        return self.eligible == "no"


# ────────────────────────────── the labels ───────────────────────────────


def load_labels(paths: list[Path | str]) -> dict[int, Label]:
    """Labels by cluster, later files winning. A missing file is no labels."""
    out: dict[int, Label] = {}
    for path in paths:
        path = Path(path)
        if not path.exists():
            continue
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                label = (row.get("label") or "").strip().lower()
                if label not in LABEL_VALUES:
                    continue
                try:
                    cid = int(row["cluster_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                eligible = (row.get("eligible") or "").strip().lower()
                job_id = (row.get("job_id") or "").strip()
                out[cid] = Label(
                    cluster_id=cid, job_id=int(job_id) if job_id.isdigit() else None,
                    set=(row.get("set") or "").strip(), stratum=(row.get("stratum") or "").strip(),
                    label=label,
                    eligible=eligible if eligible in ELIGIBLE_VALUES else "unknown",
                    reason=(row.get("reason") or "").strip(),
                    evidence=(row.get("evidence") or "").strip(),
                    note=(row.get("note") or "").strip(),
                    text_seen=(row.get("text_seen") or "").strip(),
                    title=(row.get("title") or "").strip(),
                    company=(row.get("company") or "").strip(),
                )
    return out


def write_labels(path: Path | str, labels: list[Label]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LABEL_FIELDS)
        writer.writeheader()
        for label in labels:
            writer.writerow({k: v for k, v in asdict(label).items() if k in LABEL_FIELDS})
    return path


# ───────────────────────────── what is listed ────────────────────────────


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _preview_sql(conn: sqlite3.Connection) -> str:
    """SQL true for a posting of which only a preview was collected."""
    if _has_column(conn, "jobs", "text_depth"):
        return "COALESCE(j.text_depth, 'full') = 'preview'"
    return (f"(COALESCE(j.description_truncated, 0) = 1 OR"
            f" length(COALESCE(j.description, '')) < {PREVIEW_CHARS})")


def _stratum_sql(conn: sqlite3.Connection) -> str:
    preview = _preview_sql(conn)
    return (f"CASE WHEN NOT {preview} THEN 'full'"
            " WHEN COALESCE(j.experience_level, 'unknown') IN ('entry', 'intern')"
            " THEN 'preview-level' ELSE 'preview-other' END")


def listed(conn: sqlite3.Connection, q: JobQuery | None = None) -> list[tuple[int, int]]:
    """`(cluster_id, job_id)` for every vacancy a view lists, best first — the
    job being the posting that represents it there."""
    ids = search_ids(conn, q or DEFAULT_VIEW, limit=10**7)
    if not ids:
        return []
    cluster = dict(conn.execute(
        "SELECT id, cluster_id FROM jobs WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(ids),)).fetchall())
    return [(int(cluster[i]), int(i)) for i in ids if cluster.get(i) is not None]


def strata(conn: sqlite3.Connection, job_ids: list[int]) -> dict[int, str]:
    """`{cluster_id: stratum}`, read from the given postings."""
    if not job_ids:
        return {}
    return {int(cid): stratum for cid, stratum in conn.execute(
        f"SELECT j.cluster_id, {_stratum_sql(conn)} FROM jobs j"
        " WHERE j.id IN (SELECT value FROM json_each(?))", (json.dumps(job_ids),))}


def _representatives(conn: sqlite3.Connection, cluster_ids: list[int]) -> list[int]:
    """The posting that stands for each vacancy off the list: the one whose id
    names the cluster, as `ai_fit` reads it."""
    return [int(r[0]) for r in conn.execute(
        "SELECT id FROM jobs WHERE id IN (SELECT value FROM json_each(?)) AND id = cluster_id",
        (json.dumps(cluster_ids),))]


# ─────────────────────────────── sampling ────────────────────────────────


def sample(conn: sqlite3.Connection, *, seed: int = 29, per_stratum: dict[str, int] | None
           = None, filtered: int = 60, top: int = 150) -> list[dict[str, Any]]:
    """The frozen sample: rows for `_SAMPLE_FIELDS`. Pure given the database.

    * `test-listed` — a random sample of the list, stratified so the few full
      postings are not drowned by previews;
    * `test-filtered` — a random sample of jobs the rules rejected only for
      what the posting's text says (years, visa, degree, job type) — not for
      its title, level word or place, which are rarely misread — the
      rejections most likely to be wrong;
    * `dev` — the top of today's list, where problems are found.
    """
    per_stratum = per_stratum or {"full": 75, "preview-level": 40, "preview-other": 35}
    rng = random.Random(seed)
    pairs = listed(conn)
    by_stratum: dict[str, list[int]] = {s: [] for s in STRATA}
    for cid, stratum in strata(conn, [jid for _, jid in pairs]).items():
        by_stratum.setdefault(stratum, []).append(cid)
    chosen: dict[int, tuple[str, str]] = {}
    for stratum in STRATA:
        pool = sorted(by_stratum.get(stratum, []))
        for cid in rng.sample(pool, min(per_stratum.get(stratum, 0), len(pool))):
            chosen[cid] = ("test-listed", stratum)

    content = ("experience", "visa", "education", "employment")
    rejected = [int(r[0]) for r in conn.execute(
        """
        SELECT j.cluster_id FROM jobs j JOIN job_matches m ON m.job_id = j.id
         WHERE j.status = 'open' AND m.passed = 0 AND j.id = j.cluster_id
           AND json_array_length(m.filter_reasons) > 0
           AND NOT EXISTS (SELECT 1 FROM json_each(m.filter_reasons) f
                           WHERE json_extract(f.value, '$.code')
                                 NOT IN (SELECT value FROM json_each(?)))
         ORDER BY j.cluster_id
        """, (json.dumps(content),)).fetchall()]
    stratum_of = strata(conn, _representatives(conn, rejected))
    for cid in rng.sample(rejected, min(filtered, len(rejected))):
        chosen.setdefault(cid, ("test-filtered", stratum_of.get(cid, "full")))

    head = [(cid, jid) for cid, jid in pairs if cid not in chosen][:top]
    stratum_of = strata(conn, [jid for _, jid in head])
    for cid, _ in head:
        chosen[cid] = ("dev", stratum_of.get(cid, "full"))

    # The posting to read is the one Search shows for a listed vacancy (a
    # company's own board over an aggregator's preview), else the cluster's own.
    shown = dict(pairs)
    rows = []
    for cid, (set_, stratum) in chosen.items():
        job = conn.execute(
            """
            SELECT j.id, j.title, COALESCE(NULLIF(j.posting_company, ''), '') AS company,
                   m.score
              FROM jobs j LEFT JOIN job_matches m ON m.job_id = j.id
             WHERE j.cluster_id = ?
             ORDER BY CASE WHEN j.id = ? THEN 0 WHEN j.id = j.cluster_id THEN 1 ELSE 2 END
             LIMIT 1
            """, (cid, shown.get(cid, cid))).fetchone()
        if job is None:
            continue
        rows.append({"cluster_id": cid, "job_id": int(job[0]), "set": set_, "stratum": stratum,
                     "score": round(float(job[3] or 0), 1), "title": job[1] or "",
                     "company": job[2] or ""})
    rows.sort(key=lambda r: (r["set"], r["stratum"], -r["score"]))
    return rows


def write_sample(path: Path | str, rows: list[dict[str, Any]], *, force: bool = False) -> Path:
    """A sample is written once: re-drawing it after seeing results would let
    the measure follow the rules instead of judging them."""
    path = Path(path)
    if path.exists() and not force:
        raise FileExistsError(f"{path} exists — a frozen sample is not redrawn")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_SAMPLE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


# ─────────────────────────────── digests ─────────────────────────────────

#: A line worth reading when judging eligibility: years, degrees, languages,
#: passports, and the words requirements are written in.
_REQUIREMENT = re.compile(
    r"\byears?\b|\byrs?\b|jahre|\bans\b|años|anni|experience|erfahrung|expérience|"
    r"\bdegree|bachelor|master|\bm\.?sc|\bph\.?\s?d|doctor|graduat|student|enrol|"
    r"english|german|deutsch|french|français|dutch|italian|spanish|portuguese|swedish|"
    r"danish|finnish|norwegian|polish|fluen|native|speak|language|"
    r"citizen|clearance|security|visa|sponsor|work permit|right to work|authori[sz]|"
    r"must|required|requirement|essential|mandatory|minimum|qualification|"
    r"you have|you bring|you are|we're looking|we are looking|looking for|ideal candidate|"
    r"intern\b|internship|junior|senior|entry[- ]level|new grad",
    re.I)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def digest(conn: sqlite3.Connection, job_id: int, *, budget: int = 1100) -> str:
    """One posting, compressed for judging: what it is, where, what was
    extracted, and only the lines that state a requirement."""
    cur = conn.execute(
        """
        SELECT j.*, s.name AS source_name, m.score, m.passed, m.filter_reasons
          FROM jobs j LEFT JOIN job_sources s ON s.id = j.source_id
          LEFT JOIN job_matches m ON m.job_id = j.id WHERE j.id = ?
        """, (job_id,))
    r = cur.fetchone()
    if r is None:
        return f"#{job_id}: not in the database"
    row = dict(zip([c[0] for c in cur.description], tuple(r), strict=True))
    text = row.get("description") or ""
    preview = bool(row.get("description_truncated")) or len(text) < PREVIEW_CHARS
    reasons = "; ".join(f.get("code", "") + ": " + f.get("message", "")
                        for f in json.loads(row.get("filter_reasons") or "[]"))
    head = [
        f"#{row['cluster_id']} (job {row['id']}) {row.get('title') or ''} — "
        f"{row.get('posting_company') or '?'} — {row.get('location') or '?'} — "
        f"{row.get('source_name') or '?'} — {'PREVIEW' if preview else 'full'} "
        f"{len(text)} chars — score {float(row.get('score') or 0):.0f} "
        f"{'passed' if row.get('passed') else 'FILTERED: ' + reasons}",
        f"  extracted: level={row.get('experience_level')} years={row.get('experience_min')}"
        f"{'(req)' if row.get('experience_required') else '(pref)' if row.get('experience_min') is not None else ''}"
        f" lang={row.get('language')} asks={row.get('languages_required')}"
        f" edu={row.get('education_required')}/{row.get('education_preferred')}"
        f" visa={row.get('visa_status')} roles={row.get('role_labels')}"
        f" agency={row.get('agency')}",
    ]
    if preview:
        return "\n".join(head + ["  text: " + _clip(text, 700)])
    lines = [ln.strip() for ln in re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z•\-])", text) if ln.strip()]
    intro = _clip(" ".join(lines[:3]), 260)
    kept: list[str] = []
    used = 0
    for line in lines[3:]:
        if not _REQUIREMENT.search(line):
            continue
        line = _clip(line, 240)
        if used + len(line) > budget:
            break
        kept.append(line)
        used += len(line)
    return "\n".join(head + ["  about: " + intro] + [f"  · {ln}" for ln in kept])


# ─────────────────────────────── measuring ───────────────────────────────


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def _feedback(conn: sqlite3.Connection) -> dict[int, str]:
    return {int(cid): fb for cid, fb in conn.execute(
        "SELECT cluster_id, feedback FROM job_review WHERE feedback IS NOT NULL")}


def _why_not_listed(conn: sqlite3.Connection, cluster_id: int) -> str:
    """Why a vacancy is not in the list: a rule's code, a dismissal, or the view."""
    row = conn.execute(
        """
        SELECT m.passed, m.filter_reasons, j.status, r.dismissed_at
          FROM jobs j JOIN job_matches m ON m.job_id = j.id
          LEFT JOIN job_review r ON r.cluster_id = j.cluster_id
         WHERE j.cluster_id = ? ORDER BY CASE WHEN j.id = j.cluster_id THEN 0 ELSE 1 END
         LIMIT 1
        """, (cluster_id,)).fetchone()
    if row is None:
        return "gone"
    if row[3]:
        return "dismissed"
    if row[2] != "open":
        return "closed"
    if not row[0]:
        codes = [f.get("code", "?") for f in json.loads(row[1] or "[]")]
        return "+".join(sorted(set(codes))) or "filtered"
    return "hidden by the view"


def measure(conn: sqlite3.Connection, labels: dict[int, Label], *,
            q: JobQuery | None = None) -> dict[str, Any]:
    """The list against the labels. See the module docstring for what each
    number means; every rate comes with the counts behind it."""
    q = q or DEFAULT_VIEW
    pairs = listed(conn, q)
    order = [cid for cid, _ in pairs]
    in_list = set(order)
    sizes: dict[str, int] = {}
    for stratum in strata(conn, [jid for _, jid in pairs]).values():
        sizes[stratum] = sizes.get(stratum, 0) + 1
    passing = int(conn.execute(
        "SELECT COUNT(*) FROM job_matches m JOIN jobs j ON j.id = m.job_id"
        " WHERE j.status = 'open' AND m.passed = 1 AND j.id = j.cluster_id").fetchone()[0])

    # ── the random sample of the list ──
    listed_labels = [lb for lb in labels.values() if lb.set == "test-listed"]
    per_stratum: dict[str, dict[str, Any]] = {}
    for stratum in STRATA:
        mine = [lb for lb in listed_labels if lb.stratum == stratum]
        kept = [lb for lb in mine if lb.cluster_id in in_list]
        known = [lb for lb in kept if lb.eligible != "unknown"]
        per_stratum[stratum] = {
            "labelled": len(mine), "still_listed": len(kept),
            "violations": sum(lb.violation for lb in known), "eligibility_known": len(known),
            "worth_it": sum(lb.worth_it for lb in kept),
            "unknown": len(kept) - len(known),
        }

    def weighted(key: str, denom: str = "still_listed") -> float | None:
        """A stratum's rate, weighted by how much of the list it is now. A
        preview can hide a blocker, so "unknown" is its own rate rather than
        being dropped: dropping it would leave only the blockers a preview
        happened to show, and call the list worse than it is."""
        total = sum(sizes.get(s, 0) for s in STRATA if per_stratum[s][denom])
        if not total:
            return None
        return round(sum(sizes.get(s, 0) / total * per_stratum[s][key] / per_stratum[s][denom]
                         for s in STRATA if per_stratum[s][denom]), 3)

    removed = [lb for lb in listed_labels if lb.cluster_id not in in_list]
    removed_why: dict[str, dict[str, int]] = {}
    for lb in removed:
        why = _why_not_listed(conn, lb.cluster_id)
        bucket = removed_why.setdefault(why, {"worth_it": 0, "not": 0})
        bucket["worth_it" if lb.worth_it else "not"] += 1
    worth = [lb for lb in listed_labels if lb.worth_it]
    unworthy = [lb for lb in listed_labels if not lb.worth_it]

    # ── the jobs the rules rejected ──
    rejected_labels = [lb for lb in labels.values() if lb.set == "test-filtered"]
    rejected_worth = [lb for lb in rejected_labels if lb.worth_it]

    # ── the top of the list ──
    top: dict[str, Any] = {}
    for n in (25, 50, 100, 200):
        head = [labels[c] for c in order[:n] if c in labels]
        top[str(n)] = {"labelled": len(head), "of": min(n, len(order)),
                       "worth_it": sum(lb.worth_it for lb in head),
                       "good": sum(lb.label == "good" for lb in head),
                       "violations": sum(lb.violation for lb in head),
                       "precision": _rate(sum(lb.worth_it for lb in head), len(head))}

    # ── Simon's own ratings ──
    fb = _feedback(conn)
    liked = [c for c, v in fb.items() if v == "good_match"]
    disliked = [c for c, v in fb.items() if v == "bad_match"]
    lost = {c: _why_not_listed(conn, c) for c in liked if c not in in_list}

    return {
        "date": date.today().isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "view": {k: v for k, v in asdict(q).items() if v not in ((), None, "", False)},
        "listed": len(order),
        "listed_by_stratum": sizes,
        "passing_rules": passing,
        "labels": len(labels),
        "sample": {
            "per_stratum": per_stratum,
            "violation_rate": weighted("violations"),
            "unknown_rate": weighted("unknown"),
            "precision": weighted("worth_it"),
            "removed": len(removed),
            "removed_worth_it": sum(lb.worth_it for lb in removed),
            "worth_it_removed_rate": _rate(sum(lb.worth_it for lb in removed), len(worth)),
            "bad_removed_rate": _rate(sum(not lb.worth_it for lb in removed), len(unworthy)),
            "removed_why": removed_why,
        },
        "rejected_sample": {
            "labelled": len(rejected_labels),
            "worth_it": len(rejected_worth),
            "wrongly_rejected_rate": _rate(len(rejected_worth), len(rejected_labels)),
            "worth_it_now_listed": sum(lb.cluster_id in in_list for lb in rejected_worth),
        },
        "top": top,
        "simon": {
            "liked": len(liked), "liked_listed": len(liked) - len(lost),
            "liked_not_listed": {str(c): why for c, why in lost.items()},
            "disliked": len(disliked),
            "disliked_listed": sum(c in in_list for c in disliked),
        },
    }


def save_report(report: dict[str, Any], directory: Path | str, *, name: str | None = None
                ) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name or f"audit-{report['date']}.json")
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def latest_report(directory: Path | str) -> dict[str, Any] | None:
    """The newest saved report, for RocketApply's How it works page."""
    best: dict[str, Any] | None = None
    for path in Path(directory).glob("audit-*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or "listed" not in data:
            continue
        data["_file"] = path.name
        if best is None or str(data.get("generated_at", "")) > str(best.get("generated_at", "")):
            best = data
    return best
