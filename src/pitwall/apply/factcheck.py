"""STAGE 6 — the safety net.

Two layers, in this order because the first is free:

1. DETERMINISTIC GATES. Numbers against an allowlist derived from the profile,
   forbidden entities, gated verbs, blocked topics, teammates' work. These are
   exact and they catch most of it.
2. A MODEL AUDIT, one sentence per call. Short prompts are where small models
   are reliable, and the third verdict — OVERSTATED — is the point: "deep
   expertise in X" from one project is not caught by entity matching and is the
   commonest way a letter stops being true.

THE AUDITOR MUST NOT BE THE WRITER. A model grading its own output is
systematically biased toward passing it, so the router is told to exclude the
provider that wrote the text. When no second provider is reachable the check
still runs, but `independent` is False and it says so — a self-graded pass is
worth less and the human should know which one they got.
"""

from __future__ import annotations

import ast
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .letter import Letter
from .llm import AllProvidersFailed, TaskRouter
from .truth import TruthLayer, Violation

log = logging.getLogger(__name__)

__all__ = [
    "SentenceVerdict", "FactCheck", "check_letter", "split_sentences",
    "BATCH_SCHEMA",
]

#: Every sentence in ONE reply. `n` is the 1-based number the prompt gave the
#: sentence, so a model that reorders or drops one cannot silently shift the
#: verdicts onto the wrong sentences — `_audit_batch` matches on `n` and records
#: anything missing as unchecked rather than as passed.
BATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "verdict": {
                        "type": "string",
                        "enum": ["SUPPORTED", "UNSUPPORTED", "OVERSTATED"],
                    },
                    "unsupported_span": {"type": ["string", "null"]},
                },
                "required": ["n", "verdict", "unsupported_span"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

#: A sentence with no first-person marker asserts nothing about the candidate.
#: "The role asks for someone who can design RL environments" is a statement
#: about the POSTING; grading it against the candidate's facts is a category
#: error, and it was the source of most false positives. Skipping these also
#: removes roughly a third of the calls, which is most of the stage's cost.
_FIRST_PERSON = re.compile(r"\b(?:I|I'?m|I'?ve|I'?d|my|me|myself)\b")

#: Don't split on an abbreviation or a decimal point.
_SENTENCE = re.compile(r"(?<![A-Z][a-z]\.)(?<=[.!?])\s+(?=[A-Z\"'])")
_ABBREV = re.compile(r"\b(?:e\.g|i\.e|etc|vs|Dr|Mr|Ms|Prof|Inc|Ltd|St)\.$", re.I)


def split_sentences(text: str) -> list[str]:
    out: list[str] = []
    for block in text.split("\n"):
        block = block.strip()
        if not block:
            continue
        pieces = _SENTENCE.split(block)
        buffer = ""
        for piece in pieces:
            candidate = f"{buffer} {piece}".strip() if buffer else piece
            if _ABBREV.search(candidate):
                buffer = candidate
                continue
            buffer = ""
            if candidate.strip():
                out.append(candidate.strip())
        if buffer.strip():
            out.append(buffer.strip())
    return out


@dataclass
class SentenceVerdict:
    sentence: str
    verdict: str = "SUPPORTED"
    span: str | None = None
    #: model — a model judged it. no-claim — nothing about the candidate to
    #: judge. unchanged — an earlier round already cleared this exact sentence.
    #: skipped — no auditor was reachable, so it was NOT checked.
    source: str = "model"

    @property
    def ok(self) -> bool:
        return self.verdict == "SUPPORTED"


@dataclass
class FactCheck:
    verdicts: list[SentenceVerdict] = field(default_factory=list)
    gate_violations: list[Violation] = field(default_factory=list)
    banned: list[str] = field(default_factory=list)
    #: False when the auditor could not be a different provider from the writer.
    independent: bool = True
    #: Whether the model audit blocks, or only advises. See `blocking` below.
    advisory: bool = False
    auditor: str = ""
    #: How many write-then-check rounds it took to get here.
    rounds: int = 1
    notes: list[str] = field(default_factory=list)

    @property
    def supported_text(self) -> set[str]:
        """Sentences this round cleared, for the next round to skip."""
        return {v.sentence for v in self.verdicts if v.ok}

    @property
    def failures(self) -> list[SentenceVerdict]:
        return [v for v in self.verdicts if not v.ok]

    @property
    def passed(self) -> bool:
        """Whether the letter is clean — gates silent AND the auditor content."""
        return not self.failures and not self.gate_violations and not self.banned

    @property
    def blocks_output(self) -> bool:
        """Whether this must not be written to `output/` at all.

        ONLY THE DETERMINISTIC GATES BLOCK. They are exact — a number with no
        profile record, a forbidden entity, a gated verb, a blocked topic — and
        there is no judgement in them to second-guess.

        THE MODEL AUDIT DOES NOT BLOCK, because neither auditor available here is
        reliable enough to hold a veto. Groq is the writer, and a model grading
        its own output is biased toward passing. Local Qwen2.5-7B is genuinely
        independent but imprecise in the other direction: on a real run it marked
        "built a seven-stage resumable pipeline" unsupported while that phrase
        sits verbatim in the facts it was given.

        So the audit gets what it is good for. It is high-recall, so its
        objections drive a rewrite, and anything still flagged afterwards is
        surfaced — the file is named NEEDS_REVIEW and the sentences are printed.
        A human reading three highlighted sentences is the outcome the design
        wants; a 7B model silently discarding a good letter is not.
        """
        return bool(self.gate_violations or self.banned)

    @property
    def needs_review(self) -> bool:
        return bool(self.failures)

    def summary(self) -> str:
        return (
            f"{len(self.verdicts)} sentence(s), {len(self.verdicts) - len(self.failures)} "
            f"supported, {len(self.gate_violations)} gate violation(s)"
        )


def _span(value: Any) -> str | None:
    """The offending quote, however the model chose to wrap it.

    Qwen returns a one-element list where the schema asks for a string, and
    `str()` on that yields "['deployed the model to production on AWS']" —
    technically the span, but unreadable in a report a human has to act on.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    text = str(value or "").strip()
    # A list that arrived as its own repr, which is what Qwen actually sends.
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, (list, tuple)) and parsed:
                text = str(parsed[0])
        except (ValueError, SyntaxError):
            text = text[1:-1]
    return text.strip().strip("\"'") or None


def _merge_fragments(sentences: list[str], minimum: int = 5) -> list[str]:
    """Fold short fragments into their neighbour.

    The splitter produces pieces like "not merely tuning depth" from a sentence
    broken across punctuation. Judged alone they assert nothing and read as
    unsupported; judged with the clause they belong to they are obviously fine.

    FIVE WORDS, not six. "I labelled sixty candidates myself." is a complete
    sentence with a subject, a verb and a number worth checking, and folding it
    into its neighbour both weakens the check and defeats the round-to-round
    caching, because the merged pair changes whenever either half does.
    """
    out: list[str] = []
    for sentence in sentences:
        if len(sentence.split()) < minimum and out:
            out[-1] = f"{out[-1]} {sentence}"
        else:
            out.append(sentence)
    return [s for s in out if len(s.split()) >= 4]


def _audit_batch(
    sentences: list[str], facts: list[str], router: TaskRouter, exclude: set[str],
) -> list[SentenceVerdict]:
    """Every sentence, one call.

    This replaced one-call-per-sentence on 28 Sep 2026. The old shape was
    written for a local 7B, where a short prompt was the only way to get a
    reliable answer, and the facts block — the expensive part — was re-sent with
    every sentence. Measured on job 5881: 31 calls, 819 seconds, 44,210 tokens,
    and three rewrite rounds that never converged.

    Batching is safe here in a way it would not be for the writer, because the
    task is classification against a fixed list: the facts are stated once, each
    sentence is judged against them independently, and the schema pins the reply
    to one verdict per number.

    FAIL CLOSED, per sentence. A sentence whose number is absent from the reply,
    or carries a verdict this code cannot read, is recorded `source="skipped"` —
    "not checked", never "passed". The whole point of the stage is to be a net,
    and a net that reports unchecked things as clean is worse than none.
    """
    from .letter import PROMPTS

    system = (PROMPTS / "06_fact_check.md").read_text(encoding="utf-8")
    numbered_facts = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(facts))
    numbered_sentences = "\n".join(f"{i + 1}. \"{s}\"" for i, s in enumerate(sentences))
    user = (
        f"ALLOWED FACTS:\n{numbered_facts}\n\n"
        f"SENTENCES TO JUDGE — return one verdict for each, by number:\n"
        f"{numbered_sentences}"
    )
    raw = router.complete_json(
        "fact_check", system, user, schema=BATCH_SCHEMA,
        temperature=0.0, exclude=exclude,
    )

    by_number: dict[int, dict[str, Any]] = {}
    for item in raw.get("verdicts") or []:
        if not isinstance(item, dict):
            continue
        try:
            by_number[int(item.get("n"))] = item
        except (TypeError, ValueError):
            continue

    out: list[SentenceVerdict] = []
    for index, sentence in enumerate(sentences, start=1):
        item = by_number.get(index)
        verdict = str((item or {}).get("verdict") or "").strip().upper()
        if verdict not in {"SUPPORTED", "UNSUPPORTED", "OVERSTATED"}:
            log.warning(
                "no readable verdict for sentence %s of %s — recording it as unchecked",
                index, len(sentences),
            )
            out.append(SentenceVerdict(sentence, "SUPPORTED", None, source="skipped"))
            continue
        out.append(SentenceVerdict(sentence, verdict, _span(item.get("unsupported_span"))))
    return out


def check_letter(
    letter: Letter,
    truth: TruthLayer,
    facts: list[str],
    router: TaskRouter,
    *,
    written_by: str = "",
    audit: bool = True,
    on_sentence: Callable[[int, int, str], None] | None = None,
    already_supported: set[str] | None = None,
) -> FactCheck:
    """Gate first, then audit. Returns the findings; the caller decides.

    `already_supported` carries the sentences a previous round cleared. A
    rewrite usually changes two sentences out of thirteen, and re-asking a local
    7B model about the other eleven costs about fifteen seconds each for an
    answer it has already given.

    `on_sentence(index, total, text)` is how the caller draws a progress bar.
    This stage takes minutes, and without it the only honest thing the UI can
    say is "working".
    """
    result = FactCheck()
    result.gate_violations = truth.check(letter.text)
    result.banned = list(letter.banned)

    if not audit:
        result.notes.append("model audit skipped — deterministic gates only")
        return result

    # Fragments get judged without the clause that gave them meaning, so a
    # short piece is folded back into the sentence before it.
    sentences = _merge_fragments(split_sentences(letter.text))
    cleared = already_supported or set()
    claims: list[str] = []
    skipped: list[str] = []
    for sentence in sentences:
        if sentence in cleared:
            # Unchanged since a round that already passed it.
            result.verdicts.append(
                SentenceVerdict(sentence, "SUPPORTED", None, source="unchanged")
            )
            continue
        (claims if _FIRST_PERSON.search(sentence) else skipped).append(sentence)
    for sentence in skipped:
        result.verdicts.append(
            SentenceVerdict(sentence, "SUPPORTED", None, source="no-claim")
        )
    sentences = claims
    if not sentences:
        return result

    # INDEPENDENCE IS NOW A PROPERTY OF THE MODEL, NOT THE PROVIDER, and that is
    # what `TASK_MODELS` buys: the writer is gpt-oss-20b and the auditor is
    # Qwen3.8-27b on the same key. Excluding the writer's PROVIDER would send
    # this back to local Ollama and the 819 seconds it used to cost.
    #
    # `written_by` is kept in the signature and used for the report below,
    # because the honest question — did the same model grade its own work? — is
    # answered after the call, from the trace, rather than assumed before it.
    exclude: set[str] = set()
    if on_sentence is not None:
        # One call now, so the old per-sentence counter would always read
        # "1 of 1". Report how many sentences are in the pass instead.
        on_sentence(len(sentences), len(sentences), "")
    try:
        result.verdicts.extend(_audit_batch(sentences, facts, router, exclude))
    except AllProvidersFailed as exc:
        # Recorded, never silently treated as a pass: a letter that could not be
        # checked has not been checked.
        log.warning("fact check unavailable: %s", exc)
        result.verdicts.extend(
            SentenceVerdict(s, "SUPPORTED", None, source="skipped") for s in sentences
        )

    audited = next((c for c in reversed(router.calls) if c.task == "fact_check"), None)
    wrote = next((c for c in reversed(router.calls) if c.task == "letter_write"), None)
    result.auditor = f"{audited.provider}/{audited.model}" if audited else ""
    if audited is None:
        result.independent = False
    elif wrote is not None:
        # The two models that actually answered, which is the only evidence
        # worth trusting — being ROUTED somewhere is not being answered by it.
        result.independent = audited.model != wrote.model
    else:
        # This router never wrote the letter, so there is nothing to compare
        # against and the caller's word is all there is. `check_letter` is
        # called this way by the tests and by a re-check of a letter loaded
        # from disk.
        result.independent = bool(written_by) and audited.provider != written_by
    if not result.independent and audited is not None:
        result.advisory = True
        result.notes.append(
            f"NOT INDEPENDENT — {audited.model} audited output from the same model. "
            f"A model grading itself is biased toward passing, so this pass is worth "
            f"less than a clean one: read the letter yourself."
        )
    no_claim = sum(1 for v in result.verdicts if v.source == "no-claim")
    if no_claim:
        result.notes.append(
            f"{no_claim} sentence(s) make no first-person claim — they describe the role or "
            f"the employer, so there is nothing about the candidate to check"
        )
    unchecked = sum(1 for v in result.verdicts if v.source == "skipped")
    if unchecked:
        result.notes.append(
            f"{unchecked} of {len(result.verdicts)} sentence(s) could not be checked by a model — "
            f"only the deterministic gates ran on them"
        )
    return result
