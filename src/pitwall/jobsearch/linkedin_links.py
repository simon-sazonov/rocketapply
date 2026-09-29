"""LinkedIn people searches as plain links — the free, instant way to find someone.

Clicking one opens LinkedIn's own people search in the owner's browser, signed in
as themselves. Nothing is fetched or scraped from here; the link is just a URL.
The groups match the employer people search (`people.py`), so a panel can offer
"search it yourself" and "let PitWall search" side by side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from .people import GROUP_LABELS, alumni_roles, short_title

__all__ = ["LinkPreset", "people_search", "presets"]

BASE = "https://www.linkedin.com/search/results/people/"


@dataclass(frozen=True)
class LinkPreset:
    group: str
    label: str
    url: str


def people_search(keywords: str) -> str:
    """LinkedIn's people search for these words."""
    return f"{BASE}?{urlencode({'keywords': ' '.join(keywords.split()), 'origin': 'GLOBAL_SEARCH_HEADER'})}"


def presets(company: str, job_title: str = "", *, profile: Any = None) -> list[LinkPreset]:
    """The searches worth one click each, grouped like the people search."""
    company = (company or "").strip()
    if not company:
        return []
    role = short_title(job_title) if job_title else ""
    rows = [
        ("recruiter", f"{company} recruiter"),
        ("recruiter", f"{company} talent acquisition"),
    ]
    if role:
        rows += [
            ("hiring", f"{company} {role} manager"),
            ("hiring", f"{company} head of {role}"),
            ("peer", f"{company} {role}"),
        ]
    else:
        rows.append(("hiring", f"{company} hiring manager"))
    rows += [("alumni", f"{company} {r.title}") for r in alumni_roles(profile)]
    return [LinkPreset(group, keywords, people_search(keywords)) for group, keywords in rows]


def group_label(group: str) -> str:
    return GROUP_LABELS.get(group, group)
