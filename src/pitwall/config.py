"""Configuration loading — §3.3 of PITWALL_PLAN.md.

Three layers, merged with precedence::

    CLI flags  >  .env  >  config/settings.yaml  >  code defaults

`settings.yaml` holds behaviour you tune occasionally and is committed.
`.env` holds secrets and machine-specific choices and is never committed.
CLI flags are applied by the caller via :func:`Settings.with_overrides`.

Validation happens on import, so a typo fails loudly at startup rather than
silently mid-run.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import AliasChoices, Field, field_validator, model_validator

try:
    from pydantic_settings import (
        BaseSettings,
        NoDecode,
        PydanticBaseSettingsSource,
        SettingsConfigDict,
    )
except ImportError as exc:  # pragma: no cover - depends on the interpreter
    # `NoDecode` first shipped in pydantic-settings 2.7.0. Below that this import
    # fails with a bare "cannot import name 'NoDecode'", which says nothing about
    # the actual problem: you are running the wrong interpreter. On this machine
    # the system `python3` is Anaconda, whose pydantic-settings predates it.
    import sys as _sys

    try:
        from importlib.metadata import version as _version

        _found = _version("pydantic-settings")
    except Exception:  # noqa: BLE001
        _found = "not installed"
    raise ImportError(
        f"pitwall needs pydantic-settings >= 2.7 (found {_found}) — the first "
        f"release with `NoDecode`.\n"
        f"  interpreter: {_sys.executable}\n"
        f"  fix: use the project virtualenv, `.venv/bin/python`, or run "
        f"`make venv` to create it.\n"
        f"The system python3 on macOS is often Anaconda's, which ships an older "
        f"pydantic-settings and cannot import this package."
    ) from exc

# ─────────────────────────────── paths ────────────────────────────────

#: Repository root: <root>/src/pitwall/config.py -> <root>
_PKG_ROOT = Path(__file__).resolve().parents[2]


def project_root() -> Path:
    """Repository root. `PITWALL_ROOT` wins so the app can run from anywhere."""
    env = os.getenv("PITWALL_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return _PKG_ROOT


def config_dir() -> Path:
    return project_root() / "config"


SETTINGS_YAML = "settings.yaml"

# Nested settings.yaml key  ->  flat Settings field name.
YAML_MAP: dict[str, str] = {
    "search.providers": "search_providers",
    "search.max_results_per_query": "max_results_per_query",
    "search.cache_ttl_days": "cache_ttl_days",
    "search.region": "region",
    "search.ddgs_queries_per_second": "ddgs_queries_per_second",
    "search.serper_queries_per_second": "serper_queries_per_second",
    "search.searxng_queries_per_second": "searxng_queries_per_second",
    "search.searxng_engines": "searxng_engines",
    "search.searxng_timeout_seconds": "searxng_timeout_seconds",
    "search.serper_credits": "serper_credits",
    "search.serper_reserve_credits": "serper_reserve_credits",
    "search.concurrency": "search_concurrency",
    "query_budget.max_queries_per_run": "max_queries_per_run",
    "query_budget.company_tier_weight": "company_tier_weight",
    "query_budget.role_priority_weight": "role_priority_weight",
    "scoring.heuristic_threshold": "heuristic_threshold",
    "scoring.weights.heuristic": "weight_heuristic",
    "scoring.weights.llm": "weight_llm",
    "scoring.penalties.not_current_employee": "penalty_not_current_employee",
    "scoring.penalties.same_name_different_org": "penalty_same_name_different_org",
    "scoring.penalties.role_unrelated": "penalty_role_unrelated",
    "scoring.penalties.low_confidence": "penalty_low_confidence",
    "scoring.corroboration.per_query": "corroboration_query_bonus",
    "scoring.corroboration.per_template": "corroboration_template_bonus",
    "scoring.corroboration.max": "corroboration_max",
    "scoring.bands.strong": "band_strong",
    "scoring.bands.possible": "band_possible",
    "scoring.bands.weak": "band_weak",
    "llm.batch_size": "llm_batch_size",
    "llm.concurrency": "llm_concurrency",
    "llm.triage.min_heuristic": "llm_triage_min_heuristic",
    "llm.triage.top_n": "llm_triage_top_n",
    "llm.rate_limit.retries": "llm_rate_limit_retries",
    "llm.rate_limit.max_wait_seconds": "llm_rate_limit_max_wait",
    "llm.temperature": "llm_temperature",
    "llm.max_tokens": "llm_max_tokens",
    "llm.timeout_seconds": "llm_timeout_seconds",
    "llm.prompt_version": "prompt_version",
    "llm.job_prompt_version": "job_prompt_version",
    "llm.model": "llm_model",
    "llm.reasoning_effort": "llm_reasoning_effort",
    "llm.provider_chain": "llm_provider_chain",
    "llm.daily_request_cap": "daily_llm_request_cap",
    "llm.prices": "llm_prices",
    "crm.default_followup_days": "default_followup_days",
    "crm.hide_ignored_by_default": "hide_ignored_by_default",
    "retention.purge_ignored_after_days": "retention_days",
    "jobs.sources_enabled": "jobs_sources_enabled",
    "jobs.user_agent": "jobs_user_agent",
    "jobs.crawl_delay": "jobs_crawl_delay",
    "jobs.timeout_seconds": "jobs_timeout_seconds",
    "jobs.max_detail_fetches": "jobs_max_detail_fetches",
    "jobs.close_after_days": "jobs_close_after_days",
}


def _dig(data: dict[str, Any], dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def load_yaml_settings(path: Path | None = None) -> dict[str, Any]:
    """Read settings.yaml and flatten it onto Settings field names."""
    path = path or (config_dir() / SETTINGS_YAML)
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    flat: dict[str, Any] = {}
    for dotted, field in YAML_MAP.items():
        value = _dig(raw, dotted)
        if value is not None:
            flat[field] = value
    return flat


class _YamlSource(PydanticBaseSettingsSource):
    """A pydantic-settings source backed by config/settings.yaml."""

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:  # pragma: no cover - interface
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return load_yaml_settings()


# ───────────────────────────── the model ──────────────────────────────

ProviderName = Literal["ddgs", "serper", "serpapi", "searxng"]
LLMProviderName = Literal["groq", "zai", "deepseek", "ollama", "none"]


class Settings(BaseSettings):
    """The single, validated configuration object for the whole app."""

    model_config = SettingsConfigDict(
        env_file=(_PKG_ROOT / ".env", Path.cwd() / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_default=True,
        # Fields carry an AliasChoices validation alias (SEARCH_PROVIDER_CHAIN
        # etc.), which would otherwise make the field name unusable as a
        # constructor kwarg and break `with_overrides` and every test.
        populate_by_name=True,
    )

    # ── search ──
    # NoDecode: keep pydantic-settings from JSON-decoding "ddgs,serper" from .env;
    # the before-validator below understands both the CSV and the YAML list form.
    #: The ordered fallback chain. `SEARCH_PROVIDER_CHAIN` is the v2 name;
    #: `SEARCH_PROVIDERS` still works so an existing .env keeps running.
    search_providers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["searxng", "ddgs", "serper"],
        validation_alias=AliasChoices(
            "search_provider_chain", "SEARCH_PROVIDER_CHAIN",
            "search_providers", "SEARCH_PROVIDERS",
        ),
    )
    serper_api_key: str = ""
    serpapi_api_key: str = ""
    searxng_base_url: str = "http://localhost:8888"
    #: `;`-separated engine groups, each a `,`-separated SearXNG engine list.
    #: One group is used per query, round-robin, so no single upstream engine
    #: absorbs the whole sweep and starts answering captchas. Empty = let the
    #: instance pick, which is the right setting for a SearXNG you do not own.
    searxng_engines: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["google,duckduckgo", "bing,startpage", "brave,mojeek,qwant"]
    )
    searxng_timeout_seconds: float = Field(default=30.0, ge=1)
    region: str = "uk-en"
    max_results_per_query: int = Field(default=20, ge=1, le=100)
    cache_ttl_days: int = Field(default=7, ge=0)
    ddgs_queries_per_second: float = Field(default=0.25, gt=0)
    serper_queries_per_second: float = Field(default=5.0, gt=0)
    # 1 q/s, not unbounded: the container is free, the Google/Bing sessions it
    # proxies are not. §0.1 of PITWALL_V2_IMPLEMENTATION.md.
    searxng_queries_per_second: float = Field(default=1.0, gt=0)
    serper_credits: int = Field(default=2500, ge=0)
    #: Serper's 2,500 free credits are one-time and never refill. At or below
    #: this floor the chain stops reaching for serper on its own, so the burst
    #: capacity survives a routine sweep. `--use-serper` sets it to 0 for one run.
    serper_reserve_credits: int = Field(default=500, ge=0)
    #: Thread-pool size for the search stage. Workers do network and disk-cache
    #: work only; every SQLite write happens on the thread that submitted them.
    #: The shared token bucket still applies, so this widens the pipe without
    #: raising the query rate past what the providers tolerate.
    search_concurrency: int = Field(default=4, ge=1, le=64)

    # ── LLM ──
    llm_provider: LLMProviderName = "groq"
    #: Deprecated in favour of `llm_provider_chain`; still honoured so an
    #: existing .env keeps working — `llm_chain()` folds both in.
    llm_fallback: LLMProviderName = "ollama"
    #: The ordered degradation path. v1 had `LLM_FALLBACK=none`, which meant an
    #: unreachable provider halted the pipeline mid-run. Providers without a key
    #: are skipped silently, so leaving zai/deepseek empty costs nothing, and
    #: ollama sits last on purpose: local CPU inference is the offline safety
    #: net, not the working path.
    llm_provider_chain: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["groq", "zai", "deepseek", "ollama"],
        validation_alias=AliasChoices("llm_provider_chain", "LLM_PROVIDER_CHAIN"),
    )
    #: Overrides the active provider's model when set. Groq's catalogue changes
    #: often, so the model is config, never a hardcoded constant: run
    #: `make groq-check` to list what your key can actually reach today.
    llm_model: str = ""
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    #: Verified against console.groq.com/docs/models on 29 Aug 2026: a free-plan
    #: production model, the fastest in the catalogue (~1,000 tok/s), and one of
    #: the three that support strict structured outputs. This is a classification
    #: task with all the evidence in the prompt, so the smallest capable model wins.
    #: `make groq-check` re-verifies it against the live catalogue.
    groq_model: str = "openai/gpt-oss-20b"
    #: gpt-oss and Qwen3 on Groq are reasoning models. `low` is the right setting
    #: for a rubric-following classifier: the reasoning tokens are billed against
    #: the same 8K/min free-tier budget as the answer.
    llm_reasoning_effort: Literal["none", "low", "medium", "high"] = "low"
    zai_api_key: str = ""
    zai_base_url: str = "https://api.z.ai/api/paas/v4"
    zai_model: str = "glm-4.7-flash"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-v4-flash"
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "pitwall-qwen2.5"
    llm_batch_size: int = Field(default=8, ge=1, le=32)
    #: Thread-pool size for the LLM stage. Groq's free tier allows 30 requests a
    #: minute but only 8,000 tokens a minute, and a batch of 8 costs roughly
    #: 2,500 — so in practice the token ceiling binds first and a 429 will pull
    #: this down on its own (AdaptiveConcurrency).
    llm_concurrency: int = Field(default=8, ge=1, le=64)
    #: Triage (§1.2). Unset = send every candidate that passed the heuristic
    #: gate, which is the v1 behaviour. Set, and anything below the bar keeps its
    #: heuristic score and is marked `skipped_triage` — deliberately not scored,
    #: not merely unfinished — and stays eligible for a later run.
    llm_triage_min_heuristic: float | None = Field(default=None, ge=0, le=100)
    #: Optional hard cap on LLM calls per run, highest heuristic first.
    llm_triage_top_n: int | None = Field(default=None, ge=1)
    #: How many consecutive rate-limited waves to wait out before giving up on a
    #: provider. Groq's free tier is 8,000 tokens/minute and a full sweep trips
    #: it every few batches, so degrading on the first one would abandon the
    #: fastest provider in the chain for a local CPU model.
    llm_rate_limit_retries: int = Field(default=6, ge=0, le=50)
    #: Ceiling on any single wait, so a provider asking for an hour cannot hang
    #: the run.
    llm_rate_limit_max_wait: float = Field(default=90.0, ge=1, le=3600)
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=1500, ge=256)
    # Local CPU inference is minutes per batch, not seconds. Cloud providers are
    # unaffected by a generous ceiling — they simply return sooner.
    llm_timeout_seconds: float = Field(default=900.0, ge=10)
    prompt_version: str = "v1"
    #: §2.6. Keyed separately from `prompt_version` so a change to the candidate
    #: rubric does not invalidate every job score, and vice versa.
    job_prompt_version: str = "v1"
    daily_llm_request_cap: int = Field(default=900, ge=1)
    llm_prices: Annotated[dict[str, dict[str, float]], NoDecode] = Field(
        default_factory=lambda: {
            "groq": {"input": 0.0, "output": 0.0},
            "zai": {"input": 0.0, "output": 0.0},
            "deepseek": {"input": 0.14, "output": 0.28},
            "ollama": {"input": 0.0, "output": 0.0},
        }
    )

    # ── job openings (§2) ──
    #: Which adapters run. `career_page` is off by default: highest coverage,
    #: lowest yield per unit of effort, and it is the one that fetches arbitrary
    #: HTML, so it is opt-in.
    jobs_sources_enabled: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["ats_json", "aggregator"]
    )
    #: Says honestly what this is. If a site refuses it, that is an answer, not
    #: an obstacle — see `sources/jobs/base.py`.
    jobs_user_agent: str = "PitWall/2.0 (personal job-alerting; +robots-compliant)"
    #: Floor on the gap between two requests to the same host. `Crawl-delay`
    #: wins when the site asks for more.
    jobs_crawl_delay: float = Field(default=1.0, ge=0)
    jobs_timeout_seconds: float = Field(default=20.0, ge=1)
    jobs_max_detail_fetches: int = Field(default=40, ge=0)
    #: Days a posting can go unseen before `status` becomes `closed`.
    jobs_close_after_days: int = Field(default=7, ge=1)

    # ── query budget ──
    max_queries_per_run: int = Field(default=200, ge=1)
    company_tier_weight: float = 3.0
    role_priority_weight: float = 2.0

    # ── scoring ──
    heuristic_threshold: float = Field(default=25.0, ge=0, le=100)
    weight_heuristic: float = Field(default=0.30, ge=0, le=1)
    weight_llm: float = Field(default=0.70, ge=0, le=1)
    penalty_not_current_employee: float = Field(default=0.35, ge=0, le=1)
    penalty_same_name_different_org: float = Field(default=0.10, ge=0, le=1)
    penalty_role_unrelated: float = Field(default=0.40, ge=0, le=1)
    penalty_low_confidence: float = Field(default=0.80, ge=0, le=1)
    #: §1.4 corroboration, per extra corroborating source, capped. Both zero
    #: until the re-labelled eval set says what they should be — see
    #: `scripts/eval_scoring.py corroboration`.
    corroboration_query_bonus: float = Field(default=0.0, ge=0, le=20)
    corroboration_template_bonus: float = Field(default=0.0, ge=0, le=20)
    corroboration_max: float = Field(default=12.0, ge=0, le=50)

    band_strong: float = 75.0
    band_possible: float = 55.0
    band_weak: float = 35.0

    # ── CRM / retention ──
    default_followup_days: int = Field(default=14, ge=1)
    hide_ignored_by_default: bool = True
    retention_days: int = Field(default=180, ge=1)

    # ── paths / misc ──
    db_path: Path = Path("data/pitwall.db")
    cache_dir: Path = Path("data/cache")
    log_level: str = "INFO"

    # ── validators ──

    @field_validator("search_providers", mode="before")
    @classmethod
    def _split_providers(cls, v: Any) -> Any:
        """Accept `ddgs,serper` from .env and `[ddgs, serper]` from YAML."""
        if isinstance(v, str):
            v = [p.strip() for p in v.split(",") if p.strip()]
        if isinstance(v, list):
            allowed = {"ddgs", "serper", "serpapi", "searxng"}
            bad = [p for p in v if p not in allowed]
            if bad:
                raise ValueError(
                    f"unknown search provider(s) {bad}; allowed: {sorted(allowed)}"
                )
            if not v:
                raise ValueError("SEARCH_PROVIDERS must list at least one provider")
        return v

    @field_validator("jobs_sources_enabled", mode="before")
    @classmethod
    def _split_job_sources(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = [p.strip() for p in v.split(",") if p.strip()]
        if isinstance(v, list):
            allowed = {"ats_json", "aggregator", "career_page"}
            bad = [p for p in v if p not in allowed]
            if bad:
                raise ValueError(
                    f"unknown job source kind(s) {bad}; allowed: {sorted(allowed)}"
                )
        return v

    @field_validator("llm_provider_chain", mode="before")
    @classmethod
    def _split_llm_chain(cls, v: Any) -> Any:
        """Accept `groq,zai` from .env and `[groq, zai]` from YAML."""
        if isinstance(v, str):
            v = [p.strip() for p in v.split(",") if p.strip()]
        if isinstance(v, list):
            allowed = {"groq", "zai", "deepseek", "ollama"}
            bad = [p for p in v if p not in allowed]
            if bad:
                raise ValueError(
                    f"unknown LLM provider(s) {bad}; allowed: {sorted(allowed)}"
                )
        return v

    @field_validator("searxng_engines", mode="before")
    @classmethod
    def _split_engine_groups(cls, v: Any) -> Any:
        """Accept `google,ddg;bing` from .env and a YAML list of group strings."""
        if isinstance(v, str):
            v = [g.strip() for g in v.split(";") if g.strip()]
        if isinstance(v, list):
            v = [",".join(p.strip() for p in str(g).split(",") if p.strip()) for g in v]
            v = [g for g in v if g]
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.upper()
            if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
                raise ValueError(f"LOG_LEVEL={v!r} is not a valid level")
        return v

    @field_validator("db_path", "cache_dir", mode="before")
    @classmethod
    def _expand_path(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = Path(v).expanduser()
        return v

    @model_validator(mode="after")
    def _check_coherence(self) -> Settings:
        total = self.weight_heuristic + self.weight_llm
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"scoring.weights must sum to 1.0 (got heuristic={self.weight_heuristic} "
                f"+ llm={self.weight_llm} = {total})"
            )
        if not (self.band_weak < self.band_possible < self.band_strong):
            raise ValueError(
                "scoring.bands must satisfy weak < possible < strong "
                f"(got {self.band_weak}, {self.band_possible}, {self.band_strong})"
            )
        return self

    # ── source ordering: init(CLI) > env > .env > settings.yaml > defaults ──

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlSource(settings_cls),
            file_secret_settings,
        )

    # ── derived helpers ──

    @property
    def db_file(self) -> Path:
        p = self.db_path
        return p if p.is_absolute() else project_root() / p

    @property
    def cache_path(self) -> Path:
        p = self.cache_dir
        return p if p.is_absolute() else project_root() / p

    @property
    def export_dir(self) -> Path:
        return project_root() / "data" / "exports"

    @property
    def log_file(self) -> Path:
        return project_root() / "data" / "pitwall.log"

    def queries_per_second(self, provider: str) -> float:
        return {
            "ddgs": self.ddgs_queries_per_second,
            "serper": self.serper_queries_per_second,
            "serpapi": self.serper_queries_per_second,
            "searxng": self.searxng_queries_per_second,
        }.get(provider, 1.0)

    def llm_config(self, provider: str | None = None) -> dict[str, Any]:
        """base_url / model / api_key for an OpenAI-compatible provider (§5)."""
        provider = provider or self.llm_provider
        table = {
            "groq": {
                "base_url": self.groq_base_url,
                "model": self.groq_model,
                "api_key": self.groq_api_key,
            },
            "zai": {
                "base_url": self.zai_base_url,
                "model": self.zai_model,
                "api_key": self.zai_api_key,
            },
            "deepseek": {
                "base_url": self.deepseek_base_url,
                "model": self.deepseek_model,
                "api_key": self.deepseek_api_key,
            },
            "ollama": {
                "base_url": self.ollama_base_url,
                "model": self.ollama_model,
                "api_key": "ollama",
            },
        }
        cfg = table.get(provider)
        if cfg is None:
            raise ValueError(f"unknown LLM provider {provider!r}")
        if self.llm_model:
            cfg = {**cfg, "model": self.llm_model}
        return {"provider": provider, **cfg}

    def corroboration_weights(self) -> dict[str, float]:
        return {
            "query": self.corroboration_query_bonus,
            "template": self.corroboration_template_bonus,
            "max": self.corroboration_max,
        }

    def llm_chain(self) -> list[str]:
        """The ordered providers to try, most preferred first.

        Folds the three settings into one list so that every existing .env keeps
        working: an explicit `LLM_PROVIDER` still leads, `LLM_PROVIDER_CHAIN`
        supplies the body, and a legacy `LLM_FALLBACK` is still reachable at the
        end. `none` drops out; duplicates collapse, first mention wins.
        """
        ordered = [self.llm_provider, *self.llm_provider_chain, self.llm_fallback]
        out: list[str] = []
        for name in ordered:
            if name and name != "none" and name not in out:
                out.append(name)
        return out

    def llm_has_key(self, provider: str) -> bool:
        """Ollama needs no key; every cloud provider does. A keyless provider is
        skipped silently rather than failing the run."""
        if provider == "ollama":
            return True
        try:
            return bool(self.llm_config(provider).get("api_key"))
        except ValueError:
            return False

    def usable_llm_chain(self) -> list[str]:
        return [p for p in self.llm_chain() if self.llm_has_key(p)]

    def band_for(self, score: float) -> str:
        if score >= self.band_strong:
            return "strong"
        if score >= self.band_possible:
            return "possible"
        if score >= self.band_weak:
            return "weak"
        return "reject"

    def with_overrides(self, **kwargs: Any) -> Settings:
        """Apply CLI/UI overrides — the top precedence layer."""
        clean = {k: v for k, v in kwargs.items() if v is not None}
        if not clean:
            return self
        return Settings(**{**self.model_dump(), **clean})

    def ensure_dirs(self) -> None:
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Raises on a bad config, at import time."""
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
