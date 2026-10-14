"""Words and numbers as the screens show them."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pitwall.jobsearch.locations import country_name, is_country_code

ROLE_NAMES = {
    "MACHINE_LEARNING": "Machine learning", "AI": "AI", "DATA_SCIENCE": "Data science",
    "APPLIED_SCIENCE": "Applied science", "DECISION_SCIENCE": "Decision science",
    "OPTIMIZATION": "Optimisation", "SIMULATION": "Simulation",
    "DATA_ENGINEERING": "Data engineering", "SOFTWARE_ENGINEERING": "Software engineering",
    "ANALYTICS": "Analytics", "BI": "Business intelligence", "OTHER": "Other",
}
EXPERIENCE_NAMES = {"intern": "Internship", "entry": "Entry level", "mid": "Mid level",
                    "senior": "Senior", "unknown": "Not stated"}
MODE_NAMES = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site",
              "unknown": "Not stated"}
VISA_NAMES = {"ok": "No sponsorship needed", "explicit": "Sponsorship offered",
              "likely": "Sponsorship likely", "unknown": "Sponsorship unclear",
              "no": "No sponsorship"}
LABEL_NAMES = {"strong": "Strong fit", "good": "Good fit", "fair": "Fair fit", "weak": "Weak fit"}
STATUS_NAMES = {
    "SHORTLISTED": "Selected", "CV_READY": "In review", "IN_PROCESS": "In process",
    "APPLIED": "Applied", "ONLINE_ASSESSMENT": "Online assessment", "INTERVIEW": "Interview",
    "FINAL_INTERVIEW": "Final interview", "OFFER": "Offer", "REJECTED": "Rejected",
    "WITHDRAWN": "Withdrawn", "CLOSED": "Closed", "SKIPPED": "Removed",
}
STATE_NAMES = {"clean": "Ready to send", "review": "Needs a look", "unsent": "Do not send",
               "": "CV only"}
COMPONENT_NAMES = {
    "role": "Role", "experience": "Experience", "skills": "Skills", "location": "Location",
    "visa": "Visa", "career": "Direction", "company": "Company", "fit": "CV match",
}


def country(code: str) -> str:
    return country_name(code) if code else ""


def where(job: dict[str, Any]) -> str:
    cities = job.get("cities") or []
    countries = [country_name(c) for c in job.get("countries") or []]
    place = ", ".join(cities[:2]) or ", ".join(countries[:2]) or (job.get("location") or "—")
    if cities and len(countries) == 1:
        place += f", {countries[0]}"
    mode = job.get("remote_status")
    if mode == "remote":
        regions = job.get("remote_regions") or []
        region = ", ".join(country_name(r) if is_country_code(r) else r for r in regions)
        place = f"Remote · {region}" if region and not cities else f"{place} · remote"
    elif mode == "hybrid":
        place += " · hybrid"
    return place


def age(stamp: Any) -> str:
    try:
        when = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - when
    days = delta.days
    if days <= 0:
        hours = int(delta.total_seconds() // 3600)
        return "just now" if hours <= 0 else f"{hours}h ago"
    return f"{days}d ago" if days < 60 else f"{days // 30}mo ago"


def salary(job: dict[str, Any]) -> str:
    lo, hi, cur = job.get("salary_min"), job.get("salary_max"), job.get("salary_currency") or ""
    if lo is None and hi is None:
        return ""

    def k(v: Any) -> str:
        v = float(v or 0)
        return f"{v / 1000:.0f}k" if v >= 1000 else f"{v:.0f}"

    span = f"{k(lo)}–{k(hi)}" if lo and hi and lo != hi else k(lo or hi)
    return f"{cur} {span}".strip()


def tokens(n: int | float | None) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def seconds(s: float | None) -> str:
    s = float(s or 0)
    return f"{s:.0f}s" if s < 90 else f"{s / 60:.1f} min"


def reasons(items: list[str] | None, limit: int = 3) -> str:
    return " · ".join(str(r).lstrip("+?- ").strip() for r in (items or [])[:limit])
