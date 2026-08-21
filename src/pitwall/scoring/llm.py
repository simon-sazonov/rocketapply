"""Stage 2 scoring: the LLM filter — §9.2/§9.3 and Phase 5 of PITWALL_PLAN.md.

All three providers speak the OpenAI-compatible Chat Completions API, so one
client class handles them: only `base_url`, `api_key` and `model` change.

Robustness ladder (§9.2) — a person is **never** silently dropped because the
model misbehaved:

1. Parse JSON -> validate with pydantic.
2. Parse fails -> extract the first balanced ``{...}`` substring, retry parse.
3. Still fails -> one retry to the model: "Return ONLY the JSON object."
4. Still fails -> ``verdict='needs_review'``, keep the candidate, surface it.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import Settings, get_settings
from ..models import Company, LLMBatchResponse, LLMVerdict, Role
from .schema import SCHEMA_HINT, response_format

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"

#: base_url / model / key env per provider (§2.3).
PROVIDERS: dict[str, dict[str, Any]] = {
    "zai": {
        "base_url": "https://api.z.ai/api/paas/v4",
        "model": "glm-4.7-flash",
        "key_env": "ZAI_API_KEY",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-flash",
        "key_env": "DEEPSEEK_API_KEY",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen3:8b",
        "key_env": None,
    },
}

_QUOTA_MARKERS = (
    "429", "quota", "rate limit", "rate_limit", "too many requests",
    "insufficient", "exceeded", "billing",
)


class QuotaExhausted(RuntimeError):
    """The LLM provider refused on quota grounds — degrade or stop cleanly."""


class LLMUnavailable(RuntimeError):
    """The provider could not be reached or is not configured."""


@dataclass
class Usage:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0

    def add(self, tin: int, tout: int) -> None:
        self.calls += 1
        self.tokens_in += tin
        self.tokens_out += tout

    def cost(self, prices: dict[str, float]) -> float:
        return (
            self.tokens_in / 1_000_000 * float(prices.get("input", 0.0))
            + self.tokens_out / 1_000_000 * float(prices.get("output", 0.0))
        )


@dataclass
class BatchOutcome:
    """Result of one `score_batch` call."""

    verdicts: list[LLMVerdict] = field(default_factory=list)
    needs_review: list[int] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    raw: str = ""
    error: str | None = None


# ─────────────────────────────── prompts ────────────────────────────────


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(PROMPTS_DIR)),
        autoescape=select_autoescape(enabled_extensions=(), default=False),
        trim_blocks=False,
        lstrip_blocks=False,
    )


def load_system_prompt(version: str = "v1") -> str:
    path = PROMPTS_DIR / f"system_{version}.txt"
    if not path.exists():
        raise FileNotFoundError(f"system prompt not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def render_user_prompt(
    company: Company | None,
    role: Role | None,
    candidates: list[dict[str, Any]],
    version: str = "v1",
) -> str:
    template = _env().get_template(f"user_{version}.j2")
    return template.render(
        company=company or _blank_company(),
        role=role or _blank_role(),
        candidates=candidates,
        schema_hint=SCHEMA_HINT,
    )


def _blank_company() -> Company:
    return Company(name="(any motorsport organisation)", aliases=[], negative_keywords=[])


def _blank_role() -> Role:
    return Role(title="(any technical role)", family="data_science", aliases=[], seniority=[])


# ──────────────────────────── JSON repair ───────────────────────────────


def repair_json(text: str) -> dict[str, Any] | None:
    """Step 2 of the ladder: pull the first balanced ``{...}`` out of prose."""
    if not text:
        return None
    stripped = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.I | re.M)
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else {"results": parsed}
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    while start != -1:
        depth, in_str, escape = 0, False, False
        for i in range(start, len(stripped)):
            ch = stripped[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        out = json.loads(stripped[start : i + 1])
                        if isinstance(out, dict):
                            return out
                    except json.JSONDecodeError:
                        break
        start = stripped.find("{", start + 1)
    return None


def parse_batch(text: str) -> LLMBatchResponse | None:
    data = repair_json(text)
    if data is None:
        return None
    if "results" not in data and isinstance(data.get("candidate_id"), int):
        data = {"results": [data]}  # single-object reply
    try:
        return LLMBatchResponse.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        log.debug("batch validation failed: %s", exc)
        return None


# ─────────────────────────────── the client ──────────────────────────────


class LLMScorer:
    """OpenAI-compatible scorer with batching, repair and quota degradation."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        provider: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.provider = provider or self.settings.llm_provider
        self.cfg = self.settings.llm_config(self.provider)
        self.model = self.cfg["model"]
        self.usage = Usage()
        self.system_prompt = load_system_prompt(self.settings.prompt_version)
        self._client = client
        self.degraded_from: str | None = None

    # ── client plumbing ──

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover
                raise LLMUnavailable("the `openai` package is not installed") from exc
            key = self.cfg.get("api_key") or ""
            if not key and self.provider != "ollama":
                raise LLMUnavailable(
                    f"no API key for {self.provider!r} — set "
                    f"{PROVIDERS.get(self.provider, {}).get('key_env', 'the API key')} in .env"
                )
            self._client = OpenAI(
                base_url=self.cfg["base_url"], api_key=key or "not-needed", timeout=90.0
            )
        return self._client

    def is_available(self) -> bool:
        try:
            _ = self.client
            return True
        except LLMUnavailable:
            return False

    def switch_to(self, provider: str) -> None:
        """Mid-run degradation to the fallback provider (§5 step 6)."""
        self.degraded_from = self.provider
        self.provider = provider
        self.cfg = self.settings.llm_config(provider)
        self.model = self.cfg["model"]
        self._client = None
        log.warning("LLM degraded from %s to %s", self.degraded_from, provider)

    # ── the call ──

    def _complete(self, messages: list[dict[str, str]]) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.settings.llm_temperature,
            top_p=1,
            max_tokens=self.settings.llm_max_tokens,
            response_format=response_format(strict=False),
        )
        usage = getattr(resp, "usage", None)
        self.usage.add(
            int(getattr(usage, "prompt_tokens", 0) or 0),
            int(getattr(usage, "completion_tokens", 0) or 0),
        )
        choice = resp.choices[0] if resp.choices else None
        return (getattr(choice.message, "content", "") or "") if choice else ""

    def score_batch(
        self,
        candidates: list[dict[str, Any]],
        company: Company | None,
        role: Role | None,
    ) -> BatchOutcome:
        """Score up to `llm_batch_size` candidates sharing one company+role context.

        Batching keeps the system prompt cached (huge on DeepSeek: cache-hit
        input is $0.0028/1M vs $0.14/1M) and cuts request count 8×, which
        matters against Z.ai's ~1,000/day free ceiling.
        """
        if not candidates:
            return BatchOutcome(model=self.model, provider=self.provider)

        ids = [int(c["id"]) for c in candidates]
        user_prompt = render_user_prompt(company, role, candidates, self.settings.prompt_version)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        try:
            text = self._complete(messages)
        except Exception as exc:  # noqa: BLE001 - provider SDKs raise many shapes
            if _is_quota_error(exc):
                raise QuotaExhausted(str(exc)) from exc
            log.error("LLM call failed: %s", exc)
            return BatchOutcome(
                needs_review=ids, model=self.model, provider=self.provider, error=str(exc)[:300]
            )

        parsed = parse_batch(text)

        # ladder step 3: one corrective retry
        if parsed is None:
            log.warning("LLM reply was not valid JSON; retrying once with a correction")
            messages.append({"role": "assistant", "content": text[:2000]})
            messages.append(
                {
                    "role": "user",
                    "content": "Your previous reply was not valid JSON. Return ONLY the JSON object.",
                }
            )
            try:
                text = self._complete(messages)
            except Exception as exc:  # noqa: BLE001
                if _is_quota_error(exc):
                    raise QuotaExhausted(str(exc)) from exc
                return BatchOutcome(
                    needs_review=ids, model=self.model, provider=self.provider, error=str(exc)[:300]
                )
            parsed = parse_batch(text)

        # ladder step 4: keep everyone, mark for review
        if parsed is None:
            log.error("LLM reply still unparseable after retry — marking batch needs_review")
            return BatchOutcome(
                needs_review=ids,
                model=self.model,
                provider=self.provider,
                raw=text[:2000],
                error="unparseable JSON after one retry",
            )

        by_id = {v.candidate_id: v for v in parsed.results}
        verdicts, missing = [], []
        for cid in ids:
            v = by_id.get(cid)
            if v is None:
                missing.append(cid)
            else:
                verdicts.append(v)
        if missing:
            log.warning("LLM omitted %d candidate(s) from its reply", len(missing))
        return BatchOutcome(
            verdicts=verdicts,
            needs_review=missing,
            model=self.model,
            provider=self.provider,
            raw=text[:2000],
        )

    def estimated_cost(self) -> float:
        prices = self.settings.llm_prices.get(self.provider, {"input": 0.0, "output": 0.0})
        return self.usage.cost(prices)


def _is_quota_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if status in (429, 402):
        return True
    return any(m in text for m in _QUOTA_MARKERS)


def batched(items: list[Any], size: int) -> list[list[Any]]:
    size = max(1, int(size))
    return [items[i : i + size] for i in range(0, len(items), size)]


def ollama_reachable(base_url: str, timeout: float = 2.0) -> bool:
    """Cheap health check used before degrading to the local fallback."""
    import httpx

    root = base_url.rstrip("/").removesuffix("/v1")
    try:
        return httpx.get(f"{root}/api/tags", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False
