"""Do the score's parts agree with the owner's own judgement?

The weights in `config/job_search.yaml` were set by reasoning, not by data.
This module measures them against what the owner has actually done:

* **good**: 👍 "good match", or an application that went out (APPLIED and
  later; an explicit 👎 still wins)
* **bad**: 👎 "poor match", or a dismissal for a reason about fit ("Too
  senior", "Wrong role", "Poor match", …). "Duplicate", "Closed" and
  "Already applied" say nothing about fit and are left out.

For each component it reports how well it separates the two: the AUC, the
chance that a random good job scores higher on it than a random bad one (0.5
is a coin, 1.0 is perfect). It then fits non-negative weights (logistic
regression, pure Python, lightly regularised) and reports the AUC of the
current weights next to the proposed ones.

It proposes; it never writes. The weights stay whatever the owner saves on
🧭 Job Setup, or with `pitwall job-calibrate --apply`.

**Borrowed ratings.** Ten 👎 are too few to fit eight weights. A second
rater's labels (`--labels`, a CSV of cluster_id,label) can supply the volume,
but then the weights learn that rater's taste. So when labels are given the
weights are fitted on THEM and tested on the owner's own ratings, which the
fit never saw. The owner's ratings are the exam, not the textbook. Where both
rated the same vacancy, the owner's rating wins and it counts once, on the
owner's side.

Only vacancies that pass the hard filters count: weights rank what is shown,
and a job the filter removes is not shown whatever it scores. The proposal is
blended half-and-half with the current weights, because a few dozen examples
can move a weight further than they can justify.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .tracker import SUBMITTED

SUBMITTED_ORDERED = tuple(sorted(SUBMITTED))

#: Dismissal reasons that are a judgement about fit.
NEGATIVE_REASONS = frozenset({"Too senior", "Wrong country", "No sponsorship", "Wrong role",
                              "Salary", "Not interested", "Poor match"})

MIN_EACH = 15          # below this many of either kind, the numbers are noise


@dataclass
class Example:
    cluster_id: int
    good: bool
    components: dict[str, float]
    score: float


@dataclass
class Report:
    good: int = 0                      # the owner's own, passing the filters
    bad: int = 0
    extra_good: int = 0                # borrowed labels, when given
    extra_bad: int = 0
    auc: dict[str, float] = field(default_factory=dict)
    current_auc: float | None = None   # on the owner's ratings
    proposed: dict[str, float] = field(default_factory=dict)
    proposed_auc: float | None = None  # on the owner's ratings
    blended: dict[str, float] = field(default_factory=dict)
    blended_auc: float | None = None   # on the owner's ratings
    extra_current_auc: float | None = None
    extra_blended_auc: float | None = None
    fitted_on: str = "your ratings"
    enough: bool = False

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def examples(conn: sqlite3.Connection) -> list[Example]:
    """One example per judged vacancy, from its canonical posting's match.

    An explicit 👍/👎 always wins. Otherwise an application counts as good only
    once it went out (`SUBMITTED`): SHORTLISTED, CV_READY and IN_PROCESS are
    intentions, not judgements.
    """
    sent = ", ".join("?" * len(SUBMITTED_ORDERED))
    rows = conn.execute(
        f"""
        SELECT r.cluster_id, r.feedback, r.dismissed_at, r.dismissed_reason,
               (SELECT COUNT(*) FROM applications a WHERE a.cluster_id = r.cluster_id
                   AND a.status IN ({sent})) AS applied,
               m.components, m.score
          FROM job_review r
          JOIN job_matches m ON m.job_id = r.cluster_id
         WHERE m.passed = 1
        UNION ALL
        SELECT DISTINCT a.cluster_id, NULL, NULL, NULL, 1, m.components, m.score
          FROM applications a
          JOIN job_matches m ON m.job_id = a.cluster_id
         WHERE a.cluster_id NOT IN (SELECT cluster_id FROM job_review) AND m.passed = 1
           AND a.status IN ({sent})
        """,
        (*SUBMITTED_ORDERED, *SUBMITTED_ORDERED),
    ).fetchall()
    out: dict[int, Example] = {}
    for cid, feedback, dismissed, reason, applied, comps, score in rows:
        if feedback in ("good_match", "bad_match"):
            good = feedback == "good_match"
        elif applied:
            good = True
        elif dismissed and reason in NEGATIVE_REASONS:
            good = False
        else:
            continue
        try:
            parts = {k: float(v.get("value", 0.5)) for k, v in json.loads(comps or "{}").items()}
        except (TypeError, ValueError, AttributeError):
            continue
        out[int(cid)] = Example(int(cid), good, parts, float(score or 0))
    return list(out.values())


def auc(values: list[float], labels: list[bool]) -> float | None:
    """Mann-Whitney AUC: P(value of a good > value of a bad), ties count half."""
    pos = [v for v, g in zip(values, labels, strict=True) if g]
    neg = [v for v, g in zip(values, labels, strict=True) if not g]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def fit_weights(data: list[Example], names: list[str], *, steps: int = 3000,
                rate: float = 0.5, l2: float = 0.01) -> dict[str, float]:
    """Non-negative logistic-regression weights, by projected gradient descent."""
    w = dict.fromkeys(names, 1.0)
    bias = 0.0
    n = len(data) or 1
    for _ in range(steps):
        grad = dict.fromkeys(names, 0.0)
        gbias = 0.0
        for ex in data:
            z = bias + sum(w[k] * ex.components.get(k, 0.5) for k in names)
            p = 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))
            err = p - (1.0 if ex.good else 0.0)
            gbias += err
            for k in names:
                grad[k] += err * ex.components.get(k, 0.5)
        bias -= rate * gbias / n
        for k in names:
            w[k] = max(0.0, w[k] - rate * (grad[k] / n + l2 * w[k]))
    return w


def load_labels(path: str) -> dict[int, bool]:
    """A ratings CSV: `cluster_id,label` with label good / bad (others skipped)."""
    import csv

    out: dict[int, bool] = {}
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            label = (row.get("label") or "").strip().lower()
            if label in ("good", "bad") and (row.get("cluster_id") or "").strip().isdigit():
                out[int(row["cluster_id"])] = label == "good"
    return out


def labelled(conn: sqlite3.Connection, labels: dict[int, bool],
             skip: set[int]) -> list[Example]:
    """Borrowed labels as examples, for vacancies that pass the filters."""
    out: list[Example] = []
    for cid, good in labels.items():
        if cid in skip:
            continue
        row = conn.execute("SELECT components, score FROM job_matches WHERE job_id = ?"
                           " AND passed = 1", (cid,)).fetchone()
        if not row:
            continue
        try:
            parts = {k: float(v.get("value", 0.5)) for k, v in json.loads(row[0] or "{}").items()}
        except (TypeError, ValueError, AttributeError):
            continue
        out.append(Example(cid, good, parts, float(row[1] or 0)))
    return out


def rescore(ex: Example, weights: dict[str, float], current: dict[str, float]) -> float:
    """What this vacancy would score under `weights`.

    The stored score is the weighted average of the parts plus what sits
    outside them (the language and agency penalties). That remainder is kept,
    so the only thing that changes is the weighting.
    """
    def average(w: dict[str, float]) -> float:
        total = sum(w.get(k, 0.0) for k in ex.components) or 1.0
        return sum(w.get(k, 0.0) * v for k, v in ex.components.items()) / total * 100

    return average(weights) + (ex.score - average(current))


def calibrate(conn: sqlite3.Connection, current: dict[str, float],
              labels: dict[int, bool] | None = None) -> Report:
    owner = examples(conn)
    report = Report(good=sum(e.good for e in owner), bad=sum(not e.good for e in owner))
    extra = labelled(conn, labels or {}, {e.cluster_id for e in owner})
    report.extra_good = sum(e.good for e in extra)
    report.extra_bad = sum(not e.good for e in extra)
    fit_set = extra if extra else owner
    report.fitted_on = "the borrowed ratings" if extra else "your ratings"
    report.enough = sum(e.good for e in fit_set) >= MIN_EACH and \
        sum(not e.good for e in fit_set) >= MIN_EACH
    everything = owner + extra
    if not everything:
        return report
    names = sorted(current)
    judged = [e.good for e in everything]
    for name in names:
        report.auc[name] = auc([e.components.get(name, 0.5) for e in everything], judged) or 0.5

    def score_auc(data: list[Example], weights: dict[str, float] | None) -> float | None:
        values = [e.score if weights is None else rescore(e, weights, current) for e in data]
        return auc(values, [e.good for e in data])

    report.current_auc = score_auc(owner, None)
    report.extra_current_auc = score_auc(extra, None) if extra else None
    if not report.enough:
        return report
    raw = fit_weights(fit_set, names)
    total = sum(current.values()) or 100.0
    scale = total / (sum(raw.values()) or 1.0)
    report.proposed = {k: round(v * scale, 1) for k, v in raw.items()}
    report.blended = {k: round((current[k] + report.proposed[k]) / 2, 1) for k in names}
    report.proposed_auc = score_auc(owner, report.proposed)
    report.blended_auc = score_auc(owner, report.blended)
    report.extra_blended_auc = score_auc(extra, report.blended) if extra else None
    return report
