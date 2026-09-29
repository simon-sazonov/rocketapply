"""Per-task model routing over PitWall's existing provider chain.

One global chain is wrong here. Job analysis wants strict-schema JSON and real
comprehension; synonym resolution is trivial and cacheable forever; letter
writing wants the best prose available; fact checking wants a DIFFERENT MODEL
FAMILY FROM THE WRITER, because a model grading its own output is systematically
biased toward passing it.

Everything underneath — the provider table, quota degradation, the
rate-limit/quota distinction, the JSON repair ladder — is
`pitwall.scoring.llm`'s and is reused rather than reimplemented. This module
adds routing, a content-addressed cache and a trace.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..scoring.llm import (
    PROVIDERS,
    REASONING_MODELS,
    STRICT_JSON_MODELS,
    LLMUnavailable,
    classify_error,
    repair_json,
    retry_after_seconds,
)

log = logging.getLogger(__name__)

__all__ = ["TASK_ROUTING", "TASK_MODELS", "Call", "TaskRouter", "AllProvidersFailed"]

#: Task -> preferred providers, most preferred first. A provider with no key is
#: skipped silently; `deterministic_fallback` means the caller has a no-model
#: path and should take it rather than failing.
TASK_ROUTING: dict[str, tuple[str, ...]] = {
    # Strict schema JSON, needs real comprehension, one call per job.
    "job_analysis": ("groq", "ollama"),
    # Trivial and cacheable forever — never spend cloud quota on it.
    "synonym_resolve": ("ollama", "groq"),
    # Genuine judgement, but the output is a tiny JSON and the solver overrides
    # any infeasible choice, so a failure here costs nothing.
    # Groq only, to match its CAPABLE_ONLY marking. Not yet called — the
    # selector is deterministic today and the LLM advisory term is unwired.
    "slot_select": ("groq",),
    # The step that most improves letter quality.
    "narrative_plan": ("groq",),
    "letter_write": ("groq",),
    # GROQ FIRST, since 28 Sep 2026, and it is still not the writer — see
    # `TASK_MODELS`, which pins a different model family for this task. The old
    # order put local Qwen first to guarantee independence, and it did, at a
    # measured 819 seconds of the 832 a letter took: 31 sequential calls at
    # ~26s each on a 4-core 2018 CPU with no usable GPU. Worse, a 4-bit 7B
    # cannot do entailment against a 40-fact whitelist — of six sentences it
    # flagged on a real letter, four were verbatim quotes of `evidence.yaml`.
    # Ollama stays as the offline path. zai and deepseek are absent
    # deliberately — both are paid.
    "fact_check": ("groq", "ollama"),
    # Grounded extraction from text that is already in the prompt, not prose, so
    # a weak model is survivable here in a way it is not for `letter_write`: the
    # deterministic gates and the verify pass catch a bad answer at the cost of a
    # few seconds. Deliberately NOT in CAPABLE_ONLY for that reason.
    "company_research": ("groq", "ollama"),
    # Ollama FIRST, for the same reason as `fact_check`: the model that wrote a
    # sentence is biased toward passing it, and a company claim is the one class
    # of claim `check_letter` cannot judge — its first-person filter skips every
    # third-person sentence, so this is the only audit these sentences get.
    "research_verify": ("ollama", "groq"),
    # One sentence in, one sentence out, then re-gated deterministically. NOT in
    # CAPABLE_ONLY, and the reason is specific rather than optimistic: that set
    # exists because local Qwen COPIES the style exemplar out of a 5,000-token
    # letter prompt. A repair prompt contains no exemplar and no other sentence,
    # and whatever comes back has to satisfy the same regex that rejected the
    # original — so a weak answer is caught rather than shipped.
    "letter_repair": ("groq", "ollama"),
    # A LinkedIn note for RocketApply's People panel. Prose, so capable-only
    # like the letter: a small local model copies rather than writes.
    "outreach_note": ("groq",),
}

#: Groq's free tier allows 8,000 tokens a minute. The letter prompt is about
#: 5,000 tokens once the exemplars are in it, so two attempts inside a minute
#: trip the limit routinely — a PAUSE, not a failure. Waiting beats abandoning
#: the only provider that can do the job.
RATE_LIMIT_SLEEP = 8.0
RATE_LIMIT_RETRIES = 4

#: Tasks that must NOT quietly fall through to a small local model.
#:
#: Learned the hard way. When Groq rate-limited mid-run, `letter_write` fell
#: through to local Qwen2.5-7B, which does not imitate the style exemplar — it
#: COPIES it, and produced a letter opening with Simon's Alpine thesis sentence
#: verbatim, addressed to a different company. The contamination gate caught it,
#: so nothing shipped, but twenty minutes were spent producing something that
#: could never have passed. Prose generation waits for a capable model or stops.
CAPABLE_ONLY: frozenset[str] = frozenset(
    {"narrative_plan", "letter_write", "slot_select", "outreach_note"})

#: A task that must NOT run on its provider's default model, and what it runs on
#: instead. Keyed `(task, provider)`.
#:
#: INDEPENDENCE IS A PROPERTY OF THE MODEL, NOT THE PROVIDER. The writer is
#: `openai/gpt-oss-20b`; an auditor on the same key but a different family is a
#: genuine second opinion, and it answers in about a second where the local 7B
#: took twenty-six. Qwen3.8-27b is chosen because it is a different family, is
#: in `STRICT_JSON_MODELS` so the schema is enforced while decoding, and — unlike
#: the gpt-oss models — is not a reasoning model, so none of the budget is spent
#: on a trace nobody reads. Verified reachable on the free tier, 28 Sep 2026.
TASK_MODELS: dict[tuple[str, str], str] = {
    ("fact_check", "groq"): "qwen/qwen3.8-27b",
}


def schema_hint(schema: dict[str, Any]) -> str:
    """A compact restatement of the required keys, for models that cannot be
    constrained while decoding.

    Groq's gpt-oss-20b enforces the schema at the token level and simply cannot
    emit a wrong shape. Local Qwen cannot: given only `{"type":"json_object"}`
    it returns well-formed JSON with keys of its own invention — a real
    fact-check call came back `{"isAllowed": false, "reason": "..."}` instead of
    `{"verdict": ..., "unsupported_span": ...}`. The content was right and the
    shape was useless, so the caller read a missing `verdict` and defaulted to
    SUPPORTED. Naming the keys in the prompt is what stops that.
    """
    properties = schema.get("properties") or {}
    required = schema.get("required") or list(properties)
    parts = []
    for key in required:
        spec = properties.get(key) or {}
        if spec.get("enum"):
            kind = " | ".join(repr(v) for v in spec["enum"])
        elif spec.get("type") == "array":
            kind = f"[{(spec.get('items') or {}).get('type', 'string')}, …]"
        else:
            kind = str(spec.get("type", "string"))
        parts.append(f'  "{key}": {kind}')
    return (
        "Return JSON with EXACTLY these keys, and no others:\n{\n"
        + ",\n".join(parts)
        + "\n}"
    )


class AllProvidersFailed(RuntimeError):
    """Every routed provider refused. The caller degrades or stops."""


@dataclass
class Call:
    """One line of `trace.jsonl`. Without this you cannot debug a quality
    regression, and quality regressions are the failure mode you actually face."""

    task: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ms: int = 0
    attempt: int = 0
    outcome: str = "ok"
    cached: bool = False
    prompt_sha: str = ""
    #: Which key answered, as a fingerprint — "...a1b2", never the key. Empty
    #: when the provider has only one, which is the normal single-key case. With
    #: three keys in play this is what lets a letter be traced to the account
    #: that wrote it, in `trace.jsonl` and in the provenance.
    key: str = ""


@dataclass
class TaskRouter:
    """Routes one task to the first provider that answers.

    `clients` injects OpenAI-compatible stubs by provider name. The test suite
    blocks sockets, so every test path passes them.
    """

    settings: Settings = field(default_factory=get_settings)
    clients: dict[str, Any] = field(default_factory=dict)
    cache_dir: pathlib.Path | None = None
    trace_path: pathlib.Path | None = None
    calls: list[Call] = field(default_factory=list)
    #: Called as (provider, seconds, attempt) whenever a rate limit forces a
    #: wait. A silent pause of a minute is indistinguishable from a hang, and
    #: that is exactly how it was reported.
    on_wait: Any = None
    #: One `KeyPool` per provider, built on first use from `provider_keys`. With
    #: a single key this changes nothing; with several, a refusal switches key
    #: instead of sleeping.
    pools: dict[str, Any] = field(default_factory=dict)
    #: Called as (provider, key_fingerprint, kind) when a key is swapped out, so
    #: the Studio can show which key is working.
    on_key_switch: Any = None
    #: Tasks whose cached answer is NOT read (a fresh one is still written).
    #: The cache is keyed on the exact prompt, so "regenerate" with unchanged
    #: inputs used to hand back the very same letter at zero cost. RocketApply's
    #: Regenerate sets this to the letter's tasks.
    fresh_tasks: frozenset[str] = frozenset()
    #: One OpenAI client per (provider, key). Keyed on the key so three keys do
    #: not share one connection pool, and built once rather than per call.
    _clients_by_key: dict[tuple[str, str], Any] = field(default_factory=dict, repr=False)

    # ── routing ──

    def providers_for(self, task: str, exclude: set[str] | None = None) -> list[str]:
        """Routed order, filtered to providers that can actually be reached.

        INJECTED CLIENTS ARE EXHAUSTIVE. When any are supplied the router uses
        only those, never the configured chain — otherwise a test that stubs
        `groq` would fall through to a real Ollama on a machine where one
        happens to be running, and the suite makes no network call (§8).
        """
        excluded = exclude or set()
        preferred = TASK_ROUTING.get(task, ("groq",))
        chain = list(self.clients) if self.clients else self.settings.usable_llm_chain()
        out = [p for p in preferred if p in chain and p not in excluded]
        if task in CAPABLE_ONLY:
            # No generic fallback, in EITHER branch: a model that cannot do this
            # task produces something worse than nothing, and slowly. The first
            # version applied this only to the configured chain, so an injected
            # client sailed straight past it.
            return out
        # Anything reachable but unrouted is still better than failing.
        out += [p for p in chain if p not in out and p not in excluded]
        return out

    def pool_for(self, provider: str) -> Any:
        """This provider's keys, built once.

        An injected client means a test, and a test has no keys to pool.
        """
        from .keys import shared_pool

        # An explicitly supplied pool wins, which is how the tests exercise key
        # switching without a network. Otherwise an injected client means a test
        # and a test has no keys to pool.
        if provider in self.pools:
            return self.pools[provider]
        if provider in self.clients:
            return None
        # Shared across every router in this process, so eight concurrent jobs
        # have one view of what is left rather than eight.
        self.pools[provider] = shared_pool(provider, self.settings.provider_keys(provider))
        return self.pools[provider]

    def _client(self, provider: str, key: str | None = None) -> Any:
        if provider in self.clients:
            return self.clients[provider]
        from openai import OpenAI

        cfg = self.settings.llm_config(provider)
        key = key or cfg.get("api_key") or ""
        if not key and provider != "ollama":
            raise LLMUnavailable(
                f"no API key for {provider!r} — set "
                f"{PROVIDERS.get(provider, {}).get('key_env', 'the key')} in .env"
            )
        # Cached per key, not per provider: building an OpenAI client per call
        # would reopen a connection pool every time.
        cache_key = (provider, key)
        if cache_key not in self._clients_by_key:
            self._clients_by_key[cache_key] = OpenAI(
                base_url=cfg["base_url"], api_key=key or "not-needed",
                timeout=self.settings.llm_timeout_seconds,
            )
        return self._clients_by_key[cache_key]

    def _model(self, provider: str, task: str = "") -> str:
        if provider in self.clients:
            # An injected stub is the test's model; an override would make the
            # suite assert against a name no stub answers to.
            return str(self.settings.llm_config(provider).get("model") or provider)
        override = TASK_MODELS.get((task, provider))
        return override or str(self.settings.llm_config(provider)["model"])

    # ── caching ──

    def _cache_file(self, digest: str) -> pathlib.Path | None:
        return (self.cache_dir / f"{digest}.json") if self.cache_dir else None

    def _cached(self, digest: str, task: str, preferred: list[str]) -> dict[str, Any] | None:
        """A cached answer, but only if a model we would still choose produced it.

        A CACHED RESULT IS ONLY AS GOOD AS THE MODEL THAT MADE IT, and the key
        is the prompt, which cannot know that. When Groq was rate-limited,
        `letter_write` degraded to local Qwen and its output — a near-verbatim
        copy of the style exemplar — was cached under the same key. Every later
        run then "succeeded" in two seconds by serving that letter back, with
        the degradation invisible.

        So the provider is stored with the value and checked on read. For the
        tasks where model quality decides the output, only the FIRST-choice
        provider's answer is reusable.
        """
        path = self._cache_file(digest)
        if not (path and path.exists()):
            return None
        try:
            hit = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            path.unlink(missing_ok=True)
            return None
        came_from = hit.get("provider", "")
        acceptable = preferred[:1] if task in CAPABLE_ONLY else preferred
        if came_from and came_from not in acceptable:
            log.info("ignoring a %s cache entry for %s — it came from %s",
                     task, digest[:8], came_from)
            return None
        return hit

    def _store(self, digest: str, payload: dict[str, Any]) -> None:
        """Written through a temp file and renamed, because `write_text` is not
        atomic and parallel shards share this directory.

        A plain write leaves the file empty for an instant. Another worker
        reading the same digest in that instant gets a JSONDecodeError, and
        `_cached` would treat a torn read as a miss and pay for the call again —
        or worse, the half-written file stays and the entry is poisoned. The
        rename is atomic on POSIX, so a reader sees either the old file or the
        whole new one.
        """
        path = self._cache_file(digest)
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # uuid4, not the pid: the Studio writes from threads, which share a pid,
        # and a shared temp name means one thread renames the file out from under
        # another and the loser raises FileNotFoundError. A 16-thread stress test
        # produced 213 of those before this was unique per call.
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    # ── the call ──

    def complete_json(
        self,
        task: str,
        system: str,
        user: str,
        *,
        schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        exclude: set[str] | None = None,
    ) -> dict[str, Any]:
        """One structured call, routed, cached and traced.

        Raises `AllProvidersFailed` rather than returning a half-parsed answer:
        a stage that silently proceeds on a broken analysis is worse than one
        that stops, because everything downstream trusts it.
        """
        providers = self.providers_for(task, exclude)
        if not providers:
            hint = (
                " — this task needs a capable model and will not fall back to a small "
                "local one, because that produces something worse than nothing"
                if task in CAPABLE_ONLY else ""
            )
            raise AllProvidersFailed(f"no usable provider for task {task!r}{hint}")

        # Structured output is a schema, so sampling is pure downside.
        temperature = 0.0 if temperature is None else temperature
        # The MODEL is in the key, not only the provider. `_cached` validates the
        # provider, which was enough while one model answered for each; with
        # `TASK_MODELS` pinning a second model on the same provider it is not.
        digest = hashlib.sha256(
            json.dumps(
                [task, system, user, schema, temperature,
                 sorted({self._model(p, task) for p in providers})],
                sort_keys=True,
            ).encode()
        ).hexdigest()[:32]

        if task not in self.fresh_tasks and (
            hit := self._cached(digest, task, providers)
        ) is not None:
            self._trace(Call(task, hit.get("provider", "?"), hit.get("model", "?"),
                             cached=True, prompt_sha=digest))
            return hit["result"]

        errors: list[str] = []
        # Roughly what this call will cost, so a key without room for it is
        # skipped rather than asked and refused.
        estimate = len(system) // 4 + len(user) // 4 + 600
        for provider in providers:
            model = self._model(provider, task)
            pool = self.pool_for(provider)
            # With several keys the budget is attempts ACROSS keys: one key's
            # refusal is another key's turn, and switching costs nothing.
            tries = RATE_LIMIT_RETRIES + (len(pool) if pool else 0)
            for attempt in range(tries + 1):
                key = pool.choose(estimate) if pool else None
                if pool and key is None:
                    # Every key is parked. Only now is waiting the right move.
                    wait = min(pool.wait_seconds() or RATE_LIMIT_SLEEP, 60.0)
                    if attempt >= tries:
                        errors.append(f"{provider}: every key is rate-limited")
                        break
                    log.info("%s: all %d keys parked; waiting %.0fs", provider, len(pool), wait)
                    if self.on_wait is not None:
                        self.on_wait(provider, wait, attempt + 1)
                    time.sleep(wait)
                    continue
                started = time.monotonic()
                try:
                    result, usage = self._one(
                        provider, model, system, user, schema, temperature, max_tokens, key
                    )
                except LLMUnavailable as exc:
                    errors.append(f"{provider}: {exc}")
                    break
                except Exception as exc:  # noqa: BLE001 — classified, then handled
                    kind = classify_error(exc)
                    from .keys import fingerprint as _fp

                    self._trace(Call(task, provider, model, attempt=attempt,
                                     outcome=kind, prompt_sha=digest,
                                     key=_fp(key) if key else ""))
                    wait = retry_after_seconds(exc, RATE_LIMIT_SLEEP)
                    if pool and key is not None and kind in {"rate_limit", "quota"}:
                        # THE POINT OF SEVERAL KEYS. Park this one and let the
                        # next iteration pick another; with one key the router
                        # had to sleep, and a daily ceiling killed the provider
                        # outright even though another key was sitting idle.
                        from .keys import fingerprint

                        pool.note_refusal(key, kind, wait)
                        if self.on_key_switch is not None:
                            self.on_key_switch(provider, fingerprint(key), kind)
                        if attempt < tries:
                            continue
                    if kind == "rate_limit" and attempt < tries:
                        # A PAUSE, not a failure, and the distinction is the
                        # whole point: the first version classified nothing, so
                        # a routine per-minute token limit on Groq looked like a
                        # dead provider and the run silently degraded.
                        log.info("%s rate-limited on %s; waiting %.0fs", provider, task, wait)
                        if self.on_wait is not None:
                            self.on_wait(provider, wait, attempt + 1)
                        time.sleep(wait)
                        continue
                    errors.append(f"{provider}: {kind} — {type(exc).__name__}: {exc}")
                    break

                if pool and key is not None:
                    pool.record(key, usage[0] + usage[1])
                from .keys import fingerprint as _fp

                call = Call(
                    task, provider, model, usage[0], usage[1],
                    int((time.monotonic() - started) * 1000), attempt,
                    "ok" if result is not None else "unparseable", prompt_sha=digest,
                    key=_fp(key) if key else "",
                )
                self._trace(call)
                if result is not None:
                    self._store(digest, {"provider": provider, "model": model, "result": result})
                    return result
                errors.append(f"{provider}: returned no parseable JSON")
                break

        raise AllProvidersFailed(f"task {task!r} failed on every provider: {'; '.join(errors)}")

    def _one(
        self, provider: str, model: str, system: str, user: str,
        schema: dict[str, Any] | None, temperature: float, max_tokens: int | None,
        key: str | None = None,
    ) -> tuple[dict[str, Any] | None, tuple[int, int]]:
        extra: dict[str, Any] = {}
        if any(model.startswith(p) for p in REASONING_MODELS):
            # The trace is billed as output and counts against Groq's per-minute
            # ceiling; for a schema-constrained task it is pure cost.
            extra["reasoning_effort"] = "low"
            extra["extra_body"] = {"reasoning_format": "hidden"}

        if schema and model in STRICT_JSON_MODELS:
            # Constrained while decoding, so a non-conforming reply is impossible
            # and the repair ladder never has to run.
            response_format: dict[str, Any] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": schema},
            }
        else:
            response_format = {"type": "json_object"}
            if schema:
                system = f"{system}\n\n{schema_hint(schema)}"

        response = self._client(provider, key).chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=temperature,
            max_tokens=max_tokens or self.settings.llm_max_tokens,
            response_format=response_format,
            **extra,
        )
        usage = getattr(response, "usage", None)
        counts = (
            int(getattr(usage, "prompt_tokens", 0) or 0),
            int(getattr(usage, "completion_tokens", 0) or 0),
        )
        choice = response.choices[0] if response.choices else None
        content = (getattr(choice.message, "content", "") or "") if choice else ""
        try:
            parsed = json.loads(content)
            return (parsed if isinstance(parsed, dict) else {"results": parsed}), counts
        except json.JSONDecodeError:
            return repair_json(content), counts

    def _trace(self, call: Call) -> None:
        self.calls.append(call)
        if self.trace_path:
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
            with self.trace_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(call)) + "\n")

    @property
    def tokens(self) -> tuple[int, int]:
        return (
            sum(c.prompt_tokens for c in self.calls),
            sum(c.completion_tokens for c in self.calls),
        )
