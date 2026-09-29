"""The searches every keyword source runs, derived from the profile.

One list, built from what the profile already says: the preferred titles, the
accepted role labels, and any extra searches the owner adds. It is never
maintained by hand in a second place. A hand-kept list per source is how
Adzuna's eight searches drifted from the seventeen titles on the Job Setup
page.

Two rules keep the list short, and so keep a quota-limited source's budget on
searches that find different jobs:

* **Seniority words go.** "Junior Data Scientist" and "Graduate Data
  Scientist" are one search, "data scientist". The seniority filter decides
  the level afterwards, from the posting.
* **A search containing a shorter one is dropped.** Title searches match every
  word, so "machine learning" already finds every "machine learning engineer"
  and "applied machine learning engineer".

Searches for labels the owner ranks lower (below `SECONDARY_BELOW`) come last,
so a budget that runs out spends it on the primary ones first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .settings import JobSearchConfig

#: Words that say how senior a job is, not what it is.
SENIORITY_WORDS = frozenset({
    "junior", "jr", "graduate", "grad", "entry", "level", "senior", "sr", "lead",
    "principal", "staff", "intern", "internship", "trainee", "early", "career",
    "new", "working", "student", "werkstudent", "i", "ii", "iii",
})

#: What a role label is called when someone searches for it. Two spellings of
#: optimisation, because a title search does not stem.
LABEL_QUERIES: dict[str, tuple[str, ...]] = {
    "MACHINE_LEARNING": ("machine learning", "ml engineer"),
    "AI": ("ai engineer",),
    "DATA_SCIENCE": ("data scientist",),
    "APPLIED_SCIENCE": ("applied scientist",),
    "DECISION_SCIENCE": ("decision scientist",),
    "OPTIMIZATION": ("optimization", "optimisation", "operations research"),
    "SIMULATION": ("simulation engineer",),
    "DATA_ENGINEERING": ("data engineer",),
    "SOFTWARE_ENGINEERING": ("software engineer",),
    "ANALYTICS": ("data analyst",),
    "BI": ("business intelligence",),
}

#: Labels ranked below this in `roles.preference` are searched after the rest.
SECONDARY_BELOW = 0.8


@dataclass(frozen=True)
class Query:
    text: str
    primary: bool


def _words(text: str) -> list[str]:
    text = re.sub(r"\([^)]*\)", " ", text.lower())      # "(m/w/d)", "(Remote)"
    return re.findall(r"[a-z0-9+#]+", text.replace("&", " "))


def core(title: str) -> str:
    """"Graduate Machine Learning Engineer" -> "machine learning engineer"."""
    return " ".join(w for w in _words(title) if w not in SENIORITY_WORDS)


def build_queries(cfg: JobSearchConfig, extra: list[str] | None = None) -> list[Query]:
    """The profile's searches, primary first, with nothing redundant."""
    prefs = cfg.roles.preference
    candidates: list[Query] = []
    for label in cfg.roles.accepted:
        primary = prefs.get(label, 1.0) >= SECONDARY_BELOW
        candidates += [Query(q, primary) for q in LABEL_QUERIES.get(label, ())]
    candidates += [Query(core(t), True) for t in cfg.roles.preferred_titles]
    candidates += [Query(core(t), True) for t in (extra or [])]

    # Primary wins when the same text is both.
    merged: dict[str, bool] = {}
    for q in candidates:
        if q.text:
            merged[q.text] = merged.get(q.text, False) or q.primary

    words = {text: set(text.split()) for text in merged}

    def redundant(text: str) -> bool:
        return any(other != text and words[other] < words[text]
                   and (merged[other] or not merged[text])
                   for other in merged)

    kept = [Query(text, primary) for text, primary in merged.items() if not redundant(text)]
    return sorted(kept, key=lambda q: not q.primary)


def query_texts(cfg: JobSearchConfig, extra: list[str] | None = None,
                *, primary_only: bool = False) -> list[str]:
    return [q.text for q in build_queries(cfg, extra) if q.primary or not primary_only]
