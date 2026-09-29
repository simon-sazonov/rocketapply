"""The job search as one tracked run — the pipeline of §2.3 and §72.

    sync targets ─► poll ─► normalize ─► dedupe ─► filter + score ─► [AI fit]

Built on `Pipeline` rather than beside it, so a job-search run is an ordinary
`runs` row with `run_events` (§49), the same progress plumbing the Streamlit
pages already watch, and the same source-polling loop the Openings page uses —
one failing source never ends the run (§48).

Every stage after polling is local and deterministic, which is what makes
"Re-rank" after a profile edit a matter of seconds rather than another poll.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..db import transaction
from ..pipeline import Pipeline, RunParams, RunStats
from . import sponsors
from .ats_harvest import Candidate, boards_to_poll, harvest, judge, pending, record
from .companies import TargetIndex, load_targets, normalize_company, sync_targets, target_rows
from .dedupe import assign_clusters
from .filters import hard_filter
from .market import Context as MarketContext
from .market import cursors_with_yield, market_sources
from .normalize import normalize
from .queries import query_texts
from .scoring import score
from .settings import JobSearchConfig, load_config
from .skills import load_my_skills, vocabulary
from .store import rows_to_normalize, rows_to_score, save_ai_fit, save_match, save_normalized

log = logging.getLogger(__name__)

COMMAND = "job-search"


@dataclass
class JobSearchStats:
    sources: int = 0
    sources_failed: int = 0
    seen: int = 0
    new: int = 0
    updated: int = 0
    closed: int = 0
    normalized: int = 0
    clustered: int = 0
    merged: int = 0
    possible_duplicates: int = 0
    boards_found: int = 0
    boards_verified: int = 0
    scored: int = 0
    passed: int = 0
    ai_scored: int = 0
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


#: Groq's free tier refuses any single request over 8,000 tokens (HTTP 413) —
#: separate from the per-minute pause. The first live AI-fit run sent eight
#: postings of 3,000 characters plus the CV and was refused. So a request is
#: sized to this budget, in tokens, prompt and answer together.
AI_FIT_REQUEST_TOKENS = 5000
AI_FIT_DESCRIPTION_CHARS = 1800
_CHARS_PER_TOKEN = 4
_ANSWER_TOKENS_PER_JOB = 150


def fit_batch_size(profile: str, configured: int) -> int:
    """How many postings fit in one AI-fit request beside the CV."""
    fixed = len(profile) // _CHARS_PER_TOKEN + 800          # the CV and the instructions
    per_job = (AI_FIT_DESCRIPTION_CHARS + 300) // _CHARS_PER_TOKEN + _ANSWER_TOKENS_PER_JOB
    room = max(1, (AI_FIT_REQUEST_TOKENS - fixed) // per_job)
    return max(1, min(int(configured), room))


class JobSearch:
    """One owner's search, over one database."""

    def __init__(self, pipeline: Pipeline | None = None,
                 cfg: JobSearchConfig | None = None) -> None:
        self.pipe = pipeline or Pipeline()
        self.settings = self.pipe.settings
        self.repos = self.pipe.repos
        self.conn = self.repos.conn
        self.cfg = cfg or load_config()
        self._client: Any = None

    def client(self) -> Any:
        """One client per run: its per-host pacing and its cache are shared by
        every source, and by the board checks after polling."""
        if self._client is None:
            from ..sources.jobs import JobCache, PoliteClient

            self._client = PoliteClient(
                self.settings,
                cache=JobCache(settings=self.settings, ttl_hours=self.cfg.sources.cache_hours),
            )
        return self._client

    # ── stage 0: the target list ──

    def sync_targets(self, stats: JobSearchStats) -> TargetIndex:
        targets, report = load_targets()
        for issue in report.issues:
            stats.notes.append(f"target_companies.csv row {issue.row}: {issue.message}")
        with self.repos.writing():
            sync_targets(self.conn, targets)
        return TargetIndex(target_rows(self.conn))

    # ── stage 1: sources ──

    def build_sources(self, index: TargetIndex, stats: JobSearchStats) -> list[Any]:
        """Every source this profile enables. What is skipped is said, not hidden."""
        from ..sources.jobs import AdzunaSource, ArbeitnowSource, ATSSource

        src_cfg = self.cfg.sources
        client = self.client()
        out: list[Any] = []
        if src_cfg.target_ats:
            for row in index.rows.values():
                if not row.get("ats_kind") or not row.get("ats_token"):
                    continue
                try:
                    source = ATSSource(
                        row["ats_kind"], row["ats_token"],
                        company_id=row.get("networking_company_id"),
                        company_name=row["name"], settings=self.settings, client=client,
                    )
                except ValueError as exc:
                    stats.notes.append(f"{row['name']}: {exc}")
                    continue
                source.target_company_id = int(row["id"])  # type: ignore[attr-defined]
                out.append(source)
        if src_cfg.people_list_ats:
            # The people-search list's boards (the motorsport teams). Already
            # linked to their company, so their postings also reach Openings.
            taken = {(s.ats, s.token) for s in out if isinstance(s, ATSSource)}
            for row in self.repos.companies.all(active_only=True):
                kind, token = row.get("ats_kind"), row.get("ats_token")
                if not kind or not token or (kind, token) in taken:
                    continue
                try:
                    board = ATSSource(kind, token, company_id=int(row["id"]),
                                      company_name=row["name"], settings=self.settings,
                                      client=client)
                except ValueError as exc:
                    stats.notes.append(f"{row['name']}: {exc}")
                    continue
                target = index.match(normalize_company(row["name"]))
                board.target_company_id = int(target["id"]) if target else None  # type: ignore[attr-defined]
                taken.add((kind, token))
                out.append(board)
        if src_cfg.discovered.enabled:
            # Boards found from posting links on earlier runs (ats_harvest.py).
            taken = [(s.ats, s.token) for s in out if isinstance(s, ATSSource)]
            for board in boards_to_poll(self.conn, taken):
                try:
                    out.append(ATSSource(board["kind"], board["token"],
                                         company_name=board.get("company"),
                                         settings=self.settings, client=client))
                except ValueError as exc:
                    stats.notes.append(f"{board['kind']}:{board['token']}: {exc}")
        if src_cfg.arbeitnow.enabled:
            out.append(ArbeitnowSource(max_pages=src_cfg.arbeitnow.max_pages,
                                       settings=self.settings, client=client))
        adzuna = src_cfg.adzuna
        if adzuna.enabled:
            if not (self.settings.adzuna_app_id and self.settings.adzuna_app_key):
                stats.notes.append(
                    "adzuna: not configured — add ADZUNA_APP_ID and ADZUNA_APP_KEY to .env "
                    "(free at developer.adzuna.com)"
                )
            else:
                from ..sources.jobs.budget import SourceBudget, allowance, usage_of

                search = AdzunaSource(
                    self.settings.adzuna_app_id, self.settings.adzuna_app_key,
                    queries=query_texts(self.cfg, src_cfg.queries),
                    countries=adzuna.countries or self.cfg.locations.countries,
                    max_requests=None, results_per_page=adzuna.results_per_page,
                    backfill_days=adzuna.backfill_days,
                    max_pages_per_search=adzuna.max_pages_per_search,
                    title_only=adzuna.title_only,
                    cursors=cursors_with_yield(self.repos, "adzuna"),
                    settings=self.settings, client=client,
                )
                budget = SourceBudget(adzuna.per_day, adzuna.per_week, adzuna.per_month)
                used = usage_of(self.repos.quota, "adzuna")
                search.max_requests = allowance(budget, used, pace=adzuna.pace,
                                                backfill_pending=search.backfill_pending())
                if search.unsupported:
                    stats.notes.append(
                        "adzuna has no site for " + ", ".join(search.unsupported)
                        + " — other sources cover those countries"
                    )
                if search.max_requests == 0:
                    stats.notes.append(
                        f"adzuna: its free quota is spent for now ({used.day} today, "
                        f"{used.week} this week, {used.month} this month) — it resumes "
                        "when the window rolls"
                    )
                else:
                    out.append(search)

        ctx = MarketContext(self.cfg, self.settings, self.repos, client)
        out.extend(market_sources(ctx))
        stats.notes.extend(ctx.notes)
        return out

    def poll(self, index: TargetIndex, stats: JobSearchStats, run_stats: RunStats,
             *, no_cache: bool = False, sources: list[Any] | None = None) -> None:
        """`sources` replaces the configured ones — how the tests run offline."""
        if sources is None:
            sources = self.build_sources(index, stats)
        stats.sources = len(sources)
        if not sources:
            stats.notes.append("no job sources enabled — see the Sources tab of 🧭 Job Setup")
            return
        exact = self.networking_names()

        def link(source: Any, posting: Any) -> tuple[int | None, int | None]:
            # The people-search list is matched on its CANONICAL name only.
            # Its aliases are loose on purpose ("Genesis" for Genesis Magma
            # Racing) because the LinkedIn sweep has negative keywords to
            # disambiguate; an open job feed has none, and the first live run
            # filed a Paris AI start-up's vacancy under a WEC team that way.
            company_id = getattr(source, "company_id", None)
            if company_id is None:
                company_id = exact.get(normalize_company(posting.company_name))
            target_id = getattr(source, "target_company_id", None)
            if target_id is None:
                hit = index.match(normalize_company(posting.company_name))
                target_id = int(hit["id"]) if hit else None
            return company_id, target_id

        before = (run_stats.jobs_seen, run_stats.jobs_new, run_stats.jobs_updated,
                  run_stats.jobs_closed, run_stats.job_sources_failed)
        try:
            self.pipe.poll_sources(sources, run_stats, link=link, no_cache=no_cache,
                                   stage="job-search")
        finally:
            self.record_spend(sources)
        stats.seen += run_stats.jobs_seen - before[0]
        stats.new += run_stats.jobs_new - before[1]
        stats.updated += run_stats.jobs_updated - before[2]
        stats.closed += run_stats.jobs_closed - before[3]
        stats.sources_failed += run_stats.job_sources_failed - before[4]

    def record_spend(self, sources: list[Any]) -> None:
        """Quota and search cursors, written here on the thread that owns the
        connection — the sources counted them on their worker threads.

        Runs even when polling failed part-way: a request that was made is
        spent whether or not its answer was stored.
        """
        for source in sources:
            provider = getattr(source, "provider", None)
            made = int(getattr(source, "requests_made", 0) or 0)
            visits = list(getattr(source, "visits", None) or [])
            if not ((provider and made) or visits):
                continue
            with self.repos.writing():
                if provider and made:
                    self.repos.quota.consume(provider, made)
                for visit in visits:
                    self.repos.source_queries.record(
                        source.name, visit.query, visit.country, requests=visit.requests,
                        found=visit.found, complete=visit.complete, total=visit.total,
                    )

    def networking_names(self) -> dict[str, int]:
        """Normalised canonical name -> people-search company id. No aliases."""
        out: dict[str, int] = {}
        for company in self.repos.companies.all(active_only=False):
            key = normalize_company(company["name"])
            if key:
                out.setdefault(key, int(company["id"]))
        return out

    def relink(self) -> int:
        """Undo alias-based links on postings from the job search's sources.

        Runs every time, so links made by an older rule heal themselves. The
        Openings page's own sources are left alone: they link by alias on
        purpose, and each of them is one company's own board anyway.
        """
        exact = self.networking_names()
        rows = self.conn.execute(
            """
            SELECT j.id, j.company_id, j.posting_company FROM jobs j
              JOIN job_sources s ON s.id = j.source_id
             WHERE j.company_id IS NOT NULL AND s.company_id IS NULL
               AND (s.kind IN ('job_board', 'aggregator_api')
                    OR s.target_company_id IS NOT NULL)
            """
        ).fetchall()
        fixed = 0
        with self.repos.writing():
            for row in rows:
                right = exact.get(normalize_company(row["posting_company"]))
                if right != row["company_id"]:
                    self.conn.execute("UPDATE jobs SET company_id = ? WHERE id = ?",
                                      (right, row["id"]))
                    fixed += 1
        return fixed

    # ── stage 2-3: normalise, dedupe ──

    def normalize(self, index: TargetIndex, stats: JobSearchStats, *, force: bool = False) -> None:
        relinked = self.relink()
        if relinked:
            stats.notes.append(f"{relinked} posting(s) unlinked from a people-search company "
                               "they only shared a nickname with")
        vocab = vocabulary()
        rows = rows_to_normalize(self.conn, force=force)
        self.pipe.progress("normalize", f"{len(rows)} posting(s) to read", 0, len(rows))
        with transaction(self.conn):
            for row in rows:
                target = index.get(row.get("target_company_id"))
                if target is None:
                    target = index.match(normalize_company(
                        row.get("posting_company") or row.get("networking_name")
                    ))
                fields = normalize(row, vocab=vocab, target=target,
                                   company_fallback=row.get("networking_name"))
                if target is not None and not row.get("target_company_id"):
                    fields["target_company_id"] = int(target["id"])
                save_normalized(self.conn, int(row["id"]), fields)
                stats.normalized += 1
        with transaction(self.conn):
            result = assign_clusters(self.conn)
        stats.clustered += result.clustered
        stats.merged += result.merged
        stats.possible_duplicates += result.possible
        self.pipe.progress(
            "dedupe", f"{result.clustered} new posting(s) clustered · {result.merged} merged "
                      f"into an existing vacancy · {result.possible} possible duplicate(s)",
            1, 1,
        )

    # ── stage 3b: company boards found from posting links ──

    def discover(self, index: TargetIndex, stats: JobSearchStats, run_stats: RunStats,
                 *, no_cache: bool = False) -> None:
        """Harvest board links, verify the pending ones, and store what the
        verified boards list — in this run, from the responses just read.

        Reading a board to verify it IS polling it, so its postings are stored
        at once (the second read is a cache hit), not a day later.
        """
        from concurrent.futures import ThreadPoolExecutor

        from ..sources.jobs import ATSSource

        conf = self.cfg.sources.discovered
        if not conf.enabled:
            return
        since = int(self.repos.state.get("ats_harvest.max_id", "0") or 0)
        with self.repos.writing():
            added, top = harvest(self.conn, since_id=since)
            self.repos.state.set("ats_harvest.max_id", top)
        stats.boards_found += added
        todo = pending(self.conn, conf.max_checks_per_run)
        if not todo:
            return
        self.pipe.progress("boards", f"checking {len(todo)} board(s) linked from postings",
                           0, len(todo))
        client = self.client()

        def check(c: Candidate) -> tuple[Candidate, Any, list[Any] | None, str]:
            board = ATSSource(c.kind, c.token, company_name=c.company,
                              settings=self.settings, client=client)
            try:
                return c, board, board.fetch(no_cache=no_cache), ""
            except Exception as exc:  # noqa: BLE001 - a bad board is a verdict, not a crash
                return c, board, None, f"{type(exc).__name__}: {exc}"[:240]

        verified: list[Any] = []
        workers = max(1, int(self.settings.jobs_parallel_sources))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="board") as pool:
            for n, (cand, board, postings, error) in enumerate(pool.map(check, todo), 1):
                if postings is None:
                    ok, reason, count = False, error or "no answer", 0
                else:
                    ok, reason = judge(cand, postings)
                    count = len(postings)
                with self.repos.writing():
                    record(self.conn, cand, verified=ok, reason=reason, postings=count)
                if ok:
                    verified.append(board)
                self.pipe.progress("boards", f"{n}/{len(todo)} · {cand.kind}:{cand.token}: "
                                             f"{'✓ ' + str(count) if ok else '✕ ' + reason[:60]}",
                                   n, len(todo))
        stats.boards_verified += len(verified)
        if verified:
            self.poll(index, stats, run_stats, no_cache=False, sources=verified)
            self.normalize(index, stats)

    # ── stage 4: filter and score ──

    def score(self, index: TargetIndex, stats: JobSearchStats) -> None:
        vocab = vocabulary()
        owned, warnings = load_my_skills(vocab=vocab)
        stats.notes.extend(warnings)
        sponsor_index = sponsors.load(self.settings)
        rows = rows_to_score(self.conn)
        self.attach_cv_similarity(rows)
        fingerprint = self.cfg.fingerprint()
        weights = self.cfg.weights.model_dump()
        self.pipe.progress("score", f"{len(rows)} posting(s) to rank", 0, len(rows))
        with transaction(self.conn):
            for row in rows:
                target = index.get(row.get("target_company_id"))
                rejections = hard_filter(row, self.cfg)
                match = score(row, self.cfg, owned, vocab, target, sponsor_index)
                save_match(self.conn, int(row["id"]), config_hash=fingerprint,
                           passed=not rejections,
                           reasons=[r.as_dict() for r in rejections], match=match,
                           weights=weights)
                stats.scored += 1
                stats.passed += int(not rejections)
        self.pipe.progress("score", f"{stats.passed} of {stats.scored} pass the hard filters",
                           len(rows), len(rows))

    def attach_cv_similarity(self, rows: list[dict[str, Any]]) -> None:
        """Each row's CV-similarity percentile among the rows being scored."""
        from ..config import config_dir
        from .similarity import CVSimilarity, profile_text

        cv = profile_text(config_dir(), config_dir().parent / "profile")
        if not cv.strip() or not rows:
            return
        texts = [f"{r.get('title') or ''}\n{r.get('description') or ''}" for r in rows]
        model = CVSimilarity(cv, texts)
        for row, rank in zip(rows, model.rank([model.cosine(t) for t in texts]), strict=True):
            row["cv_similarity"] = rank

    # ── stage 5: the optional second opinion ──

    def ai_fit(self, stats: JobSearchStats, run_stats: RunStats, top_n: int) -> None:
        """§26: the LLM reads only the strongest few, after every cheap stage.

        Sends `config/profile.md` and the public posting — nothing from the
        contact fields (§51). Skipped, with a note, when either is missing.
        """
        from typing import cast

        from ..scoring.jobs import JobRelevanceScorer, job_payload, load_profile
        from ..scoring.llm import LLMUnavailable

        if top_n <= 0:
            return
        profile = load_profile(self.settings)
        if not profile:
            stats.notes.append("AI fit skipped — write your CV on 🧭 Job Setup → My CV "
                               "(config/my_profile.md) to enable it")
            return
        rows = [dict(r) for r in self.conn.execute(
            """
            SELECT j.id, j.title, j.description, j.location, j.employment_type,
                   COALESCE(t.name, j.posting_company) AS company_name
              FROM job_matches m JOIN jobs j ON j.id = m.job_id
              LEFT JOIN target_companies t ON t.id = j.target_company_id
             WHERE m.passed = 1 AND m.ai_score IS NULL AND j.status = 'open'
               AND j.id = j.cluster_id
             ORDER BY m.score DESC LIMIT ?
            """,
            (top_n,),
        )]
        if not rows:
            return
        pipe = self.pipe
        pipe._llm_remaining = self.settings.llm_chain()
        pipe._llm_rate_limit_hits = 0
        pipe._llm_factory = JobRelevanceScorer
        opened = pipe._open_scorer(run_stats)
        if opened is None:
            stats.notes.append("AI fit skipped — no LLM provider is reachable")
            return
        scorer = cast("JobRelevanceScorer", opened)
        batch_size = fit_batch_size(profile, self.settings.llm_batch_size)
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            payload = [job_payload(r, description_limit=AI_FIT_DESCRIPTION_CHARS) for r in batch]
            outcome = scorer.score_jobs(profile, payload)
            if outcome.provider_error:
                nxt = pipe._degrade(scorer, run_stats, LLMUnavailable(outcome.error or "failed"))
                if nxt is None:
                    break
                scorer = cast("JobRelevanceScorer", nxt)
                outcome = scorer.score_jobs(profile, payload)
            with self.repos.writing():
                self.repos.quota.consume(f"llm:{scorer.provider}", 1)
                for verdict in outcome.verdicts:
                    save_ai_fit(self.conn, verdict.job_id, verdict.score,
                                [*verdict.reasons, *(f"⚠ {f}" for f in verdict.red_flags)],
                                outcome.model)
                    stats.ai_scored += 1
            pipe.progress("ai-fit", f"{stats.ai_scored}/{len(rows)} read by the AI",
                          stats.ai_scored, len(rows))
        pipe._finish_llm(scorer, run_stats)

    # ── the whole run ──

    def run(self, *, poll: bool = True, no_cache: bool = False, ai_fit: int | None = None,
            renormalize: bool = False, sources: list[Any] | None = None) -> JobSearchStats:
        stats = JobSearchStats()
        run_stats = RunStats()
        started = time.monotonic()
        params = RunParams(no_cache=no_cache)
        with self.pipe.tracked(params, run_stats, COMMAND):
            index = self.sync_targets(stats)
            if poll:
                self.poll(index, stats, run_stats, no_cache=no_cache, sources=sources)
            self.normalize(index, stats, force=renormalize)
            if poll and sources is None:     # injected sources = a test, offline
                self.discover(index, stats, run_stats, no_cache=no_cache)
                stats.notes.extend(sponsors.refresh(self.settings, self.client()))
            self.score(index, stats)
            self.ai_fit(stats, run_stats, self.cfg.ai_fit_top_n if ai_fit is None else ai_fit)
            stats.notes.extend(n for n in run_stats.notes if n not in stats.notes)
            stats.seconds = round(time.monotonic() - started, 1)
            self.pipe.progress(
                "job-search",
                f"{stats.passed} relevant of {stats.scored} open posting(s) · "
                f"{stats.new} new · {stats.sources_failed} source(s) failed",
                1, 1,
            )
        return stats

    def rerank(self) -> JobSearchStats:
        """After a profile edit: re-read and re-score what is stored, no network."""
        return self.run(poll=False, renormalize=True, ai_fit=0)
