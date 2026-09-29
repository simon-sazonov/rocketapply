"""Every market-wide source, and the rules that decide whether it runs.

Company ATS boards come from the target list (`run.py`). This module holds
everything else: the aggregators, the government portals and the remote
boards. Each is one `SourceSpec`: its switch in `config/job_search.yaml`, the
credentials it needs, whether it only lists remote jobs, and a builder that
turns the profile into zero or more adapters.

The rules are the same for all of them, and each skip is reported, not
silent:

* switched off in the profile            -> not built
* a credential it needs is missing       -> "add X to .env"
* none of your countries is one it covers -> not built
* a remote-only board when you do not accept remote work -> not built
* polled too recently for its own terms (Jobicy: one an hour) -> skipped this run

Adding a source is one adapter module plus one entry in `REGISTRY`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..config import Settings
from .queries import query_texts
from .settings import JobSearchConfig


@dataclass
class Context:
    cfg: JobSearchConfig
    settings: Settings
    repos: Any
    client: Any
    notes: list[str] = field(default_factory=list)

    @property
    def queries(self) -> list[str]:
        return query_texts(self.cfg, self.cfg.sources.queries)

    @property
    def primary(self) -> list[str]:
        return query_texts(self.cfg, self.cfg.sources.queries, primary_only=True)

    @property
    def countries(self) -> list[str]:
        return list(self.cfg.locations.countries)

    def wants(self, code: str) -> bool:
        """Is `code` a country the search covers? An empty list means anywhere."""
        return not self.countries or code in self.countries

    @property
    def work_from(self) -> list[str]:
        """Where the owner can do a remote job from: home, else anywhere authorised."""
        home = self.cfg.locations.home_country
        return [home] if home else list(self.cfg.work_authorization.authorized_countries)

    def cursors(self, name: str) -> dict[tuple[str, str], dict[str, Any]]:
        return cursors_with_yield(self.repos, name)

    def allowance(self, provider: str, per_day: int) -> int:
        """What is left of a keyed source's own daily cap."""
        return max(0, int(per_day) - self.repos.quota.used_since(provider, 1))


def cursors_with_yield(repos: Any, name: str) -> dict[tuple[str, str], dict[str, Any]]:
    """A source's search cursors, each with how many of its postings passed.

    The yield is counted from `jobs.found_query` (schema v7): the search that
    last returned a posting. A source whose searches span every country at
    once (EURES, Findwork: country "*") is counted per query.
    """
    rows = repos.source_queries.for_source(name)
    if not rows:
        return rows
    counts = repos.conn.execute(
        """
        SELECT j.found_query, COALESCE(j.source_country, ''), COUNT(*)
          FROM jobs j
          JOIN job_sources s ON s.id = j.source_id
          JOIN job_matches m ON m.job_id = j.id
         WHERE s.name = ? AND m.passed = 1 AND j.found_query IS NOT NULL
         GROUP BY 1, 2
        """,
        (name,),
    ).fetchall()
    by_query: dict[str, int] = {}
    exact: dict[tuple[str, str], int] = {}
    for query, country, n in counts:
        key = str(query).lower()
        by_query[key] = by_query.get(key, 0) + int(n)
        exact[(key, str(country).upper())] = int(n)
    for (query, country), row in rows.items():
        row["relevant"] = by_query.get(query, 0) if country in ("*", "") else \
            exact.get((query, country.upper()), 0)
    return rows


@dataclass(frozen=True)
class SourceSpec:
    key: str                         # attribute under `sources:` in the profile
    label: str                       # for the Job Setup page
    covers: str                      # where it finds jobs, in words
    build: Callable[[Context], list[Any]]
    needs: tuple[str, ...] = ()      # Settings attributes that must be set
    env: tuple[str, ...] = ()        # …and how they are spelled in .env
    remote_only: bool = False
    signup: str = ""


# ───────────────────────────── the builders ──────────────────────────────


def _arbeitnow_uk(ctx: Context) -> list[Any]:
    from ..sources.jobs.arbeitnow import ENDPOINT_UK, ArbeitnowSource

    if not ctx.wants("GB"):
        return []
    return [ArbeitnowSource(max_pages=ctx.cfg.sources.arbeitnow_uk.max_pages,
                            endpoint=ENDPOINT_UK, name="arbeitnow-uk",
                            settings=ctx.settings, client=ctx.client)]


def _eures(ctx: Context) -> list[Any]:
    from ..sources.jobs.eures import MEMBERS, EuresSource

    members = [c for c in ctx.countries if c in MEMBERS] if ctx.countries else sorted(MEMBERS)
    if not members:
        return []
    return [EuresSource(queries=ctx.queries, countries=members, cursors=ctx.cursors("eures"),
                        max_pages_per_search=ctx.cfg.sources.eures.max_pages_per_search,
                        settings=ctx.settings, client=ctx.client)]


def _workable(ctx: Context) -> list[Any]:
    from ..sources.jobs.workable_search import WorkableSearchSource

    if not ctx.countries:
        ctx.notes.append("workable-search: needs a country list to search by location")
        return []
    return [WorkableSearchSource(queries=ctx.queries, countries=ctx.countries,
                                 cursors=ctx.cursors("workable-search"),
                                 settings=ctx.settings, client=ctx.client)]


def _jooble(ctx: Context) -> list[Any]:
    from ..sources.jobs.jooble import LOCATIONS, JoobleSource

    covered = [c for c in LOCATIONS if ctx.wants(c)]
    if not covered:
        return []
    conf = ctx.cfg.sources.jooble
    left = ctx.allowance("jooble", conf.per_day)
    if left == 0:
        ctx.notes.append(f"jooble: today's {conf.per_day} requests are spent")
        return []
    return [JoobleSource(ctx.settings.jooble_api_key_us, queries=ctx.queries,
                         countries=covered, cursors=ctx.cursors("jooble"), max_requests=left,
                         max_pages_per_search=conf.max_pages_per_search,
                         settings=ctx.settings, client=ctx.client)]


def _itjobs(ctx: Context) -> list[Any]:
    from ..sources.jobs.itjobs import ITJobsSource

    if not ctx.wants("PT"):
        return []
    return [ITJobsSource(ctx.settings.itjobs_api_key, queries=ctx.queries,
                         cursors=ctx.cursors("itjobs"),
                         max_pages_per_search=ctx.cfg.sources.itjobs.max_pages_per_search,
                         settings=ctx.settings, client=ctx.client)]


def _jobtech(ctx: Context) -> list[Any]:
    from ..sources.jobs.jobtech import JobTechSource

    if not ctx.wants("SE"):
        return []
    return [JobTechSource(queries=ctx.queries, cursors=ctx.cursors("jobtech"),
                          settings=ctx.settings, client=ctx.client)]


def _devjobs(ctx: Context) -> list[Any]:
    from ..sources.jobs.devjobs import HOSTS, DevJobsSource

    return [DevJobsSource(code, settings=ctx.settings, client=ctx.client)
            for code in HOSTS if ctx.wants(code)]


def _mycareersfuture(ctx: Context) -> list[Any]:
    from ..sources.jobs.mycareersfuture import MyCareersFutureSource

    if not ctx.wants("SG"):
        return []
    return [MyCareersFutureSource(queries=ctx.queries, cursors=ctx.cursors("mycareersfuture"),
                                  settings=ctx.settings, client=ctx.client)]


def _himalayas(ctx: Context) -> list[Any]:
    from ..sources.jobs.himalayas import HimalayasSource

    if not ctx.work_from:
        ctx.notes.append("himalayas: set your home country, so remote jobs are searched "
                         "for where you can work from")
        return []
    return [HimalayasSource(queries=ctx.primary, work_from=ctx.work_from,
                            cursors=ctx.cursors("himalayas"),
                            max_pages_per_search=ctx.cfg.sources.himalayas.max_pages_per_search,
                            settings=ctx.settings, client=ctx.client)]


def _jobicy(ctx: Context) -> list[Any]:
    from ..sources.jobs.jobicy import JobicySource

    return [JobicySource(industry=ctx.cfg.sources.jobicy.industry, settings=ctx.settings,
                         client=ctx.client)]


def _remoteok(ctx: Context) -> list[Any]:
    from ..sources.jobs.remoteok import RemoteOKSource

    return [RemoteOKSource(tags=ctx.cfg.sources.remoteok.tags, settings=ctx.settings,
                           client=ctx.client)]


def _findwork(ctx: Context) -> list[Any]:
    from ..sources.jobs.findwork import FindworkSource

    conf = ctx.cfg.sources.findwork
    left = ctx.allowance("findwork", conf.per_day)
    if left == 0:
        return []
    return [FindworkSource(ctx.settings.findwork_api_key, queries=ctx.primary,
                           cursors=ctx.cursors("findwork"), max_requests=left,
                           settings=ctx.settings, client=ctx.client)]


#: Adzuna and Arbeitnow (DE) stay in `run.py`: they predate this registry and
#: carry their own quota logic and tests there.
REGISTRY: tuple[SourceSpec, ...] = (
    SourceSpec("eures", "EURES", "every EU country, and CH", _eures),
    SourceSpec("workable_search", "Workable search", "every country on your list", _workable),
    SourceSpec("arbeitnow_uk", "Arbeitnow UK", "GB", _arbeitnow_uk),
    SourceSpec("jooble", "Jooble", "US", _jooble, needs=("jooble_api_key_us",),
               env=("JOOBLE_API_KEY_US",), signup="https://jooble.org/api/about"),
    SourceSpec("itjobs", "ITJobs.pt", "PT", _itjobs, needs=("itjobs_api_key",),
               env=("ITJOBS_API_KEY",), signup="https://www.itjobs.pt/api"),
    SourceSpec("jobtech", "JobTech (Arbetsförmedlingen)", "SE", _jobtech),
    SourceSpec("devjobs", "DevJobs boards", "DE, CH, GB, NL", _devjobs),
    SourceSpec("mycareersfuture", "MyCareersFuture", "SG", _mycareersfuture),
    SourceSpec("himalayas", "Himalayas", "remote", _himalayas, remote_only=True),
    SourceSpec("jobicy", "Jobicy", "remote", _jobicy, remote_only=True),
    SourceSpec("remoteok", "Remote OK", "remote", _remoteok, remote_only=True),
    SourceSpec("findwork", "Findwork", "remote, mostly US", _findwork,
               needs=("findwork_api_key",), env=("FINDWORK_API_KEY",), remote_only=True,
               signup="https://findwork.dev/developers/"),
)


def too_soon(repos: Any, source: Any, now: datetime | None = None) -> float | None:
    """Hours left before a source that limits polling may run again, or None."""
    hours = getattr(source, "min_interval_hours", None)
    if not hours:
        return None
    row = repos.job_sources.get(source.name) or {}
    last = row.get("last_success_at")
    if not last:
        return None
    try:
        then = datetime.fromisoformat(str(last)).replace(tzinfo=UTC)
    except ValueError:
        return None
    elapsed = ((now or datetime.now(UTC)) - then).total_seconds() / 3600
    return None if elapsed >= hours else hours - elapsed


def market_sources(ctx: Context) -> list[Any]:
    """Every market-wide source this profile enables, with each skip explained."""
    out: list[Any] = []
    remote_ok = "remote" in ctx.cfg.locations.work_modes
    for spec in REGISTRY:
        if not getattr(ctx.cfg.sources, spec.key).enabled:
            continue
        missing = [env for attr, env in zip(spec.needs, spec.env, strict=True)
                   if not getattr(ctx.settings, attr, "")]
        if missing:
            ctx.notes.append(f"{spec.key}: not configured — add {', '.join(missing)} to .env"
                             + (f" (free at {spec.signup})" if spec.signup else ""))
            continue
        if spec.remote_only and not remote_ok:
            continue
        for source in spec.build(ctx):
            wait = too_soon(ctx.repos, source)
            if wait is not None:
                ctx.notes.append(f"{source.name}: polled under its minimum interval ago — "
                                 f"next in {wait * 60:.0f} min")
                continue
            out.append(source)
    return out
