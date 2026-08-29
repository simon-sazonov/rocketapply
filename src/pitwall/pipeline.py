"""The pipeline orchestrator — §3.1 of PITWALL_PLAN.md.

Six stages, each writing to SQLite and each **independently resumable**: if the
run dies at stage 5, stages 1–4 do not repeat.

    1 query builder  ->  2 search  ->  3 extract & dedupe
                     ->  4 heuristics  ->  5 LLM  ->  6 dashboard
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Settings, get_settings
from .db import Repos, open_repos
from .extract.linkedin import parse_profile
from .inputs import load_and_sync, load_negative_keywords
from .models import Company, LLMVerdict, Role, Score
from .query_builder import build_all
from .ratelimit import AdaptiveConcurrency
from .scoring import heuristics
from .scoring.llm import (
    LLMScorer,
    LLMUnavailable,
    QuotaExhausted,
    batched,
    ollama_reachable,
)
from .search import (
    ProviderError,
    RateLimited,
    SerpCache,
    build_chain,
)
from .search.base import QuotaExhausted as SearchQuotaExhausted
from .search.base import SearchProvider

log = logging.getLogger("pitwall.pipeline")

ProgressFn = Callable[[str, str, int, int], None]


@dataclass
class RunStats:
    run_id: int = 0
    queries_planned: int = 0
    queries_executed: int = 0
    queries_cached: int = 0
    queries_failed: int = 0
    results_fetched: int = 0
    rows_parsed: int = 0
    candidates_new: int = 0
    candidates_touched: int = 0
    blocked_skipped: int = 0
    heuristic_scored: int = 0
    heuristic_passed: int = 0
    llm_calls: int = 0
    llm_scored: int = 0
    llm_needs_review: int = 0
    llm_tokens_in: int = 0
    llm_tokens_out: int = 0
    cost_estimate: float = 0.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _LLMJob:
    """One batch, with everything read from SQLite before it leaves the thread."""

    rows: list[dict[str, Any]]
    payload: list[dict[str, Any]]
    company: Company | None
    role: Role | None
    company_id: int | None
    role_id: int | None


@dataclass
class RunParams:
    """Everything a run can be narrowed by. CLI flags and UI controls both
    land here — the top precedence layer of §3.3."""

    companies: list[str] | None = None
    roles: list[str] | None = None
    template_ids: list[str] | None = None
    max_queries: int | None = None
    max_results: int | None = None
    heuristic_threshold: float | None = None
    use_llm: bool = True
    no_cache: bool = False
    cache_only: bool = False
    reprocess: bool = False
    rescore_all: bool = False
    prompt_version: str | None = None
    limit: int | None = None
    #: Overrides both stage pools for one run; None = the configured values.
    concurrency: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Pipeline:
    def __init__(
        self,
        settings: Settings | None = None,
        repos: Repos | None = None,
        *,
        on_progress: ProgressFn | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self.repos = repos or open_repos(self.settings)
        self.conn: sqlite3.Connection = self.repos.conn
        self.cache = SerpCache(settings=self.settings)
        self._on_progress = on_progress
        self.markers = load_negative_keywords()
        self._run_id: int | None = None
        #: Providers still to try in this LLM stage; `_degrade` pops from it.
        self._llm_remaining: list[str] = []

    # ── progress plumbing ──

    def progress(self, stage: str, message: str, current: int = 0, total: int = 0) -> None:
        log.info("[%s] %s (%s/%s)", stage, message, current, total)
        if self._run_id:
            with contextlib.suppress(sqlite3.Error):  # pragma: no cover
                self.repos.runs.progress(self._run_id, stage, message, current, total)
        if self._on_progress:
            self._on_progress(stage, message, current, total)

    def cancelled(self) -> bool:
        return bool(self._run_id and self.repos.runs.is_cancelled(self._run_id))

    # ── provider chain ──

    def chain(self) -> list[SearchProvider]:
        """The chain, with serper's live credit balance folded in (§0.2).

        Reads the `quota` table rather than a parallel counter — that table is
        already the record of what has been spent.
        """
        return build_chain(self.settings, credits=self._provider_credits())

    def _provider_credits(self) -> dict[str, int | None]:
        return {name: self.repos.quota.credits(name) for name in ("serper", "serpapi")}

    # ─────────────────────── stage 0: inputs ────────────────────────

    def load_inputs(self, params: RunParams | None = None) -> tuple[list[Company], list[Role], Any]:
        companies, roles, report = load_and_sync(self.conn)
        if params:
            if params.companies:
                wanted = {c.casefold() for c in params.companies}
                companies = [c for c in companies if c.name.casefold() in wanted]
            if params.roles:
                wanted_r = {r.casefold() for r in params.roles}
                roles = [r for r in roles if r.title.casefold() in wanted_r]
        return companies, roles, report

    # ──────────────────── stage 1: query builder ─────────────────────

    def build_queries(self, params: RunParams | None = None) -> tuple[int, dict[str, Any]]:
        params = params or RunParams()
        companies, roles, report = self.load_inputs(params)
        if not companies or not roles:
            self.progress("build", "no active companies or roles — check config/*.csv", 0, 0)
            return 0, {"planned": 0, "capped_to": 0, "errors": len(report.errors)}

        chain = self.chain()
        provider = chain[0]
        specs, summary = build_all(
            companies,
            roles,
            self.settings,
            provider=provider.name,
            supports_operators=provider.supports_operators(),
            template_ids=params.template_ids,
            max_queries=params.max_queries,
        )
        inserted, existing = self.repos.queries.insert_many(specs)
        summary.update({"inserted": inserted, "already_known": existing, "provider": provider.name})
        self.progress(
            "build",
            f"{summary['planned']} planned, capped to {summary['capped_to']}, "
            f"{inserted} new for {provider.name}",
            summary["capped_to"],
            summary["planned"],
        )
        return inserted, summary

    # ───────────────────────── stage 2: search ───────────────────────

    def search(self, params: RunParams | None = None, stats: RunStats | None = None) -> RunStats:
        params = params or RunParams()
        stats = stats or RunStats()
        chain = self.chain()
        self._prime_credits(chain)

        max_results = params.max_results or self.settings.max_results_per_query
        limit = params.limit or params.max_queries or self.settings.max_queries_per_run
        stale_before = (
            datetime.now(UTC) - timedelta(days=self.settings.cache_ttl_days)
        ).strftime("%Y-%m-%d %H:%M:%S")

        pending = self.repos.queries.pending(limit=limit, stale_before=stale_before)
        total = len(pending)
        workers = max(1, params.concurrency or self.settings.search_concurrency)
        self.progress(
            "search", f"{total} queries to run on {workers} worker(s)", 0, total
        )
        if not pending:
            return stats

        disabled: set[str] = set()
        disabled_lock = threading.Lock()
        throttle = AdaptiveConcurrency(workers, name="search")

        def fetch(row: dict[str, Any]) -> tuple[dict[str, Any], Any, str, bool]:
            """Runs on a worker thread. Network and disk cache only — **never**
            SQLite. Every database write happens on the submitting thread, which
            is what makes `database is locked` structurally impossible here."""
            with throttle.slot():
                hits, provider_name, from_cache = self._search_one(
                    row["query_text"], chain, max_results, params, disabled,
                    disabled_lock=disabled_lock, throttle=throttle,
                )
            return row, hits, provider_name, from_cache

        done = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pitwall-search") as pool:
            futures = {pool.submit(fetch, q): q for q in pending}
            for future in as_completed(futures):
                if self.cancelled():
                    stats.notes.append("cancelled during search")
                    for pending_future in futures:
                        pending_future.cancel()
                    break
                try:
                    row, hits, provider_name, from_cache = future.result()
                except CancelledError:  # pragma: no cover - cancellation race
                    continue
                except Exception as exc:  # noqa: BLE001
                    row = futures[future]
                    log.exception("search worker raised on query %s", row.get("id"))
                    hits, provider_name, from_cache = None, f"worker: {exc}", False

                done += 1
                if hits is not None and not from_cache:
                    self._record_search_quota(chain, provider_name)
                with self.repos.writing():
                    if hits is None:
                        self.repos.queries.mark(row["id"], "failed", error=provider_name)
                        stats.queries_failed += 1
                    else:
                        n = self.repos.results.insert_hits(row["id"], provider_name, hits)
                        self.repos.queries.mark(
                            row["id"], "done", provider=provider_name,
                            results_count=len(hits), error=None,
                        )
                        stats.queries_executed += 1
                        stats.results_fetched += n
                        if from_cache:
                            stats.queries_cached += 1
                if done % 5 == 0 or done == total:
                    self.progress(
                        "search",
                        f"{done}/{total} queries · {stats.results_fetched} rows "
                        f"({stats.queries_cached} cached)",
                        done,
                        total,
                    )
        if throttle.reductions:
            stats.notes.append(
                f"search concurrency reduced {throttle.reductions}× to {throttle.limit} "
                "after rate limiting"
            )
        return stats

    def _search_one(
        self,
        query_text: str,
        chain: list[SearchProvider],
        max_results: int,
        params: RunParams,
        disabled: set[str],
        *,
        disabled_lock: threading.Lock | None = None,
        throttle: AdaptiveConcurrency | None = None,
    ) -> tuple[list[Any] | None, str, bool]:
        """Cache -> provider chain. Returns (hits|None, provider_or_error, cached).

        Called from worker threads, so it touches the network and the disk cache
        and nothing else. Quota accounting is returned to the caller rather than
        written here — see `_record_search_quota`.
        """
        last_error = "no provider available"
        for provider in chain:
            if provider.name in disabled:
                continue
            if not params.no_cache:
                cached = self.cache.get(provider.name, query_text, max_results)
                if cached is not None:
                    return cached, provider.name, True
            if params.cache_only:
                continue
            try:
                hits = provider.search(
                    query_text, max_results=max_results, region=self.settings.region
                )
            except SearchQuotaExhausted as exc:
                log.warning("%s out of quota — disabled for this run", provider.name)
                if disabled_lock is not None:
                    with disabled_lock:
                        disabled.add(provider.name)
                else:
                    disabled.add(provider.name)
                last_error = f"{provider.name}: {exc}"
                continue
            except RateLimited as exc:
                # The ceiling was wrong. Lower it for the rest of the run rather
                # than walking every remaining query into the same wall (§1.1).
                if throttle is not None:
                    throttle.reduce(f"{provider.name} rate limited")
                last_error = f"{provider.name}: {exc}"
                log.warning("provider %s rate limited: %s", provider.name, exc)
                continue
            except ProviderError as exc:
                last_error = f"{provider.name}: {exc}"
                log.warning("provider %s failed: %s", provider.name, exc)
                continue
            except Exception as exc:  # noqa: BLE001
                last_error = f"{provider.name}: {type(exc).__name__}: {exc}"
                log.warning("provider %s raised: %s", provider.name, exc)
                continue

            self.cache.put(provider.name, query_text, max_results, hits)
            return hits, provider.name, False
        if params.cache_only:
            return [], chain[0].name if chain else "cache", True
        return None, last_error, False

    def _record_search_quota(self, chain: list[SearchProvider], name: str) -> None:
        """Quota bookkeeping for one executed query — **submitting thread only**.

        A sqlite3 connection belongs to the thread that opened it, and `Repos`
        holds one. Calling this from a worker raises ProgrammingError, which the
        consume loop would then record as a failed query rather than as the bug
        it is. Keeping every write on one thread is the whole design.
        """
        with self.repos.writing():
            self.repos.quota.consume(name, 1)
            if name in {"serper", "serpapi"}:
                self.repos.quota.spend_credits(name, 1)
                remaining = self.repos.quota.credits(name)
                if remaining is not None:
                    for provider in chain:
                        if provider.name == name:
                            provider.set_quota(remaining)  # type: ignore[attr-defined]

    def _prime_credits(self, chain: list[SearchProvider]) -> None:
        for provider in chain:
            if provider.name in {"serper", "serpapi"}:
                stored = self.repos.quota.credits(provider.name)
                if stored is None:
                    stored = self.settings.serper_credits
                    self.repos.quota.set_credits(provider.name, stored)
                provider.set_quota(stored)  # type: ignore[attr-defined]

    # ───────────────── stage 3: extract & dedupe ──────────────────

    def extract(self, params: RunParams | None = None, stats: RunStats | None = None) -> RunStats:
        params = params or RunParams()
        stats = stats or RunStats()
        rows = self.repos.results.unprocessed(reprocess=params.reprocess)
        total = len(rows)
        self.progress("extract", f"{total} SERP rows to parse", 0, total)

        done_ids: list[int] = []
        touched: set[int] = set()
        for i, row in enumerate(rows, start=1):
            done_ids.append(row["id"])
            fields = parse_profile(row.get("title") or "", row.get("snippet") or "", row.get("url") or "")
            if not fields:
                continue
            stats.rows_parsed += 1
            slug = fields["linkedin_slug"]
            if self.repos.candidates.is_blocked(slug):
                stats.blocked_skipped += 1
                continue
            cid, is_new = self.repos.candidates.upsert(fields, replace=params.reprocess)
            self.repos.candidates.link_source(cid, row["id"])
            touched.add(cid)
            if is_new:
                stats.candidates_new += 1
                self.repos.crm.ensure(cid)
            if i % 50 == 0 or i == total:
                self.progress("extract", f"{i}/{total} rows · {len(touched)} people", i, total)

        for cid in touched:
            self.repos.candidates.refresh_evidence_count(cid)
        if done_ids:
            self.repos.results.mark_processed(done_ids)
        stats.candidates_touched = len(touched)
        self.progress(
            "extract",
            f"{stats.rows_parsed} profile rows -> {len(touched)} people "
            f"({stats.candidates_new} new)",
            total,
            total,
        )
        return stats

    # ───────────────── stage 4: heuristic pre-score ───────────────

    def score_heuristics(
        self, params: RunParams | None = None, stats: RunStats | None = None
    ) -> RunStats:
        params = params or RunParams()
        stats = stats or RunStats()
        threshold = (
            params.heuristic_threshold
            if params.heuristic_threshold is not None
            else self.settings.heuristic_threshold
        )
        prompt_version = params.prompt_version or self.settings.prompt_version
        companies, roles, _ = self.load_inputs(params)
        by_company = {c.id: c for c in companies if c.id}
        by_role = {r.id: r for r in roles if r.id}
        allow_recruiters = any("recruit" in r.title.lower() or "talent" in r.title.lower() for r in roles)

        pairs = self._candidate_context(params)
        total = len(pairs)
        self.progress("heuristics", f"{total} candidate×company×role pairs", 0, total)
        existing = self._existing_llm_scores(prompt_version) if params.rescore_all else {}

        for i, ctx in enumerate(pairs, start=1):
            company = by_company.get(ctx["company_id"])
            role = by_role.get(ctx["role_id"])
            result = heuristics.score_candidate(
                ctx["candidate"],
                company,
                role,
                serp_title=ctx["serp_title"],
                serp_snippet=ctx["serp_snippet"],
                markers=self.markers,
                allow_recruiters=allow_recruiters,
            )
            passed = heuristics.passes(result, threshold)

            # Re-scoring must not throw away LLM work already paid for: if a
            # verdict exists under this prompt version, re-blend against the
            # fresh heuristic score instead of resetting it to pending_llm.
            prior = existing.get(
                (ctx["candidate"]["id"], ctx["company_id"], ctx["role_id"])
            )
            if prior is not None:
                prior_verdict, prior_model, prior_provider = prior
                blended = self.final_score(result.score, prior_verdict)
                self.repos.scores.upsert(
                    Score(
                        candidate_id=ctx["candidate"]["id"],
                        company_id=ctx["company_id"],
                        role_id=ctx["role_id"],
                        heuristic_score=result.score,
                        llm_score=prior_verdict.score,
                        final_score=blended,
                        verdict=self.settings.band_for(blended),
                        is_current_employee=prior_verdict.is_current_employee,
                        company_match=prior_verdict.company_match,
                        role_match=prior_verdict.role_match,
                        seniority=prior_verdict.seniority,
                        confidence=prior_verdict.confidence,
                        reasons=[*result.reasons, *prior_verdict.reasons],
                        red_flags=prior_verdict.red_flags,
                        model=prior_model,
                        provider=prior_provider,
                        prompt_version=prompt_version,
                    )
                )
                stats.heuristic_scored += 1
                if passed:
                    stats.heuristic_passed += 1
                continue

            self.repos.scores.upsert(
                Score(
                    candidate_id=ctx["candidate"]["id"],
                    company_id=ctx["company_id"],
                    role_id=ctx["role_id"],
                    heuristic_score=result.score,
                    llm_score=None,
                    # The heuristic score IS the provisional final score until
                    # the LLM runs: Phase 6 (the read-only dashboard) lands
                    # before Phase 5 in the plan's schedule, so the table has to
                    # sort sensibly on heuristics alone. `verdict='pending_llm'`
                    # is what marks it provisional; the LLM stage overwrites
                    # both fields with the 0.3/0.7 blend.
                    final_score=result.score,
                    verdict="pending_llm" if passed else "reject",
                    reasons=result.reasons,
                    red_flags=[],
                    prompt_version=prompt_version,
                )
            )
            stats.heuristic_scored += 1
            if passed:
                stats.heuristic_passed += 1
            if i % 100 == 0 or i == total:
                self.progress(
                    "heuristics",
                    f"{i}/{total} scored · {stats.heuristic_passed} above {threshold:g}",
                    i,
                    total,
                )
        return stats

    def _existing_llm_scores(
        self, prompt_version: str
    ) -> dict[tuple[int, int | None, int | None], tuple[LLMVerdict, str | None, str | None]]:
        """LLM verdicts already stored under this prompt version, by pair key."""
        out: dict[
            tuple[int, int | None, int | None], tuple[LLMVerdict, str | None, str | None]
        ] = {}
        for row in self.conn.execute(
            "SELECT * FROM scores WHERE prompt_version = ? AND llm_score IS NOT NULL",
            (prompt_version,),
        ):
            verdict = LLMVerdict(
                candidate_id=row["candidate_id"],
                is_person_profile=True,
                is_current_employee=(
                    None if row["is_current_employee"] is None else bool(row["is_current_employee"])
                ),
                company_match=row["company_match"] or "unknown",
                role_match=row["role_match"] or "unknown",
                seniority=row["seniority"] or "unknown",
                score=row["llm_score"] or 0.0,
                confidence=row["confidence"] or 0.0,
                reasons=_json_loads(row["reasons"]),
                red_flags=_json_loads(row["red_flags"]),
            )
            out[(row["candidate_id"], row["company_id"], row["role_id"])] = (
                verdict,
                row["model"],
                # `scores.provider` is guaranteed by the v2 -> v3 migration.
                row["provider"],
            )
        return out

    def _candidate_context(self, params: RunParams) -> list[dict[str, Any]]:
        """Every (candidate, company, role) pair implied by the evidence.

        A person is judged against the company×role whose query surfaced them —
        that is the pair the LLM is asked about, and the pair the score row is
        keyed on.
        """
        sql = """
            SELECT c.id AS candidate_id, q.company_id, q.role_id,
                   sr.title AS serp_title, sr.snippet AS serp_snippet, sr.rank AS rank
              FROM candidates c
              JOIN candidate_sources cs ON cs.candidate_id = c.id
              JOIN search_results sr    ON sr.id = cs.search_result_id
              JOIN queries q            ON q.id = sr.query_id
             WHERE q.company_id IS NOT NULL AND q.role_id IS NOT NULL
        """
        params_sql: list[Any] = []
        if not params.rescore_all:
            sql += """
               AND NOT EXISTS (
                   SELECT 1 FROM scores s
                    WHERE s.candidate_id = c.id AND s.company_id = q.company_id
                      AND s.role_id = q.role_id AND s.prompt_version = ?
               )
            """
            params_sql.append(params.prompt_version or self.settings.prompt_version)
        sql += " ORDER BY c.id, q.company_id, q.role_id, sr.rank ASC"

        best: dict[tuple[int, int, int], dict[str, Any]] = {}
        for row in self.conn.execute(sql, params_sql):
            key = (row["candidate_id"], row["company_id"], row["role_id"])
            if key in best:
                continue  # best-ranked evidence for this pair
            best[key] = {
                "candidate_id": row["candidate_id"],
                "company_id": row["company_id"],
                "role_id": row["role_id"],
                "serp_title": row["serp_title"] or "",
                "serp_snippet": row["serp_snippet"] or "",
            }
        if not best:
            return []
        cand_rows = {
            r["id"]: dict(r)
            for r in self.conn.execute(
                "SELECT * FROM candidates WHERE id IN "
                f"({','.join(str(int(k[0])) for k in best)})"
            )
        }
        out = []
        for key, ctx in best.items():
            cand = cand_rows.get(key[0])
            if cand:
                out.append({**ctx, "candidate": cand})
        return out

    # ───────────────────── stage 5: LLM filter ─────────────────────

    def score_llm(self, params: RunParams | None = None, stats: RunStats | None = None) -> RunStats:
        params = params or RunParams()
        stats = stats or RunStats()
        prompt_version = params.prompt_version or self.settings.prompt_version

        if not params.use_llm or self.settings.llm_provider == "none":
            self.progress("llm", "LLM disabled for this run", 0, 0)
            return stats

        # The chain, not one provider. Everything after the first entry is the
        # degradation path (§0.4); keyless providers drop out silently.
        self._llm_remaining = self.settings.llm_chain()
        scorer = self._open_scorer(stats)
        if scorer is None:
            tried = ", ".join(self.settings.llm_chain()) or "none"
            note = f"no usable LLM provider in the chain ({tried})"
            stats.notes.append(note)
            self.progress("llm", f"skipped — {note}", 0, 0)
            return stats

        cap = self.settings.daily_llm_request_cap

        pending = self.repos.scores.awaiting_llm(prompt_version, limit=params.limit)
        groups: dict[tuple[int | None, int | None], list[dict[str, Any]]] = {}
        for row in pending:
            groups.setdefault((row["company_id"], row["role_id"]), []).append(row)

        companies, roles, _ = self.load_inputs()
        by_company = {c.id: c for c in companies if c.id}
        by_role = {r.id: r for r in roles if r.id}

        total = len(pending)
        workers = max(1, params.concurrency or self.settings.llm_concurrency)

        # Everything the model needs, read from SQLite up front on this thread,
        # so the workers below are pure network calls.
        queue: deque[_LLMJob] = deque()
        for (company_id, role_id), rows in groups.items():
            company = by_company.get(company_id) if company_id is not None else None
            role = by_role.get(role_id) if role_id is not None else None
            for batch in batched(rows, self.settings.llm_batch_size):
                queue.append(
                    _LLMJob(
                        rows=batch,
                        payload=[self._llm_payload(r) for r in batch],
                        company=company,
                        role=role,
                        company_id=company_id,
                        role_id=role_id,
                    )
                )

        done = 0
        throttle = AdaptiveConcurrency(workers, name="llm")
        self.progress(
            "llm",
            f"{total} candidates in {len(queue)} batches on {workers} worker(s) "
            f"via {scorer.provider}",
            0,
            total,
        )

        # Waves rather than a continuous stream: degradation swaps the shared
        # scorer, and doing that while other workers hold a reference to the old
        # one is the kind of race that only shows up in production. A wave
        # boundary is a point where nothing is in flight.
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pitwall-llm") as pool:
            while queue:
                if self.cancelled():
                    stats.notes.append("cancelled during LLM scoring")
                    break
                used = self.repos.quota.used_today(f"llm:{scorer.provider}")
                if used >= cap * 0.95:
                    # A per-provider ceiling, so this is a reason to move down
                    # the chain, not a reason to stop.
                    nxt = self._degrade(
                        scorer, stats, LLMUnavailable(f"daily cap reached ({used}/{cap})")
                    )
                    if nxt is None:
                        break
                    scorer = nxt
                    continue

                wave = [queue.popleft() for _ in range(min(throttle.limit, len(queue)))]
                active = scorer
                call = functools.partial(self._llm_call, active, throttle)

                requeue: list[_LLMJob] = []
                failure: Exception | None = None
                for job, outcome, err in pool.map(call, wave):
                    if err is not None or (outcome is not None and outcome.provider_error):
                        if isinstance(err, QuotaExhausted):
                            throttle.reduce(f"{active.provider} quota")
                        failure = err or LLMUnavailable(outcome.error or "the call failed")
                        requeue.append(job)
                        continue

                    with self.repos.writing():
                        self.repos.quota.consume(f"llm:{active.provider}", 1)
                        stats.llm_calls += 1
                        self._persist_verdicts(
                            job.rows, outcome, job.company_id, job.role_id,
                            prompt_version, stats,
                        )
                    done += len(job.rows)

                if requeue:
                    # Put the failed batches back at the FRONT, so nobody is
                    # skipped, and move to the next provider before retrying.
                    queue.extendleft(reversed(requeue))
                    nxt = self._degrade(scorer, stats, failure or LLMUnavailable("unknown"))
                    if nxt is None:
                        break
                    scorer = nxt

                self.progress(
                    "llm",
                    f"{done}/{total} scored · {stats.llm_calls} calls on {scorer.provider}",
                    done,
                    total,
                )

        if throttle.reductions:
            stats.notes.append(
                f"LLM concurrency reduced {throttle.reductions}× to {throttle.limit} "
                "after rate limiting"
            )
        return self._finish_llm(scorer, stats)

    @staticmethod
    def _llm_call(
        scorer: LLMScorer, throttle: AdaptiveConcurrency, job: _LLMJob
    ) -> tuple[_LLMJob, Any, Exception | None]:
        """Worker thread: one API call.

        No SQLite and no shared mutable state beyond the scorer's own HTTP
        client, which is thread-safe, and its `Usage` counters, which are
        lock-guarded. Exceptions come back as values so one bad batch cannot
        take the pool down with it.
        """
        with throttle.slot():
            try:
                return job, scorer.score_batch(job.payload, job.company, job.role), None
            except Exception as exc:  # noqa: BLE001
                return job, None, exc

    def _llm_payload(self, score_row: dict[str, Any]) -> dict[str, Any]:
        """The raw evidence the model judges — title and snippet, not our parse."""
        ev = self.conn.execute(
            """
            SELECT sr.title, sr.snippet FROM candidate_sources cs
              JOIN search_results sr ON sr.id = cs.search_result_id
             WHERE cs.candidate_id = ? ORDER BY sr.rank ASC LIMIT 1
            """,
            (score_row["candidate_id"],),
        ).fetchone()
        cand = self.conn.execute(
            "SELECT * FROM candidates WHERE id = ?", (score_row["candidate_id"],)
        ).fetchone()
        return {
            "id": score_row["candidate_id"],
            "raw_title": (ev["title"] if ev else "") or "",
            "raw_snippet": ((ev["snippet"] if ev else "") or (cand["headline"] if cand else "") or "")[:500],
            "profile_url": cand["profile_url"] if cand else "",
        }

    def _persist_verdicts(
        self,
        batch: list[dict[str, Any]],
        outcome: Any,
        company_id: int | None,
        role_id: int | None,
        prompt_version: str,
        stats: RunStats,
    ) -> None:
        by_id = {v.candidate_id: v for v in outcome.verdicts}
        for row in batch:
            cid = row["candidate_id"]
            verdict = by_id.get(cid)
            heuristic = float(row.get("heuristic_score") or 0.0)
            if verdict is None:
                self.repos.scores.upsert(
                    Score(
                        candidate_id=cid,
                        company_id=company_id,
                        role_id=role_id,
                        heuristic_score=heuristic,
                        llm_score=None,
                        final_score=heuristic,
                        verdict="needs_review",
                        reasons=_json_loads(row.get("reasons")),
                        red_flags=[outcome.error or "model omitted this candidate"],
                        model=outcome.model,
                        provider=outcome.provider,
                        prompt_version=prompt_version,
                    )
                )
                stats.llm_needs_review += 1
                continue

            final = self.final_score(heuristic, verdict)
            self.repos.scores.upsert(
                Score(
                    candidate_id=cid,
                    company_id=company_id,
                    role_id=role_id,
                    heuristic_score=heuristic,
                    llm_score=verdict.score,
                    final_score=final,
                    verdict=self.settings.band_for(final),
                    is_current_employee=verdict.is_current_employee,
                    company_match=verdict.company_match,
                    role_match=verdict.role_match,
                    seniority=verdict.seniority,
                    confidence=verdict.confidence,
                    reasons=[*_json_loads(row.get("reasons")), *verdict.reasons],
                    red_flags=verdict.red_flags,
                    model=outcome.model,
                    provider=outcome.provider,
                    prompt_version=prompt_version,
                )
            )
            stats.llm_scored += 1

    def final_score(self, heuristic: float, verdict: Any) -> float:
        """§9.2: `0.30·H + 0.70·LLM`, then the four multiplicative penalties."""
        s = self.settings
        final = s.weight_heuristic * float(heuristic) + s.weight_llm * float(verdict.score)
        if verdict.is_current_employee is False:
            final *= s.penalty_not_current_employee
        if verdict.company_match == "same_name_different_org":
            final *= s.penalty_same_name_different_org
        if verdict.role_match == "unrelated":
            final *= s.penalty_role_unrelated
        if (verdict.confidence or 0.0) < 0.5:
            final *= s.penalty_low_confidence
        if not verdict.is_person_profile:
            final = min(final, 10.0)
        return round(max(0.0, min(100.0, final)), 2)

    def _open_scorer(self, stats: RunStats) -> LLMScorer | None:
        """The first provider in the remaining chain that is actually usable.

        Three ways to be unusable, and all three are silent skips rather than
        run-ending failures:

        * **no key** — a keyless cloud provider is simply not configured, which
          is the normal state of zai and deepseek in a Groq-first setup;
        * **daily cap spent** — a per-provider guard, so a spent groq quota now
          moves to the next provider instead of ending the stage;
        * **preflight** — a missing local model, or one whose context window is
          too small for a full batch. Ollama truncates from the front, which
          would silently delete the rubric and score everyone against nothing.
        """
        cap = self.settings.daily_llm_request_cap
        while self._llm_remaining:
            name = self._llm_remaining.pop(0)
            try:
                scorer = LLMScorer(self.settings, provider=name)
                if not scorer.is_available():
                    raise LLMUnavailable(f"{name} has no API key configured")
                used = self.repos.quota.used_today(f"llm:{scorer.provider}")
                if used >= cap * 0.95:
                    raise LLMUnavailable(f"daily cap reached ({used}/{cap})")
                scorer.preflight()
            except (LLMUnavailable, ValueError) as exc:
                log.warning("LLM provider %s unusable: %s", name, exc)
                stats.notes.append(f"{name} skipped: {exc}")
                continue
            log.info("LLM provider: %s (%s)", scorer.provider, scorer.model)
            return scorer
        return None

    def _degrade(self, scorer: LLMScorer, stats: RunStats, exc: Exception) -> LLMScorer | None:
        """Walk to the next provider in the chain, mid-run, per batch.

        Returns the new scorer, or None when the chain is exhausted — in which
        case the remaining candidates stay `pending_llm` and nothing is lost.
        """
        stats.llm_tokens_in += scorer.usage.tokens_in
        stats.llm_tokens_out += scorer.usage.tokens_out
        stats.cost_estimate += scorer.estimated_cost()

        while self._llm_remaining:
            candidate = self._llm_remaining[0]
            if candidate == "ollama" and not ollama_reachable(self.settings.ollama_base_url):
                self._llm_remaining.pop(0)
                stats.notes.append("ollama unreachable — cannot degrade to it")
                continue
            nxt = self._open_scorer(stats)
            if nxt is None:
                break
            note = f"{scorer.provider} failed ({str(exc)[:120]}); degraded to {nxt.provider}"
            log.warning(note)
            stats.notes.append(note)
            self.progress("llm", f"degraded to {nxt.provider}", 0, 0)
            nxt.degraded_from = scorer.provider
            return nxt

        stats.notes.append(
            f"every LLM provider in the chain failed ({str(exc)[:160]}); remaining "
            "candidates left as pending_llm — progress saved"
        )
        self.progress("llm", "LLM chain exhausted — progress saved, nothing lost", 0, 0)
        return None

    def _finish_llm(self, scorer: LLMScorer, stats: RunStats) -> RunStats:
        """Bank the current scorer's usage. Every scorer retired by `_degrade`
        was banked on its way out, so this only ever adds the live one."""
        stats.llm_tokens_in += scorer.usage.tokens_in
        stats.llm_tokens_out += scorer.usage.tokens_out
        stats.cost_estimate += scorer.estimated_cost()
        return stats

    # ───────────────────────── full run ───────────────────────────

    @contextlib.contextmanager
    def tracked(
        self, params: RunParams, stats: RunStats, command: str = "run"
    ) -> Iterator[int]:
        """Open a `runs` row, stream progress into it, and always close it.

        The single place run bookkeeping happens. The Streamlit worker got this
        right in v1 by going through `run()`; the CLI stages did not go through
        `run()` at all, which is why the `runs` table was empty after a whole
        real session and Analytics had nothing to show (§0.5).

        Closing is unconditional: a stage that raises still leaves a terminal
        `failed` row carrying the error, not a row stuck at `running`.
        """
        run_id = self.repos.runs.start(params.as_dict(), command)
        self._run_id = run_id
        stats.run_id = run_id
        started = time.time()
        try:
            yield run_id
        except Exception as exc:  # noqa: BLE001 - a failed stage must not lose the run row
            log.exception("%s failed", command)
            self.repos.runs.finish(run_id, "failed", str(exc)[:500])
            self.progress("failed", str(exc)[:200], 0, 1)
            raise
        else:
            self.repos.runs.bump(
                run_id,
                queries_planned=stats.queries_planned,
                queries_executed=stats.queries_executed,
                results_fetched=stats.results_fetched,
                candidates_new=stats.candidates_new,
                llm_calls=stats.llm_calls,
                llm_tokens_in=stats.llm_tokens_in,
                llm_tokens_out=stats.llm_tokens_out,
                cost_estimate=stats.cost_estimate,
            )
            status = "cancelled" if self.cancelled() else "done"
            self.repos.runs.finish(run_id, status)
            self.progress(
                "done",
                f"{command} {status} in {time.time() - started:.0f}s · "
                f"{stats.candidates_new} new people · ${stats.cost_estimate:.4f}",
                1,
                1,
            )
        finally:
            self._run_id = None

    def run(self, params: RunParams | None = None) -> RunStats:
        params = params or RunParams()
        stats = RunStats()
        with self.tracked(params, stats, "run"):
            self.repos.queries.reset_running()
            _, summary = self.build_queries(params)
            stats.queries_planned = summary.get("capped_to", 0)

            self.search(params, stats)
            if not self.cancelled():
                self.extract(params, stats)
            if not self.cancelled():
                self.score_heuristics(params, stats)
            if not self.cancelled() and params.use_llm:
                self.score_llm(params, stats)
        return stats

    # ───────────────────────── previews ──────────────────────────

    def preview(self, params: RunParams | None = None) -> dict[str, Any]:
        """Cost/quota preview shown before you press Run (Phase 8).

        Ratios come from §13.1's typical run: 200 queries -> ~3,000 SERP rows
        -> ~700 candidates -> ~200 past heuristics -> 25 LLM calls.
        """
        params = params or RunParams()
        companies, roles, report = self.load_inputs(params)
        chain = self.chain()
        provider = chain[0]
        specs, summary = build_all(
            companies, roles, self.settings,
            provider=provider.name,
            supports_operators=provider.supports_operators(),
            template_ids=params.template_ids,
            max_queries=params.max_queries,
        )
        known = {
            r["query_hash"]
            for r in self.conn.execute("SELECT query_hash FROM queries WHERE status = 'done'")
        }
        fresh = [s for s in specs if s.query_hash not in known]
        max_results = params.max_results or self.settings.max_results_per_query

        n_queries = len(fresh)
        est_rows = int(n_queries * max_results * 0.75)
        est_candidates = int(est_rows * 0.23)
        est_pass = int(est_candidates * 0.28)
        est_calls = max(0, -(-est_pass // self.settings.llm_batch_size))
        prices = self.settings.llm_prices.get(self.settings.llm_provider, {"input": 0, "output": 0})
        est_cost = est_calls * (1800 * prices.get("input", 0) + 500 * prices.get("output", 0)) / 1e6
        qps = self.settings.queries_per_second(provider.name)
        return {
            "provider": provider.name,
            "supports_operators": provider.supports_operators(),
            "companies": len(companies),
            "roles": len(roles),
            "queries_total": summary["planned"],
            "queries_after_cap": summary["capped_to"],
            "queries_fresh": n_queries,
            "queries_cached": summary["capped_to"] - n_queries,
            "est_serp_rows": est_rows,
            "est_candidates": est_candidates,
            "est_after_heuristics": est_pass,
            "est_llm_calls": est_calls,
            "est_cost_usd": round(est_cost, 4),
            "est_seconds": int(n_queries / qps) if qps else 0,
            "llm_used_today": self.repos.quota.used_today(f"llm:{self.settings.llm_provider}"),
            "llm_daily_cap": self.settings.daily_llm_request_cap,
            "serper_credits": self.repos.quota.credits("serper"),
            "validation_errors": len(report.errors),
            "validation_warnings": len(report.warnings),
        }


def _json_loads(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        out = json.loads(value)
        return out if isinstance(out, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
