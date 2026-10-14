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

Most people get a TEMPLATE instead (`config/outreach_templates.yaml`): a note
for the request and a message for after they accept, worded by who the person
is. A template cannot fail and cannot invent; the drafted note is the optional
extra for the few people worth more, and one you save replaces the template.
"""

from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from typing import Any

__all__ = ["NoteDraft", "PROMPT_VERSION", "LIMIT", "draft_note", "check_note", "save_note",
           "notes_for", "note_facts", "pick_people", "run_bulk", "BulkPlan", "outreach_rows",
           "OUTREACH_COLUMNS", "load_templates", "template_texts", "short_role", "first_name",
           "company_display", "clean_title", "concerns"]

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
    from .llm import AllProvidersFailed
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
    # With the job: the employer is a legitimate name in a note to it. Without
    # it, every note to Waymo failed — "Waymo" is in a letter `style.yaml`
    # shows the writer as one to avoid, so the contamination gate flagged it.
    truth = build_truth(profile, job=application)
    banned = list(getattr(getattr(profile, "style", None), "banned_phrases", None) or [])
    facts = note_facts(profile, application)
    system = PROMPT.read_text(encoding="utf-8").replace("{limit}", str(limit))

    draft = NoteDraft()
    correction = ""
    for attempt in (1, 2):
        before = sum(router.tokens) if hasattr(router, "tokens") else 0
        try:
            reply = router.complete_json(
                "outreach_note", system,
                _user(person, application, facts, applied, hint, limit, correction),
                schema=SCHEMA, temperature=0.7 if attempt == 1 else 0.4,
            )
        except AllProvidersFailed:
            # Groq's gpt-oss now and then answers a schema call with
            # `json_validate_failed`, which the router rightly does not retry.
            # The second attempt runs cooler and usually gets through; two
            # failures in a row are real and go to the caller.
            if attempt == 2:
                raise
            continue
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


# ─────────────────────────────── templates ───────────────────────────────
#
# The first bulk run asked the model for every note. A third never came back —
# one rejected Groq reply skipped the rest of that company — and the ones that
# did leaned on search-snippet headlines ("your focus on skilled trades
# hiring"). A template says who you are, which role, and why this person, by
# the group they are in; the person's own headline never reaches the text, so a
# garbled one cannot show.


def load_templates(path: pathlib.Path | None = None) -> dict[str, Any]:
    """`config/outreach_templates.yaml`: per group a `note`, a `note_short` and a
    `message`, plus the note `limit`."""
    import yaml

    from ..config import config_dir

    if path is None:
        path = config_dir() / "outreach_templates.yaml"
        if not path.exists():  # another root (`PITWALL_ROOT`): the shipped wording
            path = pathlib.Path(__file__).resolve().parents[3] / "config" / path.name
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _nfkc(text: Any) -> str:
    """Folded and single-spaced. NFKC turns the 𝐛𝐨𝐥𝐝 letters some people put
    in their headlines back into plain ones."""
    return " ".join(unicodedata.normalize("NFKC", str(text or "")).split())


def first_name(full_name: Any) -> str:
    """"Katherine Fontana (Ware)" → "Katherine"; "F. RABHI" → "there"."""
    words = [w for w in re.sub(r"\([^)]*\)", " ", _nfkc(full_name)).split()
             if w.strip(".").casefold() not in {"dr", "mr", "mrs", "ms", "prof"}]
    first = words[0] if words else ""
    if len(first.strip(".")) <= 1:
        return "there"
    return first.capitalize() if first.isupper() else first


#: Legal and place words at the end of a registered name, never said aloud.
_LEGAL = {"pte", "ltd", "limited", "inc", "llc", "gmbh", "se", "ag", "kg", "co", "&", "sa",
          "bv", "nv", "plc", "corp", "corporation", "singapore", "international"}


def company_display(name: Any) -> str:
    """The name as a person says it: "NXP SEMICONDUCTORS SINGAPORE PTE. LTD." →
    "NXP Semiconductors", "Forto Logistics SE & Co. KG" → "Forto Logistics"."""
    text = re.sub(r"\([^)]*\)", " ", _nfkc(name))
    text = re.sub(r"\.(?:com|ai|io)\b", "", text, flags=re.I)
    words = text.replace(",", " ").split()
    while len(words) > 1 and words[-1].strip(".").casefold() in _LEGAL:
        words.pop()
    if any(len(w) > 3 for w in words) and all(w.isupper() or not w.isalpha() for w in words):
        words = [w if len(w) <= 3 else w.capitalize() for w in words]
    return " ".join(words) or _nfkc(name)


_INTERN = re.compile(r"^(?:\d{4}\s+)?(summer\s+)?intern(?:ship)?\s*[,:\-–]\s*"
                     r"(?:(?:MS/PhD|BS/MS|PhD|MS|BS|Masters?)\s*,\s*)?", re.I)


def short_role(title: Any, location: Any = "") -> tuple[str, str]:
    """A posting title as a note says it, full and short:
    "Research Engineer, Code RL (Reinforcement Learning)" →
    ("Research Engineer, Code RL role", "Research Engineer role");
    "2027 Summer Intern, MS/PhD, Machine Learning, Simulator Evaluation" →
    ("summer internship (ML, Simulator Evaluation)", "summer internship")."""
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", _nfkc(title))
    text = re.sub(r"\bMachine Learning\b", "ML", text).strip()
    intern = _INTERN.match(text)
    if intern:
        text = text[intern.end():]
    parts: list[str] = []
    for part in re.split(r"\s+[-–|]\s+|,", text):
        part = part.strip(" ,-–")
        if part and part.casefold() not in {p.casefold() for p in parts}:
            parts.append(part)
    # "… Alignment - London": the place is the posting's, not the role's.
    if len(parts) > 1 and location and parts[-1].casefold() in _nfkc(location).casefold():
        parts.pop()
    if intern:
        kind = "summer internship" if intern.group(1) else "internship"
        return (f"{kind} ({', '.join(parts)})" if parts else kind), kind
    if not parts:
        return "role", "role"
    return f"{', '.join(parts)} role", f"{parts[0]} role"


#: Which `profile/summaries.yaml` line introduces you, by the role's words.
#: First match wins; anything else is machine learning.
_FAMILIES = (
    ("quantitative", re.compile(r"quant|trading|pricing|\brisk\b", re.I)),
    ("data_science", re.compile(r"data scien|analytics|analyst|decision support", re.I)),
    ("research", re.compile(r"research", re.I)),
    ("data_engineering", re.compile(r"data engineer|\betl\b|pipeline", re.I)),
)


def about_line(profile: Any, role_title: Any) -> str:
    """One line about you for this kind of role, from your own summaries."""
    family = next((f for f, p in _FAMILIES if p.search(str(role_title or ""))),
                  "machine_learning")
    summaries = list(getattr(profile, "summaries", None) or [])
    pick = (next((s for s in summaries if (s.get("for") or [None])[0] == family), None)
            or next((s for s in summaries if family in (s.get("for") or [])), None))
    text = _nfkc((pick or {}).get("text"))
    if not text:
        return "I'm an MSc Data Science student working as a contract ML engineer."
    return f"I'm an {text}" if text.startswith("MSc") else text


def template_texts(person: dict[str, Any], company: str, applications: list[dict[str, Any]],
                   *, profile: Any = None, templates: dict[str, Any] | None = None
                   ) -> tuple[str, str]:
    """(note, message) for one person, about your best-fit role at `company`.
    The note fits the limit: the role is shortened, then `note_short` used."""
    templates = templates or load_templates()
    limit = int(templates.get("limit") or LIMIT)
    group = str(person.get("group") or "peer")
    shared = _nfkc(person.get("shared"))
    if group == "alumni" and not shared:
        group = "peer"
    wording = templates.get(group) or templates["peer"]
    best = max(applications, key=lambda a: a.get("score") or 0) if applications else {}
    full, short = short_role(best.get("title"), best.get("location"))
    titles = {_nfkc(a.get("title")).casefold() for a in applications}
    fields = {
        "first": first_name(person.get("full_name")),
        "company": company_display(company),
        "role": full,
        "also": ", along with a few related roles" if len(titles) > 1 else "",
        "about": about_line(profile, best.get("title")),
        "shared": shared,
    }
    note = ""
    for text, role in ((wording["note"], full), (wording["note"], short),
                       (wording.get("note_short") or wording["note"], short)):
        note = _nfkc(text.format_map({**fields, "role": role}))
        if len(note) <= limit:
            break
    return note, _nfkc(wording["message"].format_map(fields))


# ─────────────────────────────── who to pick ──────────────────────────────
#
# Web search is loose. Titles arrive as two people's snippets run together
# ("… - LinkedInNXP Semiconductors - LinkedInYuxi Sun - …"), a search for
# "Meetdavis" finds a Datadog sourcer, and any "manager" or "lead" counts as the
# hiring manager — an Account Manager for a data role. Each concern below is a
# flag in the CSV, and a reason to prefer someone else in the same group.

#: Debris a search snippet leaves in a title: elisions, LinkedIn chrome, labels.
_SNIPPET = re.compile(r"\.\.\.|…|LinkedIn|领英|\bView\b|\b(?:Experience|Experiência|Ervaring|"
                      r"Education|Formação|Opleiding|Location|Localidade|Locatie)\s*:", re.I)
#: A title names a job: without one of these words it is a tagline or a place.
_ROLE_WORD = re.compile(r"recruit|talent|sourc|\bhr\b|people|founder|\bceo\b|\bcto\b|chief|"
                        r"head|director|manager|lead|engineer|scientist|research|developer|"
                        r"analyst|\bdata\b|specialist|partner|architect|intern|officer|"
                        r"president|consultant", re.I)
#: A hiring manager for these roles runs something technical.
_TECH = re.compile(r"engineer|data|\bml\b|\bai\b|machine|research|scien|software|tech|system|"
                   r"founder|\bceo\b|\bcto\b|r&d|analytic|quant|platform|product|simulation|"
                   r"perception|autonom|robotic|model", re.I)


def _own_part(text: Any) -> tuple[str, bool]:
    """The part of a title or headline that is this person's own — before the
    first " · " and before any snippet debris — and whether debris was cut."""
    head = re.split(r"\s+[·|]\s+", _nfkc(text))[0]
    cut = _SNIPPET.search(head)
    if cut:
        head = re.split(r"\s+[-–]\s+", head[:cut.start()])[0]
    if head.count("(") > head.count(")"):
        head = head[:head.rfind("(")]
    return head.strip(" -–·,."), bool(cut)


def clean_title(person: dict[str, Any]) -> tuple[str, bool]:
    """(title, sure): the job part of a title or headline, and whether it came
    out whole — not cut from a run-together snippet, not a headline standing in
    for a garbled title, not shortened. ("", False) when nothing reads as a job."""
    messy = False
    for raw in (person.get("current_title"), person.get("headline")):
        head, cut = _own_part(raw)
        if len(head) >= 3 and _ROLE_WORD.search(head):
            sure = not (cut or messy or len(head) > 90)
            if len(head) > 90:
                head = head[:87].rsplit(" ", 1)[0] + "…"
            return head, sure
        messy = messy or bool(_nfkc(raw))
    return "", False


def _stated_employer(person: dict[str, Any]) -> str:
    """`current_company`, unless the parser filled it with something that is
    not an employer: a job title, a stray letter, the word LinkedIn."""
    from ..jobsearch.companies import normalize_company

    raw = _nfkc(person.get("current_company"))
    if len(normalize_company(raw)) < 3 or _ROLE_WORD.search(raw) or _SNIPPET.search(raw):
        return ""
    return raw


def concerns(person: dict[str, Any], company: str) -> tuple[list[str], int]:
    """What to check before writing to this person, and how much it should
    count against picking them (0 = nothing to check)."""
    from ..jobsearch.companies import normalize_company

    flags: list[str] = []
    weight = 0
    title, sure = clean_title(person)
    if not sure:
        flags.append("title unclear: open the profile first")
        weight += 1
    # Only what is the person's own: a headline runs on into other people's
    # snippets, and one of them naming the company proves nothing.
    token = (normalize_company(company).split() or [""])[0]
    employer = _stated_employer(person)
    said = " ".join(normalize_company(x) for x in (
        employer, _own_part(person.get("current_title"))[0],
        _own_part(person.get("headline"))[0]))
    if token and not re.search(rf"\b{re.escape(token)}\b", said):
        name = company_display(company)
        if employer:
            flags.append(f"may not work at {name}: check the profile")
            weight += 2
        else:
            flags.append(f"employer not shown: check they work at {name}")
            weight += 1
    if person.get("group") == "hiring" and not _TECH.search(title or ""):
        flags.append("check they lead a technical team")
        weight += 1
    return flags, weight


# ─────────────────────────────── in bulk ─────────────────────────────────
#
# One application at a time was the only way in: pick a row, search, find each
# email. Forty-five applications made that an afternoon. This does the same
# steps for a selection, one company at a time, and everything it finds lands
# where the one-at-a-time path puts it — the employer database — so the People
# panel shows it and a second run resumes. Notes are templates, written when
# the CSV is: nothing here spends AI tokens.


@dataclass
class BulkPlan:
    """What a bulk run does, and to whom."""

    #: `applications` rows (with `id`, `company_name`, `title`, `status`).
    applications: list[dict[str, Any]]
    #: People per COMPANY that get an email lookup and a CSV row — you connect
    #: with a person once, however many roles you applied to there.
    per_company: int = 3
    emails: bool = True
    #: Search a company again even when people were already found there.
    research: bool = False

    def companies(self) -> dict[str, list[dict[str, Any]]]:
        """Applications grouped by employer, case-insensitively, in order."""
        out: dict[str, list[dict[str, Any]]] = {}
        names: dict[str, str] = {}
        for app in self.applications:
            name = str(app.get("company_name") or "").strip()
            if name:
                out.setdefault(names.setdefault(name.casefold(), name), []).append(app)
        return out


def pick_people(found: list[dict[str, Any]], n: int, *, company: str = ""
                ) -> list[dict[str, Any]]:
    """The `n` people most worth writing to: one from each group in turn —
    recruiter, hiring manager, peer, alumni — so three picks are a recruiter, a
    manager and a peer rather than three recruiters. Within a group, people with
    fewer `concerns` at `company` come first, then the best score (`people_at`
    already sorts by group, then score, and the sort here is stable)."""
    from ..jobsearch.people import GROUPS

    def weight(person: dict[str, Any]) -> int:
        return concerns(person, company)[1] if company else 0

    queues = {g: sorted((p for p in found if p.get("group") == g), key=weight) for g in GROUPS}
    picked: list[dict[str, Any]] = []
    while len(picked) < n and any(queues.values()):
        for g in GROUPS:
            if queues[g] and len(picked) < n:
                picked.append(queues[g].pop(0))
    return picked


def run_bulk(plan: BulkPlan, conn: sqlite3.Connection, *, on_progress: Any = None,
             find: Any = None, people: Any = None, enrich_one: Any = None,
             should_stop: Any = None) -> dict[str, int]:
    """Search and look up emails for every company in `plan`.

    Never fatal per company: one employer the search engine chokes on is
    logged and skipped, and the next one runs. Already-done work is skipped —
    a company with people (unless `research`), a person with an email.
    The collaborators are injectable so the loop is testable offline.
    """
    from ..jobsearch.people import find_people, people_at

    find = find or find_people
    people = people or people_at
    enrich_one = enrich_one or _free_email
    say = on_progress or (lambda message, current=0, total=0: None)
    counts = {"companies": 0, "searched": 0, "emails": 0, "failed": 0}
    groups = plan.companies()

    for i, (company, apps) in enumerate(groups.items(), 1):
        if should_stop and should_stop():
            break
        counts["companies"] += 1
        try:
            found = people(company)
            if plan.research or not found:
                say(f"{company}: searching", i, len(groups))
                find(company, job_title=str(apps[0].get("title") or ""),
                     on_progress=lambda stage, message, *_, c=company, n=i: say(
                         f"{c}: {message}", n, len(groups)))
                counts["searched"] += 1
                found = people(company)
            say(f"{company}: {len(found)} people", i, len(groups))
            for person in pick_people(found, plan.per_company, company=company):
                if plan.emails and not person.get("email"):
                    say(f"{company}: email for {person.get('full_name')}", i, len(groups))
                    if enrich_one(int(person["id"])):
                        counts["emails"] += 1
        except Exception as exc:  # noqa: BLE001 - one employer must not stop the rest
            counts["failed"] += 1
            say(f"{company}: skipped — {exc}", i, len(groups))
    return counts


def _free_email(candidate_id: int) -> bool:
    """The People panel's "Find email": free steps only, never Hunter."""
    from .. import enrich
    from ..jobsearch.people import employer_repos, employer_settings

    ctx = enrich.enrich_candidate(candidate_id, employer_repos(), employer_settings(),
                                  steps=enrich.free_chain())
    return bool(enrich.summary(ctx).get("email"))


OUTREACH_COLUMNS = (
    "company", "name", "group", "title", "linkedin", "email", "email_status", "roles",
    "note", "note_chars", "note_source", "message", "check", "application_ids",
)


def _saved_notes(conn: sqlite3.Connection, applications: list[dict[str, Any]]
                 ) -> dict[str, dict[str, Any]]:
    """The note you saved for each person, newest first across every
    application at the company — the panel saves per application, the CSV
    writes to a person once."""
    out: dict[str, dict[str, Any]] = {}
    notes = [n for app in applications for n in notes_for(conn, int(app["id"]))
             if n.get("kind", "connection_note") == "connection_note"]
    for note in sorted(notes, key=lambda n: str(n.get("updated_at") or ""), reverse=True):
        out.setdefault(str(note["person_key"]), note)
    return out


def outreach_rows(conn: sqlite3.Connection, applications: list[dict[str, Any]], *,
                  per_company: int = 3, people: Any = None, profile: Any = None,
                  templates: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """One row per PERSON: who to write to, where, what to send with the
    request and what to send once they accept. The people are the ones
    `run_bulk` picked at each company; a person found at two companies is
    written to once. A note you saved in the People panel replaces the template.
    """
    from ..jobsearch.people import GROUP_LABELS, people_at

    people = people or people_at
    templates = templates or load_templates()
    limit = int(templates.get("limit") or LIMIT)
    if profile is None:
        try:
            from .loader import load_profile

            profile = load_profile()
        except Exception:  # noqa: BLE001 - no profile: the generic line about you
            profile = None

    rows: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for company, apps in BulkPlan(applications=applications).companies().items():
        saved = _saved_notes(conn, apps)
        roles = list(dict.fromkeys(_nfkc(a.get("title")) for a in apps if a.get("title")))
        for person in pick_people(people(company), per_company, company=company):
            key = _person_key(person)
            if key in seen:
                seen[key]["check"] = "; ".join(
                    x for x in (seen[key]["check"], f"also picked for {company}") if x)
                continue
            note, message = template_texts(person, company, apps, profile=profile,
                                           templates=templates)
            flags = concerns(person, company)[0]
            source = "template"
            mine = saved.get(key)
            if mine:
                note = str(mine.get("text") or "")
                source = "your edit" if mine.get("edited") else "AI draft"
                checks = mine.get("checks")
                flags += json.loads(checks) if isinstance(checks, str) and checks else []
            if len(note) > limit:
                flags.append(f"note too long: {len(note)} of {limit}")
            row = {
                "company": company_display(company),
                "name": person.get("full_name") or "",
                "group": GROUP_LABELS.get(person.get("group"), person.get("group") or ""),
                "title": clean_title(person)[0],
                "linkedin": person.get("profile_url") or "",
                "email": person.get("email") or "",
                "email_status": person.get("email_status") or "",
                "roles": " | ".join(roles),
                "note": note,
                "note_chars": len(note),
                "note_source": source,
                "message": message,
                "check": "; ".join(dict.fromkeys(flags)),
                "application_ids": " ".join(str(a["id"]) for a in apps),
            }
            seen[key] = row
            rows.append(row)
    return rows
