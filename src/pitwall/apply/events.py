"""What each stage produced, as it produces it — for a UI that shows the work.

`progress(key, message, fraction)` says *where* a run is. That was enough for a
progress bar and not for what RocketApply's Generate view promises: the posting's
required skills with their quotes, the coverage, the forbidden claims, the chosen
projects, the plan, the letter draft as it arrives, and the verdict on each
sentence. Those only existed on the result object returned at the very end.

A `StageEvent` carries a compact, JSON-safe summary of one stage's output. The
payload builders below are the only place that decides what is shown, so the
pipeline stays a pipeline: it calls `emit(stage, status, **payload(result))` and
moves on. A listener that raises is logged and ignored — watching a run must
never be able to break it.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "StageEvent", "StageListener", "STAGE_TASKS", "STATUSES",
    "analysis_payload", "match_payload", "truth_payload", "select_payload",
    "render_payload", "plan_payload", "write_payload", "package_payload",
]

#: start | done | skip | wait (rate limit) | draft (a letter draft arrived) | error
STATUSES = ("start", "done", "skip", "wait", "draft", "error")

#: Which LLM tasks run inside which stage. Every stage not listed is
#: deterministic — no model touches it. The Engine page draws this; the budget
#: uses it to say what a mode costs.
STAGE_TASKS: dict[str, tuple[str, ...]] = {
    "analyse": ("job_analysis",),
    "plan": ("narrative_plan", "company_research", "research_verify"),
    "write": ("letter_write", "letter_repair", "fact_check"),
}

#: Lists in a payload are capped: this is a summary for a screen, and the
#: complete record is `provenance.json`.
CAP = 12


@dataclass(frozen=True)
class StageEvent:
    job_id: int
    #: a `pipeline.STAGES` key, or "job" (a batch starting/finishing one job),
    #: or "research" (the batch's company research pre-pass)
    stage: str
    status: str
    message: str = ""
    #: position in the whole run, 0-1, as `progress` reports it
    fraction: float = 0.0
    #: seconds this stage took, on `done`
    seconds: float = 0.0
    #: prompt + completion tokens spent by the run so far
    tokens: int = 0
    data: Mapping[str, Any] = field(default_factory=dict)
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["data"] = dict(self.data)
        out["at"] = self.at or dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        return out


StageListener = Callable[[StageEvent], None]


def safe_call(listener: StageListener | None, event: StageEvent) -> None:
    if listener is None:
        return
    try:
        listener(event)
    except Exception:  # noqa: BLE001 - a watcher must not break the run it watches
        log.exception("a stage listener raised on %s/%s; ignored", event.stage, event.status)


def _cap(items: Any, n: int = CAP) -> list[Any]:
    return list(items or [])[:n]


# ─────────────────────────── one builder per stage ─────────────────────────


def analysis_payload(analysis: Any, *, cached: bool = False, model: str = "") -> dict[str, Any]:
    """What the model read out of the posting — each skill with the quote that
    proves it is really there (unquoted ones were already dropped)."""
    return {
        "role_family": getattr(analysis, "role_family", ""),
        "seniority": getattr(analysis, "seniority", ""),
        "language": getattr(analysis, "language", ""),
        "domain": getattr(analysis, "domain", ""),
        "required": [{"term": r.term, "quote": r.evidence_quote}
                     for r in _cap(getattr(analysis, "required_skills", ()))],
        "preferred": [{"term": r.term, "quote": r.evidence_quote}
                      for r in _cap(getattr(analysis, "preferred_skills", ()), 8)],
        "responsibilities": _cap(getattr(analysis, "responsibilities", ()), 5),
        "hard_gates": _cap(getattr(analysis, "hard_gates", ()), 6),
        "rejected": len(getattr(analysis, "rejected", ()) or ()),
        "truncated": bool(getattr(analysis, "truncated", False)),
        "cached": cached,
        "model": model,
    }


def match_payload(matched: Any) -> dict[str, Any]:
    return {
        "coverage_required": round(float(getattr(matched, "coverage_required", 0.0)), 3),
        "coverage_preferred": round(float(getattr(matched, "coverage_preferred", 0.0)), 3),
        "overall": round(float(getattr(matched, "overall", 0.0)), 3),
        "stretch": bool(getattr(matched, "is_stretch", False)),
        "matched": [{"requirement": m.requirement, "skill": m.skill_name,
                     "confidence": m.confidence} for m in _cap(getattr(matched, "matched", ()))],
        "partial": [{"requirement": p.requirement, "skill": p.skill_name}
                    for p in _cap(getattr(matched, "partial", ()), 6)],
        "missing": [{"requirement": m.requirement, "criticality": m.criticality}
                    for m in _cap(getattr(matched, "missing", ()))],
    }


def truth_payload(truth: Any) -> dict[str, Any]:
    forbidden = sorted(getattr(truth, "forbidden_claims", ()) or ())
    return {
        "forbidden_count": len(forbidden),
        "forbidden": forbidden[:15],
        "contradicted": [str(c[0]) if isinstance(c, tuple | list) else str(c)
                         for c in _cap(getattr(truth, "contradicted_gaps", ()), 5)],
        "allowed_numbers": len(getattr(truth, "allowed_numbers", ()) or ()),
    }


def select_payload(config: Any, profile: Any = None) -> dict[str, Any]:
    projects = []
    for slot in _cap(getattr(config, "projects", ())):
        project = profile.project(slot.id) if profile is not None else None
        projects.append({
            "id": slot.id,
            "name": getattr(project, "name", slot.id),
            "score": round(float(slot.score), 2),
            "bullets": len(slot.bullets),
            "why": (getattr(config, "rationale", {}) or {}).get(slot.id, ""),
        })
    return {
        "projects": projects,
        "skills": {row: list(items)[:10]
                   for row, items in (getattr(config, "skills", {}) or {}).items()},
        "experience_variants": {k: v for k, v in (
            getattr(config, "experience_variants", {}) or {}).items() if v},
        "overrides": _cap(getattr(config, "overrides", ()), 5),
    }


def render_payload(*, directory: Any, cv_pdf: Any, pages: int | None,
                   warnings: list[str], divergences: int = 0) -> dict[str, Any]:
    return {
        "directory": str(directory or ""),
        "cv_pdf": str(cv_pdf or ""),
        "pages": pages,
        "warnings": _cap(warnings, 5),
        "divergences": divergences,
    }


def plan_payload(plan: Any) -> dict[str, Any]:
    sources = getattr(plan, "company_sources", None) or []
    return {
        "main_theme": getattr(plan, "main_theme", ""),
        "secondary_theme": getattr(plan, "secondary_theme", ""),
        "evidence_order": _cap(getattr(plan, "evidence_order", ()), 8),
        "company_angle": getattr(plan, "company_angle", ""),
        "role_attraction": getattr(plan, "role_attraction", ""),
        "opening_strategy": getattr(plan, "opening_strategy", ""),
        "company_notes_source": getattr(plan, "company_notes_source", ""),
        "company_urls": [str(s.get("url") if isinstance(s, dict) else s) for s in sources][:3],
        "target_words": getattr(plan, "target_words", None),
    }


def write_payload(letter: Any, fact: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "words": int(getattr(letter, "words", 0) or 0),
        "attempts": int(getattr(letter, "attempts", 0) or 0),
        "accepted": bool(getattr(letter, "accepted", False)),
        "violations": [{"code": v.code, "detail": v.detail, "span": v.span}
                       for v in _cap(getattr(letter, "violations", ()), 6)],
        "banned": _cap(getattr(letter, "banned", ()), 6),
        "repairs": [{"code": r.code, "what": r.what}
                    for r in _cap(getattr(letter, "repairs", ()), 6)],
    }
    if fact is not None:
        verdicts = list(getattr(fact, "verdicts", ()) or ())
        failures = list(getattr(fact, "failures", ()) or ())
        out.update({
            "sentences": len(verdicts),
            "supported": len(verdicts) - len(failures),
            "independent": bool(getattr(fact, "independent", False)),
            "auditor": getattr(fact, "auditor", ""),
            "rounds": getattr(fact, "rounds", 0),
            "flagged": [{"verdict": v.verdict, "span": v.span or v.sentence[:120]}
                        for v in failures[:6]],
        })
    return out


def package_payload(result: Any) -> dict[str, Any]:
    def path(name: str) -> str:
        value = getattr(result, name, None)
        return str(value) if value else ""

    return {
        "letter_state": getattr(result, "letter_state", ""),
        "directory": path("directory"),
        "cv_pdf": path("cv_pdf"),
        "cv_docx": path("cv_docx"),
        "letter_txt": path("letter_path"),
        "letter_pdf": path("letter_pdf"),
        "letter_docx": path("letter_docx"),
        "prompt_tokens": int(getattr(result, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(result, "completion_tokens", 0) or 0),
        "needs_review": bool(getattr(result, "needs_review", False)),
        "error": getattr(result, "error", ""),
        "warnings": _cap(getattr(result, "warnings", ()), 6),
    }
