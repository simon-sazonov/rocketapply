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

from .locations import canonical_city, country_name, is_country_code, region_contains
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


def regions_of(job: dict[str, Any]) -> list[str]:
    """A job's remote regions: the list when stored (v7), else the single one."""
    regions = [str(r) for r in _list(job.get("remote_regions")) if r]
    if not regions and job.get("remote_region"):
        regions = [str(job["remote_region"])]
    return regions


def remote_open_to_you(region: str | list[str] | tuple[str, ...] | None,
                       cfg: JobSearchConfig) -> bool | None:
    """Can the owner take a remote job open in `region` (one, or alternatives)?

    True / False when the posting and the profile say enough to know. None when
    they do not: the posting names no region, or the profile names no place
    the owner can work from. The filter passes None; scoring shows it as a
    question.

    A bare "Remote" used to count as open everywhere. It is not: most remote
    jobs are remote within the employer's country, and reading silence as
    "worldwide" told the owner no sponsorship was needed for jobs he cannot
    legally take.
    """
    regions = [region] if isinstance(region, str) else [r for r in (region or []) if r]
    if not regions:
        return None
    if "WORLD" in regions:
        return True
    wa = cfg.work_authorization
    home = cfg.locations.home_country
    places = set(wa.authorized_countries) | ({home} if home else set())
    if not wa.needs_sponsorship:
        places |= set(cfg.locations.countries)
    if not places:
        return None
    return any(
        (r in places) if is_country_code(r) else any(region_contains(r, p) for p in places)
        for r in regions
    )


def hard_filter(job: dict[str, Any], cfg: JobSearchConfig) -> list[Rejection]:
    """Every rule the posting fails. Empty = it passes. Pure."""
    out: list[Rejection] = []
    countries = _list(job.get("countries"))
    remote = job.get("remote_status") or "unknown"
    regions = regions_of(job)
    allowed = cfg.locations.countries

    cities = [c.lower() for c in _list(job.get("cities"))]

    # ── where ──
    # §59 applies to a job that is ONLY remote. "Hamburg, München, remote" is
    # an office job with a remote option, and judging it as remote-in-Germany
    # would drop an on-site role that sponsorship could still open.
    if remote == "remote" and regions and not cities:
        verdict = remote_open_to_you(regions, cfg)
        where = " or ".join(country_name(r) if is_country_code(r) else r for r in regions)
        if verdict is False:
            out.append(Rejection(
                "remote", f"remote, but only for people in {where} (§59) — you are not "
                          f"authorised to work there",
            ))
        elif (allowed and all(is_country_code(r) for r in regions)
              and not set(regions) & set(allowed)):
            out.append(Rejection("country", f"remote within {where}, "
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
        if is_country_code(scope):
            blocked = scope not in authorized
        elif countries:
            # A region ("the right to work in the EU") about a job in a country
            # you can work in is met; the same words about a job anywhere else
            # are not, since a permit for one EU country is not one for all.
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
