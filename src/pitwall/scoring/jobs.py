"""CV relevance scoring for job postings — §2.6.

Optional by design, and built last on purpose: §2.6 says to ship §2.5 first and
see whether it is needed, because "companies where I know people" may be signal
enough on its own. So this is off unless `config/profile.md` exists.

It reuses the existing LLM layer rather than growing a second one: the same
client, the same batching, the same repair ladder, the same provider chain and
degradation, the same strict-JSON support. Only the prompt pair and the
response model differ, which is what `LLMScorer` is subclassed for.

Verdicts are keyed by `relevance_prompt_version` on the `jobs` row, exactly as
`scores` is keyed by `prompt_version` — so a rubric change is additive and the
old numbers stay comparable instead of being overwritten.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings, config_dir
from ..models import JobRelevance, JobRelevanceBatch
from .llm import PROMPTS_DIR, STRICT_JSON_MODELS, LLMScorer, _env, repair_json
from .schema import JOB_SCHEMA_HINT, job_response_format

log = logging.getLogger(__name__)

PROFILE_FILE = "profile.md"
#: The owner's own CV-and-goals text. Gitignored, because it is a CV; the
#: tracked `profile.md` stays the template and is read only if this is absent.
PERSONAL_PROFILE_FILE = "my_profile.md"


def profile_path(settings: Settings | None = None) -> Path:
    _ = settings
    personal = config_dir() / PERSONAL_PROFILE_FILE
    return personal if personal.exists() else config_dir() / PROFILE_FILE


def load_profile(settings: Settings | None = None) -> str | None:
    """The owner's CV / target-role description, or None if they have not written one.

    Absent is the normal state, and it is what keeps this stage off by default.
    """
    path = profile_path(settings)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    # A file that is still the shipped template is not a profile.
    if not text or text.startswith("<!-- template"):
        return None
    return text


@dataclass
class RelevanceOutcome:
    verdicts: list[JobRelevance] = field(default_factory=list)
    unscored: list[int] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    error: str | None = None
    provider_error: bool = False


def parse_relevance(text: str) -> JobRelevanceBatch | None:
    data = repair_json(text)
    if data is None:
        return None
    if "results" not in data and isinstance(data.get("job_id"), int):
        data = {"results": [data]}
    try:
        return JobRelevanceBatch.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        log.debug("job relevance validation failed: %s", exc)
        return None


class JobRelevanceScorer(LLMScorer):
    """`LLMScorer` pointed at a different prompt pair and a different contract."""

    def __init__(self, settings: Settings | None = None, **kwargs: Any) -> None:
        super().__init__(settings, **kwargs)
        self.prompt_version = self.settings.job_prompt_version
        self.system_prompt = self._load_job_system_prompt(self.prompt_version)

    @staticmethod
    def _load_job_system_prompt(version: str) -> str:
        path = PROMPTS_DIR / f"job_system_{version}.txt"
        if not path.exists():
            raise FileNotFoundError(f"job system prompt not found: {path}")
        return path.read_text(encoding="utf-8").strip()

    def render(self, profile: str, jobs: list[dict[str, Any]]) -> str:
        template = _env().get_template(f"job_user_{self.prompt_version}.j2")
        return template.render(profile=profile, jobs=jobs, schema_hint=JOB_SCHEMA_HINT)

    def _complete_json(self, messages: list[dict[str, str]]) -> str:
        """Same call as the parent, with the job schema instead of the candidate one."""
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.settings.llm_temperature,
            top_p=1,
            max_tokens=self.settings.llm_max_tokens,
            response_format=job_response_format(strict=self.model in STRICT_JSON_MODELS),
            **self.call_kwargs(),
        )
        usage = getattr(resp, "usage", None)
        self.usage.add(
            int(getattr(usage, "prompt_tokens", 0) or 0),
            int(getattr(usage, "completion_tokens", 0) or 0),
        )
        choice = resp.choices[0] if resp.choices else None
        return (getattr(choice.message, "content", "") or "") if choice else ""

    def score_jobs(self, profile: str, jobs: list[dict[str, Any]]) -> RelevanceOutcome:
        """Score one batch of postings against the profile.

        Same contract as `score_batch`: a posting is never silently dropped. If
        the model omits it or answers unparseably it comes back in `unscored`
        and keeps its NULL relevance, so the next run picks it up again.
        """
        if not jobs:
            return RelevanceOutcome(model=self.model, provider=self.provider)

        ids = [int(j["id"]) for j in jobs]
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": self.render(profile, jobs)},
        ]
        try:
            text = self._complete_json(messages)
        except Exception as exc:  # noqa: BLE001 - provider SDKs raise many shapes
            log.error("job relevance call failed: %s", exc)
            return RelevanceOutcome(
                unscored=ids, model=self.model, provider=self.provider,
                error=str(exc)[:300], provider_error=True,
            )

        parsed = parse_relevance(text)
        if parsed is None:
            return RelevanceOutcome(
                unscored=ids, model=self.model, provider=self.provider,
                error="unparseable JSON",
            )

        by_id = {v.job_id: v for v in parsed.results}
        verdicts = [by_id[i] for i in ids if i in by_id]
        missing = [i for i in ids if i not in by_id]
        if missing:
            log.warning("the model omitted %d posting(s) from its reply", len(missing))
        return RelevanceOutcome(
            verdicts=verdicts, unscored=missing,
            model=self.model, provider=self.provider,
        )


def job_payload(row: dict[str, Any], description_limit: int = 3000) -> dict[str, Any]:
    """One posting, trimmed to what the model actually needs to judge it."""
    return {
        "id": int(row["id"]),
        "title": row.get("title") or "",
        "company": row.get("company_name") or "",
        "location": row.get("location") or "",
        "employment_type": row.get("employment_type") or "",
        "description": (row.get("description") or "")[:description_limit],
    }
