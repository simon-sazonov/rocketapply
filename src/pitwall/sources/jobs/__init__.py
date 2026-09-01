"""Job sources — Phase 2 of PITWALL_V2_IMPLEMENTATION.md.

Same shape as `pitwall.search`: a protocol in `base`, one module per adapter,
a shared cache, and a builder that turns configuration into live adapters.

Sources come from two places, and both are additive:

* `config/job_sources.yaml` — hand-maintained aggregators and career pages;
* the `companies` table — `ats_kind` / `ats_token`, filled once by
  `scripts/discover_ats.py` and overridable in `config/companies.csv`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from ...config import Settings, config_dir, get_settings
from .aggregator import AggregatorSource
from .ats import ATS_KINDS, ENDPOINTS, ATSSource, endpoint_for
from .base import (
    JobCache,
    JobPosting,
    JobSource,
    PoliteClient,
    RobotsDisallowed,
    RobotsGate,
    SourceError,
)
from .career_page import CareerPageSource

log = logging.getLogger(__name__)

SOURCES_YAML = "job_sources.yaml"

__all__ = [
    "ATS_KINDS",
    "ENDPOINTS",
    "AggregatorSource",
    "ATSSource",
    "CareerPageSource",
    "JobCache",
    "JobPosting",
    "JobSource",
    "PoliteClient",
    "RobotsDisallowed",
    "RobotsGate",
    "SourceError",
    "build_sources",
    "endpoint_for",
    "load_source_config",
]


def load_source_config(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Read `config/job_sources.yaml`. A missing file is not an error."""
    p = Path(path) if path else config_dir() / SOURCES_YAML
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return list(data.get("sources") or [])


def build_sources(
    settings: Settings | None = None,
    companies: list[dict[str, Any]] | None = None,
    *,
    config_path: Path | str | None = None,
    client: PoliteClient | None = None,
    kinds: list[str] | None = None,
) -> list[JobSource]:
    """Every enabled adapter, from the config file and the companies table."""
    settings = settings or get_settings()
    kinds = kinds if kinds is not None else settings.jobs_sources_enabled
    client = client or PoliteClient(settings)
    out: list[JobSource] = []

    for company in companies or []:
        if "ats_json" not in kinds:
            break
        kind, token = company.get("ats_kind"), company.get("ats_token")
        if not kind or not token:
            continue
        try:
            out.append(ATSSource(
                kind, token, company_id=company.get("id"),
                company_name=company.get("name"), settings=settings, client=client,
            ))
        except ValueError as exc:
            log.warning("skipping %s: %s", company.get("name"), exc)

    for row in load_source_config(config_path):
        if not row.get("enabled", True):
            log.info("job source %r is disabled: %s", row.get("name"), row.get("reason", ""))
            continue
        kind = row.get("kind")
        if kind not in kinds:
            continue
        try:
            if kind == "aggregator":
                out.append(AggregatorSource(
                    row["name"], row["sitemap_url"],
                    url_contains=row.get("url_contains", "/job"),
                    settings=settings, client=client,
                    max_details=row.get("max_details"),
                ))
            elif kind == "career_page":
                out.append(CareerPageSource(
                    row["name"], row["url"], settings=settings, client=client,
                ))
            elif kind == "ats_json":
                out.append(ATSSource(
                    row["ats"], row["token"], company_name=row.get("company"),
                    settings=settings, client=client,
                ))
        except (KeyError, ValueError) as exc:
            log.warning("job source %r is misconfigured: %s", row.get("name"), exc)
    return out
