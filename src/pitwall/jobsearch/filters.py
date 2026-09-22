"""Hard eligibility — §12, §14, §21, §59 of the spec.

Deterministic, cheap, and run before anything expensive. Every rule here acts
only on something the posting STATES:

* no country        -> passes the country rule
* no years stated   -> passes the experience rule
* no visa sentence  -> passes the visa rule
* no job type       -> passes the employment rule

§90 is explicit that a false rejection is the costly error — a silently lost
good job is worse than one more uncertain job to read. So a rejected job is
kept, with every reason it failed, and the inbox's "Filtered out" tab exists to
audit exactly these rules.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .locations import canonical_city, country_name, region_contains
from .settings import JobSearchConfig
from .visa import NO_SPONSORSHIP


@dataclass(frozen=True)
class Rejection:
    code: str          # country | remote | work_mode | city | role | seniority |
    #                    experience | employment | visa | education
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


#: Labels for the Filtered-out tab, in the order the rules run.
REASON_LABELS = {
    "country": "Wrong country",
    "remote": "Remote, but not open to you",
    "work_mode": "Work mode",
    "city": "Excluded city",
    "role": "Not a target role",
    "seniority": "Too senior (title)",
    "experience": "Too much experience required",
    "employment": "Employment type",
    "visa": "No sponsorship",
    "education": "PhD required",
}


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return value
    try:
        out = json.loads(value or "[]")
        return out if isinstance(out, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _names(codes: list[str]) -> str:
    return ", ".join(country_name(c) for c in codes)


def _word_re(words: list[str]) -> re.Pattern[str] | None:
    words = [w.strip() for w in words if w and w.strip()]
    if not words:
        return None
    body = "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in sorted(words, key=len,
                                                                         reverse=True))
    return re.compile(rf"(?<![\w-])(?:{body})\.?(?![\w-])", re.I)


def remote_open_to_you(region: str | None, cfg: JobSearchConfig) -> bool | None:
    """Can the owner take a remote job restricted to `region`?

    True / False when the profile says enough to know; None when it does not
    (no home country and no authorised countries) — which the filter treats as
    unknown, and therefore passes.
    """
    if not region or region == "WORLD":
        return True
    wa = cfg.work_authorization
    home = cfg.locations.home_country
    places = set(wa.authorized_countries) | ({home} if home else set())
    if not wa.needs_sponsorship:
        places |= set(cfg.locations.countries)
    if not places:
        return None
    if len(region) == 2:
        return region in places
    return any(region_contains(region, place) for place in places)


def hard_filter(job: dict[str, Any], cfg: JobSearchConfig) -> list[Rejection]:
    """Every rule the posting fails. Empty = it passes. Pure."""
    out: list[Rejection] = []
    countries = _list(job.get("countries"))
    remote = job.get("remote_status") or "unknown"
    region = job.get("remote_region")
    allowed = cfg.locations.countries

    cities = [c.lower() for c in _list(job.get("cities"))]

    # ── where ──
    # §59 applies to a job that is ONLY remote. "Hamburg, München, remote" is
    # an office job with a remote option, and judging it as remote-in-Germany
    # would drop an on-site role that sponsorship could still open.
    if remote == "remote" and region and not cities:
        verdict = remote_open_to_you(region, cfg)
        if verdict is False:
            where = country_name(region) if len(region) == 2 else region
            out.append(Rejection(
                "remote", f"remote, but only for people in {where} (§59) — you are not "
                          f"authorised to work there",
            ))
        elif len(region) == 2 and allowed and region not in allowed:
            out.append(Rejection("country", f"remote within {country_name(region)}, "
                                            f"which is not on your country list"))
    elif countries and allowed and not set(countries) & set(allowed):
        out.append(Rejection("country", f"located in {_names(countries)}"))

    if remote in ("remote", "hybrid", "onsite") and remote not in cfg.locations.work_modes:
        out.append(Rejection("work_mode", f"{remote} — not a work mode you accept"))

    excluded = {canonical_city(c).lower() for c in cfg.locations.excluded_cities}
    if cities and excluded and all(canonical_city(c).lower() in excluded for c in cities):
        out.append(Rejection("city", f"only in {', '.join(c.title() for c in cities)}"))

    # ── what ──
    labels = _list(job.get("role_labels")) or ["OTHER"]
    if not set(labels) & set(cfg.roles.accepted):
        out.append(Rejection("role", "classified " + ", ".join(
            label.replace("_", " ").lower() for label in labels)))

    title = job.get("title") or ""
    excluded_words = _word_re(cfg.seniority.excluded_title_words)
    allowed_phrases = _word_re(cfg.seniority.allowed_title_phrases)
    hit = excluded_words.search(title) if excluded_words else None
    if hit and not (allowed_phrases and allowed_phrases.search(title)):
        out.append(Rejection("seniority", f"title says \"{hit.group(0).strip()}\""))

    minimum = job.get("experience_min")
    if (job.get("experience_required") and minimum is not None
            and float(minimum) > cfg.seniority.max_experience_years):
        evidence = job.get("experience_evidence") or ""
        out.append(Rejection(
            "experience",
            f"requires {float(minimum):g}+ years (your limit is "
            f"{cfg.seniority.max_experience_years:g})" + (f": \"{evidence}\"" if evidence else ""),
        ))

    types = _list(job.get("employment_types"))
    if types and not set(types) & set(cfg.employment_types):
        out.append(Rejection("employment", ", ".join(t.replace("_", " ") for t in types)))

    # ── eligibility ──
    if job.get("visa_status") == NO_SPONSORSHIP and cfg.work_authorization.needs_sponsorship:
        scope = job.get("visa_scope")
        authorized = set(cfg.work_authorization.authorized_countries)
        if scope and len(scope) == 2:
            blocked = scope not in authorized
        elif countries:
            # "…without sponsorship" about the job's own country (or a region
            # containing it): blocked unless you can work there already.
            blocked = not set(countries) & authorized
        else:
            # A refusal with no place attached, for a job with no known place:
            # nothing to compare against, so it is a concern, not a filter.
            blocked = False
        if blocked:
            out.append(Rejection("visa", f"\"{job.get('visa_evidence') or 'no sponsorship'}\""))

    if job.get("education_required") == "phd" and cfg.degree != "phd":
        out.append(Rejection("education", "a PhD is stated as required"))
    return out
