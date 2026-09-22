"""The same vacancy from several sources -> one cluster — §10, §11, §63.

Every posting belongs to a cluster, identified by the id of the first posting
that started it. That id never changes, which matters: dismissals and
applications are keyed on it, so "you dismissed this" survives the same job
arriving tomorrow from a better source.

Levels, from §11:

* **exact** — the same canonical URL. Same job.
* **source** — the same source and source id. Already one row (`UNIQUE`).
* **strong** — same employer, same title words, compatible location, and the
  descriptions agree (or one of them is a snippet). Same job.
* **fuzzy** — same employer and location, titles ≥ 90% alike but not the same
  words. Kept separate, flagged `possible_duplicate_of` (§11: "do not merge
  aggressively when there is uncertainty").

Two postings from the SAME source are never merged: that source gave them two
ids, and it knows its own vacancies better than a string comparison does.

Which member represents the cluster is decided at read time by source rank
(§63): the employer's own ATS beats a job board beats an aggregator. So a
better source arriving later becomes the Apply link without moving any keys.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

#: §63 — lower is more direct. Unknown kinds rank last but one.
SOURCE_RANK = {
    "ats_json": 0,
    "career_page": 1,
    "job_board": 2,
    "aggregator": 3,
    "aggregator_api": 4,
}
_UNKNOWN_RANK = 3

STRONG_DESCRIPTION = 80     # fuzz.ratio on the first 1,500 characters
SNIPPET_PARTIAL = 75        # fuzz.partial_ratio, snippet against full text
FUZZY_TITLE = 90            # token_set_ratio of the normalised titles


def source_rank(kind: str | None) -> int:
    return SOURCE_RANK.get(kind or "", _UNKNOWN_RANK)


@dataclass
class DedupeStats:
    clustered: int = 0
    merged: int = 0
    possible: int = 0


def _countries(row: dict[str, Any]) -> set[str]:
    try:
        return set(json.loads(row.get("countries") or "[]"))
    except (TypeError, json.JSONDecodeError):
        return set()


def _cities(row: dict[str, Any]) -> set[str]:
    try:
        return {c.lower() for c in json.loads(row.get("cities") or "[]")}
    except (TypeError, json.JSONDecodeError):
        return set()


def locations_compatible(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Unknown is compatible with anything; two known places must overlap."""
    ca, cb = _countries(a), _countries(b)
    if ca and cb and not ca & cb:
        return False
    ta, tb = _cities(a), _cities(b)
    return not (ta and tb and not ta & tb)


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).lower()


def descriptions_agree(a: dict[str, Any], b: dict[str, Any]) -> bool:
    da, db = _text(a.get("description")), _text(b.get("description"))
    if not da or not db:
        return True
    if a.get("description_truncated") or b.get("description_truncated"):
        short, long = (da, db) if len(da) <= len(db) else (db, da)
        return fuzz.partial_ratio(short[:400], long) >= SNIPPET_PARTIAL
    return fuzz.ratio(da[:1500], db[:1500]) >= STRONG_DESCRIPTION


def compare(new: dict[str, Any], other: dict[str, Any]) -> str | None:
    """`"exact"`, `"strong"`, `"fuzzy"` or None. Pure — the §89 case tests it."""
    if new.get("source_id") is not None and new.get("source_id") == other.get("source_id"):
        return None
    if new.get("canonical_url") and new.get("canonical_url") == other.get("canonical_url"):
        return "exact"
    if not new.get("company_norm") or new.get("company_norm") != other.get("company_norm"):
        return None
    if not locations_compatible(new, other):
        return None
    if new.get("title_key") and new.get("title_key") == other.get("title_key"):
        return "strong" if descriptions_agree(new, other) else "fuzzy"
    score = fuzz.token_set_ratio(new.get("title_norm") or "", other.get("title_norm") or "")
    return "fuzzy" if score >= FUZZY_TITLE else None


_COLUMNS = ("id, source_id, canonical_url, company_norm, title_key, title_norm, countries, "
            "cities, description, description_truncated, cluster_id")


def assign_clusters(conn: sqlite3.Connection) -> DedupeStats:
    """Give every normalised posting without a cluster its cluster.

    Candidates are drawn only from the same employer (indexed on
    `company_norm`) plus exact-URL matches, so this stays cheap as the table
    grows.
    """
    stats = DedupeStats()
    pending = [dict(r) for r in conn.execute(
        f"SELECT {_COLUMNS} FROM jobs WHERE cluster_id IS NULL AND normalizer_version IS NOT NULL"
        " ORDER BY id"
    )]
    for row in pending:
        candidates = [dict(r) for r in conn.execute(
            f"""
            SELECT {_COLUMNS} FROM jobs
             WHERE id != ? AND cluster_id IS NOT NULL
               AND ((company_norm = ? AND company_norm != '') OR
                    (canonical_url = ? AND canonical_url != ''))
            """,
            (row["id"], row["company_norm"] or "", row["canonical_url"] or ""),
        )]
        cluster: int | None = None
        possible: int | None = None
        for other in candidates:
            verdict = compare(row, other)
            if verdict in {"exact", "strong"}:
                cluster = int(other["cluster_id"])
                break
            if verdict == "fuzzy" and possible is None:
                possible = int(other["cluster_id"])
        if cluster is not None:
            conn.execute("UPDATE jobs SET cluster_id = ? WHERE id = ?", (cluster, row["id"]))
            stats.merged += 1
        else:
            conn.execute(
                "UPDATE jobs SET cluster_id = id, possible_duplicate_of = ? WHERE id = ?",
                (possible, row["id"]),
            )
            stats.possible += int(possible is not None)
        stats.clustered += 1
    return stats


def pick_canonical(members: list[dict[str, Any]]) -> dict[str, Any]:
    """The member that represents a cluster: most direct source, then the one
    with a full description, then the most recently seen (§63, §84 case 5)."""
    return min(
        members,
        key=lambda m: (
            source_rank(m.get("source_kind")),
            int(bool(m.get("description_truncated"))),
            0 if m.get("description") else 1,
            m.get("status") != "open",
            -_ts(m.get("last_seen_at")),
        ),
    )


def _ts(value: Any) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0
