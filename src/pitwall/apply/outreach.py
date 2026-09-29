"""A short LinkedIn note to someone at a company you applied to. Drafts only.

Nothing in this codebase sends a message, and nothing here is built to: a note
is text on a screen with a copy button. The person sending it is the owner.

The note is held to the letter's standards, because it is the same claim in a
smaller space: the facts it may use are the ones the application was built on
(`provenance.narrative_plan.evidence_order`), and the same deterministic gates
run on every draft — the truth layer, the banned phrases — plus the three a
note adds: LinkedIn's character limit, no contact details (they belong in the
application, not a public note), and no "I applied" before the job is marked
applied. A draft that fails is retried once with the rule named; if it still
fails, it comes back with its failures listed rather than being hidden.

The prompt says only what to write. Quoting a bad phrase in order to forbid it
makes a model use it — the gates catch those, not the prompt.
"""

from __future__ import annotations

import json
import pathlib
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

__all__ = ["NoteDraft", "PROMPT_VERSION", "LIMIT", "draft_note", "check_note", "save_note",
           "notes_for", "note_facts"]

PROMPT_VERSION = "n1"
#: LinkedIn's connection-note limit on a free account.
LIMIT = 200
PROMPT = pathlib.Path(__file__).with_name("prompts") / "10_outreach_note.md"

SCHEMA = {
    "type": "object",
    "properties": {"variants": {"type": "array", "items": {
        "type": "object", "properties": {"text": {"type": "string"}},
        "required": ["text"], "additionalProperties": False}}},
    "required": ["variants"],
    "additionalProperties": False,
}

_CONTACT = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.]+|https?://|www\.|linkedin\.com|\+?\d[\d\s().-]{7,}\d", re.I)
_APPLIED = re.compile(r"\bI(?:'ve|\s+have)?\s+(?:just\s+|recently\s+)?applied\b", re.I)
SUBMITTED = {"APPLIED", "ONLINE_ASSESSMENT", "INTERVIEW", "FINAL_INTERVIEW", "OFFER",
             "REJECTED", "WITHDRAWN"}


@dataclass
class NoteDraft:
    variants: list[str] = field(default_factory=list)
    #: One list per variant: the gates it still fails. Empty = clean.
    checks: list[list[str]] = field(default_factory=list)
    model: str = ""
    tokens: int = 0
    attempts: int = 0
    prompt_version: str = PROMPT_VERSION


def check_note(text: str, *, truth: Any = None, banned: list[str] | tuple[str, ...] = (),
               applied: bool = False, limit: int = LIMIT) -> list[str]:
    """Every gate a draft fails, in words a person can act on. Empty = clean."""
    from .letter import banned_phrase_hits

    failures = []
    if len(text) > limit:
        failures.append(f"too long: {len(text)} of {limit} characters")
    if not text.strip():
        failures.append("empty")
    if _CONTACT.search(text):
        failures.append("contains contact details or a link")
    if not applied and _APPLIED.search(text):
        failures.append("says you applied, but the application is not marked applied")
    failures += [f"banned phrase: {p}" for p in banned_phrase_hits(text, list(banned))]
    if truth is not None:
        failures += [f"{v.code.lower()}: {v.span or v.detail}" for v in truth.check(text)]
    return failures


def note_facts(profile: Any, application: dict[str, Any], *, limit: int = 4) -> list[str]:
    """The candidate facts a note may use: the evidence the application's letter
    was planned on, else the profile's strongest evidence."""
    order: list[str] = []
    folder = application.get("generation_dir")
    if folder:
        try:
            data = json.loads((pathlib.Path(folder) / "provenance.json").read_text("utf-8"))
            order = list((data.get("narrative_plan") or {}).get("evidence_order") or [])
        except (OSError, json.JSONDecodeError):
            order = []
    blocks = list(getattr(profile, "evidence", None) or [])
    by_id = {b.id: b for b in blocks}
    chosen = [by_id[i] for i in order if i in by_id]
    if not chosen:
        chosen = sorted(blocks, key=lambda b: -b.strength_weight)
    facts = []
    for block in chosen[:limit]:
        line = block.one_line_hook or (block.facts[0] if block.facts else "")
        if line:
            facts.append(line.strip())
    return facts


def _user(person: dict[str, Any], application: dict[str, Any], facts: list[str],
          applied: bool, hint: str, limit: int, correction: str) -> str:
    lines = [
        f"person: {person.get('full_name') or 'unknown'}",
        f"their headline: {person.get('headline') or person.get('current_title') or 'unknown'}",
        f"who they are to the candidate: {person.get('group') or 'hiring'}",
        f"company: {application.get('company_name') or ''}",
        f"role the candidate is applying for: {application.get('title') or ''}",
        f"applied: {'yes' if applied else 'no'}",
        f"limit: {limit} characters",
        "facts about the candidate (use one, in substance):",
        *[f"- {f}" for f in facts],
    ]
    if person.get("shared"):
        lines.append(f"what you share: {person['shared']}")
    if hint:
        lines.append(f"the candidate asks: {hint}")
    if correction:
        lines.append(f"\nThe previous drafts need this: {correction}")
    return "\n".join(lines)


def draft_note(*, person: dict[str, Any], application: dict[str, Any], router: Any = None,
               profile: Any = None, limit: int = LIMIT, hint: str = "", fresh: bool = False
               ) -> NoteDraft:
    """Two variants of a connection note, gated. About 1.5k Groq tokens."""
    from .loader import load_profile
    from .truth import build_truth

    profile = profile or load_profile()
    if router is None:
        from ..config import get_settings
        from .llm import TaskRouter

        settings = get_settings()
        router = TaskRouter(settings=settings, cache_dir=pathlib.Path(settings.cache_dir) / "apply")
    if fresh:
        router.fresh_tasks = frozenset(router.fresh_tasks | {"outreach_note"})
    applied = str(application.get("status") or "") in SUBMITTED
    truth = build_truth(profile)
    banned = list(getattr(getattr(profile, "style", None), "banned_phrases", None) or [])
    facts = note_facts(profile, application)
    system = PROMPT.read_text(encoding="utf-8").replace("{limit}", str(limit))

    draft = NoteDraft()
    correction = ""
    for attempt in (1, 2):
        before = sum(router.tokens) if hasattr(router, "tokens") else 0
        reply = router.complete_json(
            "outreach_note", system,
            _user(person, application, facts, applied, hint, limit, correction),
            schema=SCHEMA, temperature=0.7 if attempt == 1 else 0.4,
        )
        draft.tokens += (sum(router.tokens) - before) if hasattr(router, "tokens") else 0
        draft.attempts = attempt
        texts = [str(v.get("text") or "").strip().strip('"')
                 for v in (reply.get("variants") or []) if isinstance(v, dict)][:2]
        checks = [check_note(t, truth=truth, banned=banned, applied=applied, limit=limit)
                  for t in texts]
        draft.variants, draft.checks = texts, checks
        calls = getattr(router, "calls", None) or []
        if calls:
            draft.model = f"{calls[-1].provider}/{calls[-1].model}"
        if texts and not any(checks):
            break
        failed = sorted({c.split(":")[0] for cs in checks for c in cs}) or ["two variants"]
        correction = (f"each note at most {limit} characters, only facts from the list, "
                      f"no contact details, 'applied' only if applied: yes — fix: "
                      + ", ".join(failed))
    return draft


# ─────────────────────────────── storage ─────────────────────────────────


def _person_key(person: dict[str, Any]) -> str:
    return str(person.get("profile_url") or person.get("full_name") or "").strip().lower()


def save_note(conn: sqlite3.Connection, application_id: int, person: dict[str, Any],
              text: str, *, kind: str = "connection_note", edited: bool = False,
              checks: list[str] | None = None, model: str = "", tokens: int = 0) -> int:
    """Keep one note per (application, person, kind); saving again replaces it."""
    key = _person_key(person)
    if not key:
        raise ValueError("a note needs a person")
    conn.execute(
        """
        INSERT INTO outreach_notes (application_id, person_key, person_name, person_headline,
            profile_url, kind, text, edited, checks, model, prompt_version, tokens)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(application_id, person_key, kind) DO UPDATE SET
            text = excluded.text, edited = excluded.edited, checks = excluded.checks,
            model = COALESCE(NULLIF(excluded.model, ''), outreach_notes.model),
            tokens = outreach_notes.tokens + excluded.tokens, updated_at = datetime('now')
        """,
        (application_id, key, person.get("full_name"), person.get("headline"),
         person.get("profile_url"), kind, text, int(edited),
         json.dumps(checks or []), model, PROMPT_VERSION, int(tokens)),
    )
    row = conn.execute(
        "SELECT id FROM outreach_notes WHERE application_id = ? AND person_key = ? AND kind = ?",
        (application_id, key, kind),
    ).fetchone()
    return int(row[0])


def notes_for(conn: sqlite3.Connection, application_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM outreach_notes WHERE application_id = ? ORDER BY updated_at DESC",
        (application_id,),
    )]
