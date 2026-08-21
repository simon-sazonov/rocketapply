"""Candidate deduplication — §8.3 of PITWALL_PLAN.md.

1. **Slug is truth.** `linkedin.com/in/jane-doe-1a2b3c` -> `jane-doe-1a2b3c`.
   Lowercased, trailing slash stripped, URL-decoded. Upsert on it.
2. **Merge policy** on a repeat sighting: keep the longest non-empty value per
   field, bump `last_seen_at`, increment `evidence_count`, append the new
   `candidate_sources` link.
3. **Fuzzy safety net**: same name and same normalized company but a different
   slug is *flagged* for manual merge, never merged automatically — people
   genuinely do share names. The lower threshold auto-merges only records that
   arrived without a slug at all.
4. **Blocklist check** before every insert.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from rapidfuzz import fuzz

from .normalize import extract_linkedin_slug, normalize_company, normalize_name

#: Auto-merge threshold for slug-less records (Phase 3 of the plan).
FUZZY_MERGE_THRESHOLD = 92
#: Manual-review flag threshold for records that *do* have distinct slugs.
FUZZY_FLAG_THRESHOLD = 95

#: Fields merged by "keep the longest non-empty value".
LONGEST_WINS = ("full_name", "headline", "current_title", "current_company", "location")


def slug_key(url_or_slug: str) -> str | None:
    """Canonical dedup key from a profile URL or a raw slug."""
    if not url_or_slug:
        return None
    if "linkedin.com" in url_or_slug.lower():
        return extract_linkedin_slug(url_or_slug)
    return url_or_slug.strip().strip("/").lower() or None


def merge_fields(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Merge a fresh sighting into a stored candidate.

    Longest non-empty value wins per field, which favours the most informative
    SERP row without letting a truncated snippet overwrite a full headline.
    """
    merged = dict(existing)
    for field in LONGEST_WINS:
        old = (existing.get(field) or "").strip() if isinstance(existing.get(field), str) else existing.get(field)
        cand = (new.get(field) or "").strip() if isinstance(new.get(field), str) else new.get(field)
        if not cand:
            continue
        if not old or len(str(cand)) > len(str(old)):
            merged[field] = cand
    if new.get("profile_url"):
        merged["profile_url"] = new["profile_url"]
    merged["linkedin_slug"] = existing.get("linkedin_slug") or new.get("linkedin_slug")
    return merged


def same_person(a: dict[str, Any], b: dict[str, Any], *, threshold: int = FUZZY_FLAG_THRESHOLD) -> bool:
    """Fuzzy identity test: near-identical name **and** the same employer."""
    name_a, name_b = normalize_name(a.get("full_name") or ""), normalize_name(b.get("full_name") or "")
    if not name_a or not name_b:
        return False
    if fuzz.token_sort_ratio(name_a.lower(), name_b.lower()) < threshold:
        return False
    comp_a = normalize_company(a.get("current_company") or "")
    comp_b = normalize_company(b.get("current_company") or "")
    return bool(comp_a) and comp_a == comp_b


def find_possible_duplicates(
    candidates: list[dict[str, Any]], *, threshold: int = FUZZY_FLAG_THRESHOLD
) -> list[tuple[dict[str, Any], dict[str, Any], float]]:
    """Pairs of distinct-slug candidates that look like the same person.

    Bucketed by normalized company so this stays O(n) in practice rather than
    O(n²) over the whole table.
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for c in candidates:
        key = normalize_company(c.get("current_company") or "")
        if not key:
            continue
        buckets.setdefault(key, []).append(c)

    pairs: list[tuple[dict[str, Any], dict[str, Any], float]] = []
    for group in buckets.values():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if a.get("linkedin_slug") == b.get("linkedin_slug"):
                    continue
                na = normalize_name(a.get("full_name") or "").lower()
                nb = normalize_name(b.get("full_name") or "").lower()
                if not na or not nb:
                    continue
                ratio = fuzz.token_sort_ratio(na, nb)
                if ratio >= threshold:
                    pairs.append((a, b, float(ratio)))
    return sorted(pairs, key=lambda p: -p[2])


def match_slugless(
    record: dict[str, Any], pool: list[dict[str, Any]], *, threshold: int = FUZZY_MERGE_THRESHOLD
) -> dict[str, Any] | None:
    """Find an existing candidate a slug-less record should merge into.

    Only used for non-LinkedIn sources (Phase 10); LinkedIn rows always have a
    slug and go through the exact-key path.
    """
    if record.get("linkedin_slug"):
        return None
    best: dict[str, Any] | None = None
    best_ratio = 0.0
    for other in pool:
        if same_person(record, other, threshold=threshold):
            ratio = fuzz.token_sort_ratio(
                normalize_name(record.get("full_name") or "").lower(),
                normalize_name(other.get("full_name") or "").lower(),
            )
            if ratio > best_ratio:
                best, best_ratio = other, ratio
    return best


def merge_candidates(conn: sqlite3.Connection, keep_id: int, drop_id: int) -> None:
    """Manual merge from the UI: move evidence and CRM state, delete the loser."""
    if keep_id == drop_id:
        return
    keep = conn.execute("SELECT * FROM candidates WHERE id = ?", (keep_id,)).fetchone()
    drop = conn.execute("SELECT * FROM candidates WHERE id = ?", (drop_id,)).fetchone()
    if not keep or not drop:
        return

    merged = merge_fields(dict(keep), dict(drop))
    conn.execute(
        """
        UPDATE candidates
           SET full_name = ?, headline = ?, current_title = ?, current_company = ?,
               location = ?, last_seen_at = datetime('now')
         WHERE id = ?
        """,
        (
            merged.get("full_name"),
            merged.get("headline"),
            merged.get("current_title"),
            merged.get("current_company"),
            merged.get("location"),
            keep_id,
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO candidate_sources (candidate_id, search_result_id)
        SELECT ?, search_result_id FROM candidate_sources WHERE candidate_id = ?
        """,
        (keep_id, drop_id),
    )
    # keep the more advanced CRM state and concatenate notes
    keep_crm = conn.execute("SELECT * FROM crm WHERE candidate_id = ?", (keep_id,)).fetchone()
    drop_crm = conn.execute("SELECT * FROM crm WHERE candidate_id = ?", (drop_id,)).fetchone()
    if drop_crm:
        note = "\n".join(x for x in [(keep_crm or {})["note"] if keep_crm else None, drop_crm["note"]] if x)
        status = _more_advanced(
            (keep_crm["status"] if keep_crm else "new"), drop_crm["status"]
        )
        conn.execute("INSERT OR IGNORE INTO crm (candidate_id) VALUES (?)", (keep_id,))
        conn.execute(
            "UPDATE crm SET status = ?, note = ?, updated_at = datetime('now') WHERE candidate_id = ?",
            (status, note or None, keep_id),
        )
    conn.execute("DELETE FROM candidates WHERE id = ?", (drop_id,))

    n = conn.execute(
        """
        SELECT COUNT(DISTINCT sr.query_id) n FROM candidate_sources cs
          JOIN search_results sr ON sr.id = cs.search_result_id
         WHERE cs.candidate_id = ?
        """,
        (keep_id,),
    ).fetchone()["n"]
    conn.execute("UPDATE candidates SET evidence_count = ? WHERE id = ?", (n, keep_id))


_STATUS_RANK = {
    "ignored": -1,
    "new": 0,
    "saved": 1,
    "contacted": 2,
    "replied": 3,
    "meeting": 4,
    "closed": 5,
}


def _more_advanced(a: str, b: str) -> str:
    return a if _STATUS_RANK.get(a, 0) >= _STATUS_RANK.get(b, 0) else b
