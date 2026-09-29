"""STAGE 5b — the prose, and the gates it has to pass to survive.

The writer's prompt contains only the plan, the `facts` arrays of the selected
evidence blocks, the style guide and the avoid list. Not the full profile, not
the full posting, not the CV. Less context means less to hallucinate from.

The gates here are deterministic and run before the fact checker, because they
are free and they catch the commonest failures: a banned phrase, a gated verb, a
forbidden topic, a number nobody can substantiate. A hit quotes the offending
span back and regenerates — at most twice.

WHAT HAPPENS AFTER THOSE TWO RETRIES CHANGED ON 27 SEP 2026, and the reason is
that the old answer was to hand back nothing. Every gate was equal, so one narrow
no-break space in "1 000" failed the same test as a fabricated metric, and about
half of all runs ended with no letter, no CV and no folder. So the gates are
classified in `truth.py` — claim, policy or style — and the ladder is now:

1. repair what needs no judgement at all (`repair_mechanics`, free);
2. delete what can only be deleted (`excise_sentence`, free);
3. rewrite one offending sentence at a time (`repair_violations`, one small call);
4. ship it, under a name its state earns, with a `REVIEW.md` saying what to check.

A CLAIM never reaches a sendable letter. A STYLE problem never costs one.
"""

from __future__ import annotations

import contextlib
import json
import logging
import pathlib
import re
from dataclasses import dataclass, field

from .llm import TaskRouter
from .models import EvidenceBlock, Profile
from .planner import NarrativePlan
from .truth import NO_SURGERY, TruthLayer, Violation, blocks_sending, severity

log = logging.getLogger(__name__)

__all__ = [
    "ship_safe",
    "Letter", "write_letter", "draft_and_check", "banned_phrase_hits",
    "writer_whitelist", "MAX_RETRIES", "posting_locations",
    "invented_location_hits", "stale_availability_hits", "overclaim_hits",
    "summarising_close_hits", "shape_hits", "Repair", "repair_mechanics",
    "excise_sentence", "plain_spaces", "repair_violations", "MAX_REPAIRS",
    "stock_connector_hits",
    "REPAIR_SCHEMA",
]

PROMPTS = pathlib.Path(__file__).parent / "prompts"
MAX_RETRIES = 2

#: Bumped whenever the writer prompt changes in a way that could move the output.
#: `voice_eval.csv` keys on it, so a change that helps one letter and quietly
#: hurts the next is visible rather than a matter of impression.
#: v6 (25 Sep 2026) — the first version answering Simon's own ratings rather
#: than the assistant's. Five graded letters said the voice was already there
#: (4.54/5, 27% rewrite) and that three other things were not: the writer was
#: inventing a destination city, implying availability from the MSc end date,
#: and generalising a single experiment into a law. All three were facts the
#: profile or the posting already held and the prompt never passed.
#: v7 (27 Sep 2026) — the version that stops a letter dying of typography. The
#: writer is now told which verbs it MAY use rather than only which it may not
#: (`preferred_verbs` was computed on every run and read by nothing), corrections
#: are ordered by severity rather than alphabetically by span, and the fact
#: checker's objection survives the next gate rejection instead of being
#: overwritten.
#: v8 (28 Sep 2026) — the closing stops quoting the sentence it forbids. v7 and
#: earlier displayed the bad invitation verbatim, four times, and 21 of the 25
#: letters on disk came back containing "glad to", several reproducing the
#: forbidden shape almost word for word. Showing a model the phrasing to avoid
#: makes that phrasing available to it; the section now shows only the two forms
#: that are acceptable and states the failure as a rule about adjectives.
PROMPT_VERSION = "v8"

#: Square-bracket placeholders a model leaves when it has nothing to say —
#: "[Company Name]", "[your achievement]". Shipping one is worse than any
#: stylistic failure, because it announces the letter was generated.
PLACEHOLDER = re.compile(r"\[[^\]\n]{2,60}\]")


@dataclass
class Letter:
    text: str = ""
    attempts: int = 0
    violations: list[Violation] = field(default_factory=list)
    banned: list[str] = field(default_factory=list)
    words: int = 0
    accepted: bool = False
    #: Every draft, so a rejection can be read rather than guessed at.
    drafts: list[str] = field(default_factory=list)
    #: What was fixed without asking anyone, so the note can say so.
    repairs: list[Repair] = field(default_factory=list)
    #: Repairs that were REFUSED, and why. A silent refusal is how a guard stays at
    #: the wrong setting forever, so every discarded rewrite says what was wrong
    #: with it and lands in `REVIEW.md`.
    repair_notes: list[str] = field(default_factory=list)

    @property
    def paragraphs(self) -> list[str]:
        return [p.strip() for p in self.text.split("\n\n") if p.strip()]

    @property
    def blocking(self) -> list[Violation]:
        """The violations that may never reach a sendable letter.

        Separate from `accepted`, which keeps its exact old meaning — nothing
        outstanding at all — because four tests assert it and the retry loop is
        still right to chase perfection inside its budget. What changed is the
        SHIPPING decision: a narrow no-break space in "1 000" is not the same
        thing as a fabricated metric, and treating them alike is what produced
        runs with nothing on disk at all.
        """
        return blocks_sending(self.violations)

    @property
    def shippable(self) -> bool:
        """May this letter be written to `output/` at all?

        Banned phrases are deliberately absent from this test. They are style,
        they carry no code, and the worst a surviving one does is make the letter
        read a little flat — which is worth a line in the note, never a lost
        letter.
        """
        return not self.blocking


def banned_phrase_hits(text: str, phrases: list[str]) -> list[str]:
    """Enforced by regex, not by hoping the prompt was read.

    Whitespace-flexible because a model will happily break "proven track record"
    across a line and defeat a literal search.
    """
    hits = []
    for phrase in phrases:
        pattern = r"\s+".join(re.escape(w) for w in phrase.split())
        if re.search(pattern, text, re.I):
            hits.append(phrase)
    return hits


#: "relocate to X", "move to X", "based in X" — the destination is a factual
#: claim about the POSTING, and until v6 nothing checked it. Four of the first
#: seven letters named somewhere the posting does not: London for a San
#: Francisco role, Berlin for a Munich one, "the Waymo campus", and "relocate
#: to Helsing", which is a company rather than a place. The profile says
#: `willing_to_relocate: true`, so the truth layer was satisfied and the
#: destination went unexamined.
DESTINATION = re.compile(
    r"\b(?:relocat\w+|reloc\w+|mov\w+|based|situated|settle\w*)\s+(?:to|in|at)\s+"
    r"((?:the\s+)?[A-Z][\w'\u2019.-]*(?:[\s-]+(?:of\s+)?[A-Z][\w'\u2019.-]*){0,3})"
)

#: Availability conditioned on the MSc finishing. `personal.yaml` records
#: "available immediately, full-time" and `availability_conflict: resolved` —
#: the MSc runs alongside the work. The writer was never given that field, was
#: given "expected July 2027" as a fact, and inferred the obvious wrong thing
#: in every letter. Mentioning the end date is fine, and for a fixed-start
#: scheme it is right; tying AVAILABILITY to it is what these catch.
STALE_AVAILABILITY = (
    re.compile(r"available\s+(?:from|in|starting|as\s+of|after)\s+\w*\s*(?:20\d\d|July|Jul\b)", re.I),
    re.compile(r"(?:after|once|upon|following|when)\s+(?:I\s+)?(?:finish|complete|completing|"
               r"completion\s+of|graduat\w+)[^.]{0,48}?(?:MSc|master|degree|thesis|studies)", re.I),
    re.compile(r"(?:available|free|able|ready)\s+to\s+(?:start|join|begin|commence)"
               r"[^.]{0,40}?(?:20\d\d|July|Jul\b|graduat\w+)", re.I),
    re.compile(r"(?:MSc|master\w*|degree|thesis|studies)[^.]{0,40}?"
               r"(?:before|then)\s+(?:I\s+)?(?:could|can|would)\s+(?:start|join|begin)", re.I),
)

#: One experiment turned into a law. Simon's own style is the opposite — his
#: notes say state limits, and the letters do report negative results honestly
#: — but the INTERPRETATION kept overreaching.
#:
#: Each entry carries an INSTRUCTION, not just an objection, and neither repeats
#: the wording being rejected. Three live attempts showed why. Told not to write
#: "truly better", the model wrote "true performance"; told not to write that, it
#: went back to "truly better, not simply tuned harder" — almost word for word
#: the *bad example* the prompt itself was showing it. Naming a phrase makes it
#: available, so the prompt now shows only good forms.
#:
#: The instruction describes the SHAPE and never hands over a clause to paste.
#: The first version did hand one over — "performance under the budget I gave
#: both methods" — and the model completed it with "was comparable", which the
#: profile does not support and which understated the real result. A fragment
#: invites an invented ending; an instruction to go back to the facts does not.
OVERCLAIM: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\btruly\s+(?:better|best|outperform\w*|superior|optimal)\b", re.I),
     "one comparison cannot settle which method wins in general",
     "name the budget both methods were given, then state only the outcome your "
     "FACTS record — if they record none, say what the equal budget made possible "
     "and stop there"),
    (re.compile(r"\bthese\s+(?:findings|results)\s+(?:illustrate|demonstrate|prove|establish)\b", re.I),
     "a research-summary template; say what YOU took from it",
     "what I took from it was that an ablation can tell you whether the problem is "
     "the model, the data or the evaluation before you add anything"),
    (re.compile(r"\bmost\s+(?:projects|teams|people|candidates)\s+(?:skip|ignore|neglect|miss|omit)\b", re.I),
     "an unnecessary claim about everyone else's work; the check stands on its own",
     "a check that catches the kind of failure nobody notices until it is expensive"),
    (re.compile(r"\b(?:is|was|are|were)\s+the\s+core\s+(?:challenge|problem|issue|difficulty)\b", re.I),
     "your experiment showed this mattered in YOUR setup; scope it to that",
     "which made credit assignment the clear limitation here: the only signal "
     "arrived at the end of a long sequence"),
    # Was limited to a noun list and let through "an independent replication
    # reproduced the final result, proving that an equal budget is what makes the
    # comparison mean anything" — which a replication does not show at all.
    (re.compile(r"\b(?:proves|proving|proved|demonstrat\w+)\s+that\b", re.I),
     "a single result does not prove a general claim, and a replication shows "
     "only that the result repeats",
     "write \u201cgiving me confidence that\u2026\u201d or \u201cshowing\u2026\u201d "
     "and confine it to what this run actually tells you"),
    (re.compile(r"\bthe\s+only\s+way\s+to\s+(?:determine|establish|know|tell|measure|"
                r"compare|prove)\b", re.I),
     "an absolute about method comparison",
     "say what your own setup allowed you to see, without ranking the methods "
     "beyond what the FACTS say"),
    (re.compile(r"\bis\s+the\s+whole\s+(?:problem|challenge|story|point|issue)\b", re.I),
     "scope it to your experiment rather than to the field",
     "was the limitation here"),
    (re.compile(r"\btrue\s+(?:performance|superiority|capability|quality)\b", re.I),
     "there is no such measurement outside a setup",
     "name the budget or the benchmark the figure came from, and assert no "
     "comparison your FACTS do not already state"),
    (re.compile(r"\b(?:always|never|invariably)\s+(?:outperform\w*|beats?|wins?|better)\b", re.I),
     "one comparison cannot support 'always' or 'never'",
     "confine the claim to this comparison and this budget"),
)


#: The sign-off that lists the writer's qualities back at the reader. All ten
#: letters generated before this ended the same way — "discuss how my rigorous,
#: leak-free evaluation and reproducible pipelines fit the simulator evaluation
#: work at Waymo".
#:
#: What fails is the ENUMERATION, not the invitation. Simon asked first for the
#: long summarising version to go, then for a plain professional close to stay —
#: which is not a contradiction once you see what separates them. "…how my
#: experience could contribute to Predium's data-science team" names one generic
#: thing and reads as courtesy. "…how my experience in data-pipeline design and
#: optimisation rigour can support your team" is a précis of the three
#: paragraphs above it. So a list, an "and", or more than three words fires; one
#: noun does not. Checked against all ten real closings.
SUMMARISING_CLOSE = (
    re.compile(r"\bhow\s+my\s+([^.]{2,}?)\b(?:can|could|would|will|might)\s+"
               r"(?:support|contribute|help|benefit|serve|fit)\b", re.I),
    re.compile(r"\bhow\s+my\s+([^.]{2,}?)\b(?:aligns?|fits?)\b", re.I),
)
#: No qualifying phrase rescues this one.
BRAGGING_CLOSE = re.compile(r"\bwhat\s+you\s+(?:gain|get)\s+is\s+a\b", re.I)


def _enumerates(phrase: str) -> bool:
    """Does this name several qualities rather than one?"""
    phrase = phrase.strip()
    return bool(
        "," in phrase or re.search(r"\band\b", phrase, re.I) or len(phrase.split()) > 3
    )


#: The connector that keeps surviving every prompt instruction. A real letter
#: closed "…and that approach aligns with the rigorous experimental design I
#: applied", which is the construction Simon's own guide calls the worst sentence
#: in the batch: it praises a quality of his and would bolt onto any fact about
#: any employer.
#:
#: STYLE class on purpose. It can never cost a letter — it gets one surgery
#: attempt and otherwise lands in `REVIEW.md` — because the pattern may yet fire
#: on a legitimate use, and that should be tuned from `voice_eval` data rather
#: than from an impression.
STOCK_CONNECTOR: tuple[re.Pattern[str], ...] = (
    re.compile(r"\baligns?\s+(?:well\s+)?with\s+(?:my|the\s+\w+\s+I)\b", re.I),
    re.compile(r"\bthat\s+approach\s+aligns?\b", re.I),
    re.compile(r"\bresonates?\s+with\s+(?:my|me)\b", re.I),
    re.compile(r"\bmirrors?\s+my\b", re.I),
    re.compile(r"\bspeaks\s+(?:directly\s+)?to\s+my\b", re.I),
)


def stock_connector_hits(text: str) -> list[Violation]:
    """A fact about the employer glued to a compliment about the candidate."""
    out: list[Violation] = []
    for pattern in STOCK_CONNECTOR:
        if match := pattern.search(text):
            out.append(Violation(
                "STOCK_CONNECTOR",
                "this joins their work to yours with a phrase that would fit any "
                "employer — name the constraint both are under instead",
                match.group(0),
            ))
            break
    return out


def summarising_close_hits(text: str) -> list[Violation]:
    """An invitation carrying a précis of the letter above it."""
    out: list[Violation] = []
    for pattern in SUMMARISING_CLOSE:
        match = pattern.search(text)
        if match and _enumerates(match.group(1)):
            out.append(Violation(
                "SUMMARISING_CLOSE",
                "the closing lists your own qualities back at the reader; the "
                "evidence paragraphs already made that case. Name at most one "
                "thing, or simply offer the conversation",
                " ".join(match.group(0).split()),
            ))
    if (match := BRAGGING_CLOSE.search(text)):
        out.append(Violation(
            "SUMMARISING_CLOSE",
            "do not describe what the employer gains; show it and stop",
            " ".join(match.group(0).split()),
        ))
    return out


#: A letter with no name under it. Caught by the batch report but NOT by the
#: gate that decides whether to ship, so an unsigned letter reached `output/` on
#: 25 Sep. The two check sets had drifted; this is the shared one.
#:
#: Broadened beyond English on 27 Sep 2026. `planner._letter_language` writes in
#: German, Portuguese, Ukrainian or Russian where the profile claims C1 or better,
#: and a correctly greeted German letter used to trip NO_GREETING \u2014 which, once
#: the repair stage existed, would have stapled an English salutation on top of
#: its own.
#: Two alternations, because the inflected greetings need a suffix and the English
#: ones must not have one: `\u0448\u0430\u043d\u043e\u0432\u043d\w*` has to reach "\u0448\u0430\u043d\u043e\u0432\u043d\u0456", but `hi\w*` would
#: happily match a letter that opens "Hiring at your scale\u2026".
GREETING = re.compile(
    r"^\s*(?:"
    r"(?:dear|hello|hi|hallo|bonjour|madame|monsieur|guten\s+tag"
    r"|sehr\s+geehrte|ol[\u00e1a]|gentile"
    r"|\u0434\u043e\u0431\u0440\u0438\u0439\s+\u0434\u0435\u043d\u044c|\u0437\u0434\u0440\u0430\u0432\u0441\u0442\u0432\u0443\u0439\u0442\u0435)\b"
    r"|(?:\u0448\u0430\u043d\u043e\u0432\u043d|\u0443\u0432\u0430\u0436\u0430\u0435\u043c|prezad|estimad|car[oa])\w*"
    r")",
    re.I,
)

#: "1 000" and "63 000" — a narrow no-break space where the facts use a comma.
#: Purely typographic, but it is not the form the profile records, and the rule
#: everywhere else is that numbers are copied exactly.
THIN_SPACE_DIGITS = re.compile(r"\d[\u202f\u00a0\u2009\u2007]\d{3}\b")

#: Characters that LOOK like a space or a hyphen and are not. gpt-oss-20b emits
#: them constantly — one real letter carried eighteen U+2011 and six U+202F.
_LOOKALIKES: tuple[tuple[str, str], ...] = (
    ("\u202f", " "), ("\u00a0", " "), ("\u2007", " "), ("\u2009", " "),
    ("\u2011", "-"), ("\u2018", "'"), ("\u2019", "'"),
)


def plain_spaces(value: str) -> str:
    """The text with lookalike spaces and hyphens flattened, for COMPARING only.

    A gate that matches model output against the posting or the profile has to do
    this first, or it rejects the right answer. A letter for job 930 wrote
    "relocate to San\u202fFrancisco" with a narrow no-break space; the posting says
    "San Francisco, CA"; the substring test between them failed, and
    `invented_location_hits` reported an invented city that the posting itself
    names.

    That is unrecoverable rather than merely noisy. The correction tells the
    writer to name a posted location, the writer names San Francisco again, and
    three attempts died identically with no PDF at the end of them.

    Whether it bites at all is luck: an accepted letter from the same run carried
    twenty-four of these characters and passed, because none of them landed inside
    a place name.

    FOR COMPARISON ONLY — it collapses newlines, so a letter put through it
    would lose its paragraphs. `ship_safe` below is the variant that the text on
    disk goes through. Both leave a thin space BETWEEN DIGITS alone, because
    `THIN_SPACE_DIGITS` reports it and the NUMBER_FORM repair writes the comma
    the profile uses.
    """
    out = str(value or "")
    for fancy, plain in _LOOKALIKES:
        out = out.replace(fancy, plain)
    return " ".join(out.split())


#: A thin space doing the job of a thousands separator, which `THIN_SPACE_DIGITS`
#: and the NUMBER_FORM repair between them turn into a comma. `ship_safe` must
#: leave these alone or that repair never fires and "63 000" ships unchanged.
_DIGIT_SEPARATOR = re.compile(r"(?<=\d)[\u202f\u00a0\u2007\u2009](?=\d)")


def ship_safe(value: str) -> str:
    """The same flattening, for the text that DOES ship — and paragraphs kept.

    Every letter on disk before 28 Sep 2026 carried between sixteen and
    thirty-three U+2011 non-breaking hyphens, and several a U+202F. Nothing in
    `profile/` or the prompts contains one; the model emits them unprompted.

    They matter because the letter is a `.txt` that gets pasted into an ATS box.
    A keyword search for "macro-F1" does not match "macro\u2011F1", so the
    posting's own vocabulary silently fails to match the letter that was written
    to contain it — and some older systems render the character as a box.

    Three things this deliberately does NOT do:

    * it does not touch a thin space BETWEEN DIGITS, which is the one case that
      already has an owner: the gate reports NUMBER_FORM and `repair_mechanics`
      writes the comma the profile uses, with a line in the note saying so;
    * it does not collapse whitespace the way `plain_spaces` does, because that
      would join the paragraphs into one;
    * it leaves U+2013 and U+2014 alone. An en dash in "2018-2026" and an em
      dash between clauses are punctuation the writer chose, not lookalikes.
    """
    text = str(value or "")
    parked = _DIGIT_SEPARATOR.sub("\x00", text)
    for fancy, plain in _LOOKALIKES:
        parked = parked.replace(fancy, plain)
    return parked.replace("\x00", "\u202f")


@dataclass(frozen=True)
class Repair:
    """One thing that was fixed without asking a human, and what it was.

    Recorded rather than done silently, because `REVIEW.md` has to be able to say
    "signed it for you" — a note that claims everything is verified while quietly
    having rewritten the letter is worse than no note.
    """

    code: str
    #: mechanical — pure text, no model. excision — a whole sentence removed.
    #: surgery — one sentence rewritten by a model and then re-gated.
    tier: str
    #: Plain English for the note: "added the greeting line".
    what: str
    before: str = ""
    after: str = ""
    #: The provider, for surgery only.
    by: str = ""


def repair_mechanics(
    text: str, profile: Profile, violations: list[Violation], *, language: str = "en"
) -> tuple[str, list[Repair]]:
    """Fix the three failures that need no judgement at all. Pure — no model, no I/O.

    Each of these used to cost a whole draft, and one of them used to cost the
    whole letter. A narrow no-break space in "1 000" failed the same acceptance
    test as a fabricated metric, so a letter could be thrown away over typography
    while the correction line explaining it had already been truncated out of the
    prompt.

    Nothing here can introduce a claim:

    * the greeting is the one value the writer prompt already specifies as its
      fallback, and no recipient name exists anywhere in this pipeline;
    * the signature is the profile's own `personal.name`, and the check it
      satisfies is a bare substring search rather than a position test;
    * the separator swap cannot launder a figure, because `normalise_numerals`
      folds those four characters BEFORE the allowlist test and the validator
      licenses the comma'd and bare forms alike. A number that is not in the
      profile still fails, whichever way it is punctuated.

    ENGLISH ONLY, and that is not timidity. `personal.yaml` records Ukrainian and
    Russian as native, `planner._letter_language` will pick either for a posting
    written in them, and in that typography the thin space IS the thousands
    separator while the comma is the decimal mark. Rewriting there would turn
    "1 000" into "one point zero". For any other language the violations are
    reported in the note instead of repaired.
    """
    if language != "en":
        return text, []

    codes = {v.code for v in violations}
    out, repairs = text, []

    if "NO_GREETING" in codes:
        out = f"Dear Hiring Manager,\n\n{out.lstrip()}"
        repairs.append(Repair("NO_GREETING", "mechanical", "added the greeting line",
                              after="Dear Hiring Manager,"))

    if "NUMBER_FORM" in codes:
        fixed = THIN_SPACE_DIGITS.sub(lambda m: m.group(0)[0] + "," + m.group(0)[2:], out)
        if fixed != out:
            spans = [m.group(0) for m in THIN_SPACE_DIGITS.finditer(out)]
            out = fixed
            repairs.append(Repair(
                "NUMBER_FORM", "mechanical",
                f"wrote {len(spans)} number(s) with a comma, as the profile does",
                before=", ".join(spans[:3]),
            ))

    # Last, so the name is not then shifted by another repair.
    name = (profile.personal.name or "").strip()
    first = name.split()[0] if name else ""
    if "NOT_SIGNED" in codes and first and first not in out:
        out = f"{out.rstrip()}\n\n{name}"
        repairs.append(Repair("NOT_SIGNED", "mechanical", f"signed it {name!r}", after=name))

    return out, repairs


def excise_sentence(text: str, span: str) -> tuple[str, str]:
    """Remove the sentence containing `span`. Returns `(text, removed)`.

    For `FORBIDDEN_TOPIC` only, and deterministic on purpose. A work-authorisation
    sentence is self-contained — "I have the right to work in the EU" — so the
    correct repair is deletion, and deletion needs no model and cannot go wrong in
    an interesting way. Every other class needs the claim rewritten rather than the
    sentence dropped.

    Refuses rather than damages: never the block holding the greeting or the
    signature, and never the last sentence of a paragraph, so a deletion cannot
    leave an empty block or behead the letter. A refusal returns the text
    unchanged and the violation survives, which is the correct outcome — the
    letter then never ships as clean.
    """
    from .factcheck import split_sentences

    if not span.strip():
        return text, ""
    needle = plain_spaces(span).lower()
    blocks = text.split("\n\n")
    for index, block in enumerate(blocks):
        if not block.strip():
            continue
        if index == 0 or index == len(blocks) - 1:
            continue                      # the greeting and the sign-off
        sentences = split_sentences(block)
        if len(sentences) < 2:
            continue                      # deleting it would empty the paragraph
        for sentence in sentences:
            if needle not in plain_spaces(sentence).lower():
                continue
            start = block.find(sentence)
            if start < 0:
                continue
            kept = (block[:start] + block[start + len(sentence):])
            kept = re.sub(r"\s{2,}", " ", kept).strip()
            if not kept:
                continue
            blocks[index] = kept
            return "\n\n".join(blocks), sentence
    return text, ""


#: How many sentences may be operated on per letter. Three, because a letter
#: needing a fourth has a bad PLAN, and surgery cannot fix a plan. At the cap the
#: whole stage costs under 1,700 tokens — a third of one more whole-letter rewrite.
MAX_REPAIRS = 3

#: One key, and the single key IS the safety mechanism. There is no `verdict`, no
#: `cannot_fix`, no `reason` and no `confidence`, so the model has no channel in
#: which to dispute the rule — the only thing it can return is replacement prose,
#: and whether that prose is accepted is decided by re-running the gate.
#:
#: Flat for a second reason: `schema_hint` can only name top-level keys, and a
#: nested shape reaching an unconstrained local model comes back invented. The
#: research verifier learned that one the expensive way.
REPAIR_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"sentence": {"type": "string"}},
    "required": ["sentence"],
    "additionalProperties": False,
}


def _locate(text: str, span: str) -> str:
    """The whole sentence containing `span`, as it appears in `text`.

    Compared through `plain_spaces` on both sides, because a `Violation.span` is
    whitespace-collapsed by the gate that made it while the letter may carry a thin
    space or a line break inside the same words.

    Uses `split_sentences` and NOT `factcheck._merge_fragments`: the latter joins
    pieces with a space, so what it returns is not necessarily a substring of the
    text and could not be spliced back.
    """
    from .factcheck import split_sentences

    needle = plain_spaces(span).lower()
    if not needle:
        return ""
    for sentence in split_sentences(text):
        if needle in plain_spaces(sentence).lower() and sentence in text:
            return sentence
    return ""


def _acceptable(
    original: str, replacement: str, span: str, *, claim: bool
) -> str:
    """`""` if the replacement may be spliced, else why it may not.

    Four shape guards, each closing one way a model argues instead of fixing, and
    all of them cheaper than the re-gate that follows.
    """
    if not replacement.strip():
        return "the model returned nothing"
    if "\n" in replacement:
        return "the model returned more than one line"
    if len(replacement.split()) > len(original.split()) * 2.5 + 12:
        return "the model returned a paragraph rather than a sentence"
    if plain_spaces(span).lower() in plain_spaces(replacement).lower():
        return "the offending words are still in it"
    if claim:
        # A CLAIM may only be REMOVED. The prompt says so, and this enforces it:
        # a model that rewrites rather than deletes cannot introduce a new
        # assertion, because it cannot introduce a new content word.
        if len(replacement.split()) > len(original.split()):
            return "a claim may only be shortened, and this is longer"
        kept = {w.lower().strip(".,;:'\"") for w in original.split() if len(w) >= 4}
        new = {w.lower().strip(".,;:'\"") for w in replacement.split() if len(w) >= 4}
        if invented := sorted(new - kept):
            return f"it introduces {', '.join(invented[:3])}, which the original did not say"
    return ""


def repair_violations(
    text: str,
    profile: Profile,
    truth: TruthLayer,
    router: TaskRouter,
    job: dict,
    *,
    language: str = "en",
    written_by: str = "",
    max_repairs: int = MAX_REPAIRS,
) -> tuple[str, list[Repair], list[str]]:
    """THE DOUBLE ANALYZE — one sentence at a time, then re-gated.

    Runs once per letter, after the retry loop is exhausted, and never on a letter
    that passed. Returns `(text, repairs, notes)`.

    WHY NOT A FOURTH WHOLE-LETTER REWRITE, which is the obvious thing:

    * it costs ~5,450 tokens against ~560 here;
    * it re-rolls the twelve sentences that were already fine, and the comment on
      the retry loop above records three consecutive attempts doing exactly that,
      "each fixing what it was shown and breaking something it had already been
      told";
    * after surgery the diff is one sentence, so `REVIEW.md` can print before and
      after and Simon checks ONE sentence, which is what he actually asked for;
    * `factcheck` already established that short prompts are where small models
      are reliable.

    A MODEL MAY NEVER DISMISS A VIOLATION, and that is enforced by construction
    rather than by instruction. `REPAIR_SCHEMA` gives it nowhere to say so;
    `_acceptable` rejects the shapes an argument takes; and the replacement is kept
    only if re-running the gate proves the violation gone and no new claim in its
    place. On any doubt the original sentence is restored verbatim.
    """
    from .llm import AllProvidersFailed

    system = (PROMPTS / "09_sentence_repair.md").read_text(encoding="utf-8")
    exclude = {written_by} if written_by else set()
    if exclude and not router.providers_for("letter_repair", exclude):
        exclude = set()

    repairs: list[Repair] = []
    notes: list[str] = []
    done = 0

    # Claims first, then the gate's own order, so the budget is never spent on a
    # flat closing while a fabricated number waits behind it.
    for violation in sorted(
        _gate(text, profile, truth, job)[0],
        key=lambda v: 0 if severity(v.code) != "style" else 1,
    ):
        if violation.code in NO_SURGERY:
            # Reported by the gate, carried into REVIEW.md, and deliberately not
            # operated on. Rewriting this sentence means removing the role's own
            # subject matter from the one line whose job is to name it.
            continue
        if done >= max_repairs:
            notes.append(
                f"{violation.code.lower()} was left alone — three sentences had already "
                f"been repaired, and a letter needing a fourth has a planning problem "
                f"rather than a phrasing one"
            )
            continue
        if violation.code == "PLACEHOLDER":
            # A placeholder is the model having had nothing to say. There is
            # nothing to substitute and deleting it leaves a hole mid-sentence.
            continue

        sentence = _locate(text, violation.span)
        if not sentence:
            notes.append(f"could not find the sentence behind {violation.span!r} to repair it")
            continue

        claim = severity(violation.code) != "style"
        user = "\n".join([
            f"THE SENTENCE:\n{sentence}",
            "",
            f"THE RULE IT BREAKS:\n{violation.detail}",
            f"THE EXACT WORDS AT FAULT:\n{violation.span}",
            "",
            ("You may only REMOVE. Do not add any word that is not already in the "
             "sentence above." if claim else
             "You may rephrase. Every specific thing must already be in the sentence."),
            *([f"\nVERBS YOU MAY USE: {', '.join(truth.preferred_verbs)}"]
              if truth.preferred_verbs else []),
        ])
        before_blocking = len(blocks_sending(_gate(text, profile, truth, job)[0]))
        try:
            raw = router.complete_json(
                "letter_repair", system, user, schema=REPAIR_SCHEMA,
                temperature=0.2, max_tokens=200, exclude=exclude or None,
            )
        except AllProvidersFailed as exc:
            notes.append(f"no model was reachable to repair {violation.code.lower()}: {exc}")
            break

        replacement = " ".join(str(raw.get("sentence") or "").split())
        if why := _acceptable(sentence, replacement, violation.span, claim=claim):
            notes.append(f"the repair of {violation.code.lower()} was rejected — {why}")
            continue

        candidate = text.replace(sentence, replacement, 1)
        after = _gate(candidate, profile, truth, job)[0]
        still_there = any(
            v.code == violation.code and plain_spaces(v.span) == plain_spaces(violation.span)
            for v in after
        )
        new_claims = {v.code for v in blocks_sending(after)} - {
            v.code for v in blocks_sending(_gate(text, profile, truth, job)[0])
        }
        if still_there or new_claims or len(blocks_sending(after)) > before_blocking:
            notes.append(
                f"the repair of {violation.code.lower()} was discarded — "
                + ("it did not fix it" if still_there else f"it introduced {sorted(new_claims)}")
            )
            continue

        by = next((c.provider for c in reversed(router.calls) if c.task == "letter_repair"), "")
        text = candidate
        done += 1
        repairs.append(Repair(
            violation.code, "surgery",
            f"rewrote one sentence to fix {violation.code.lower()}",
            before=sentence, after=replacement, by=by,
        ))

    return text, repairs, notes


def shape_hits(text: str, profile: Profile) -> list[Violation]:
    """Structural failures no reader would forgive and no rating can excuse.

    Shared by the generation gate and the batch report, because they disagreed
    once and the stricter one was the report — which runs after the letter has
    already been written out.
    """
    out: list[Violation] = []
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    if not blocks or not GREETING.match(blocks[0]):
        out.append(Violation("NO_GREETING", "the letter needs to open with a greeting",
                             (blocks[0][:40] if blocks else "")))
    first_name = (profile.personal.name or "").split()
    if first_name and first_name[0] not in text:
        out.append(Violation(
            "NOT_SIGNED",
            f"the letter is not signed — it must end with {profile.personal.name!r} "
            f"on its own line",
            text[-40:].strip(),
        ))
    for match in THIN_SPACE_DIGITS.finditer(text):
        out.append(Violation(
            "NUMBER_FORM",
            "use a comma as the thousands separator, as the facts do — 63,000, "
            "not a spaced group",
            match.group(0),
        ))
    return out


def posting_locations(job: dict) -> list[str]:
    """Every place THIS posting actually names.

    The row already carries them — `cities` as JSON, plus `city`, `location`
    and `country` — and the writer was never shown any of it. Split on the
    separators the sources really use: "Barcelona; Berlin; London; Munich",
    "San Francisco, CA | New York City", "Mountain View, California, USA".
    """
    out: list[str] = []
    raw = job.get("cities")
    if isinstance(raw, str) and raw.strip().startswith("["):
        with contextlib.suppress(ValueError, TypeError):
            out += [str(x) for x in json.loads(raw)]
    elif isinstance(raw, (list, tuple)):
        out += [str(x) for x in raw]
    for key in ("city", "location", "country", "remote_region"):
        value = job.get(key)
        if value:
            out += re.split(r"[;|/\n]|,\s*|\s+or\s+", str(value))
    seen: list[str] = []
    for item in out:
        item = item.strip(" .\t")
        if item and item.lower() not in {s.lower() for s in seen}:
            seen.append(item)
    return seen


def invented_location_hits(text: str, job: dict, allowed: list[str] | None = None) -> list[Violation]:
    """Any destination the posting does not name is a hallucination.

    Saying he would relocate is true and in the profile. Saying WHERE is a
    claim about the job, and the only defensible answers are somewhere the
    posting lists or nowhere at all.
    """
    posted = posting_locations(job)
    # His own city is a legitimate thing to mention, but it is never the answer
    # to "where is this role?", so it is permitted without being suggested.
    permitted = [*(allowed or []), *posted]
    company = str(job.get("posting_company") or job.get("company_name") or "").strip()
    hits: list[Violation] = []
    # Compared with lookalike spaces flattened, because the model writes "San"
    # and "Francisco" joined by a narrow no-break space and the posting does not.
    # Without this the gate rejects the city the posting itself names, and no
    # rewrite can satisfy it — see `plain_spaces`.
    allowed_plain = [plain_spaces(ok).lower() for ok in permitted if ok]
    company_plain = plain_spaces(company).lower()
    for match in DESTINATION.finditer(text):
        place = match.group(1).strip()
        bare = re.sub(r"^the\s+", "", place, flags=re.I).strip()
        bare_plain = plain_spaces(bare).lower()
        if any(bare_plain in ok or ok in bare_plain for ok in allowed_plain):
            continue
        if company_plain and (bare_plain in company_plain or company_plain in bare_plain):
            detail = f"{company!r} is an employer, not a place you can move to"
        elif posted:
            detail = (
                f"the posting says {', '.join(posted[:4])} — name one of those, "
                f"or say you would relocate without naming anywhere"
            )
        else:
            detail = (
                "the posting does not say where this role is, so do not name a "
                "destination at all — 'I would relocate for the role' is enough"
            )
        hits.append(Violation("INVENTED_LOCATION", detail, match.group(0)))
    return hits


#: Any wording that actually states availability now. Checked because naming the
#: MSc end date and then saying nothing lets the reader do the arithmetic — which
#: is what every letter before v6 did, and what Simon objected to.
NOW = r"(?:immediately|now|at\s+once|straight\s+away|right\s+away|today|tomorrow)"
STATES_AVAILABILITY = re.compile(
    # "available immediately", and "available to join Wayve immediately" — the
    # company's name sits between the two halves often enough to matter.
    rf"available\s+{NOW}"
    rf"|available\s+(?:to\s+(?:start|join|begin)\b)?[^.]{{0,36}}?{NOW}"
    rf"|(?:can|could|able\s+to|ready\s+to|free\s+to)\s+(?:start|join|begin)"
    rf"[^.]{{0,36}}?{NOW}"
    rf"|start\s+(?:{NOW}|as\s+soon\s+as)"
    r"|full[-\s]time\s+(?:alongside|while|in\s+parallel)"
    r"|(?:alongside|in\s+parallel\s+with)\s+(?:the|my)\s+(?:MSc|master|thesis|degree|studies)",
    re.I,
)

#: Naming the degree's end without an availability claim beside it.
DATES_THE_MSC = re.compile(
    r"\b(?:MSc|master\w*|degree|thesis)\b[^.]{0,60}?\b(?:20\d\d|July|Jul\b)"
    r"|\b(?:20\d\d|July|Jul\b)[^.]{0,60}?\b(?:MSc|master\w*|degree|thesis)\b",
    re.I,
)


def stale_availability_hits(text: str, profile: Profile) -> list[Violation]:
    """Availability pinned to the MSc finishing, when the profile says now.

    Two failures, because they are opposite halves of the same mistake: saying
    the start depends on the degree, and dating the degree while never saying
    when he is free. The second is what the letters actually did, and a reader
    who is given "I finish my MSc in July 2027" and nothing else will conclude
    July 2027.
    """
    notice = (profile.personal.notice_period or "").strip()
    if not notice or "immediat" not in notice.lower():
        return []
    hits: list[Violation] = []
    for pattern in STALE_AVAILABILITY:
        match = pattern.search(text)
        if match:
            hits.append(Violation(
                "STALE_AVAILABILITY",
                f"the profile says {notice!r} — the MSc runs alongside the work and does "
                f"not delay the start. State availability now; mentioning when the MSc "
                f"ends is fine, tying the start date to it is not",
                " ".join(match.group(0).split()),
            ))
    if not hits and DATES_THE_MSC.search(text) and not STATES_AVAILABILITY.search(text):
        match = DATES_THE_MSC.search(text)
        hits.append(Violation(
            "UNSTATED_AVAILABILITY",
            f"you date the degree and never say when he can start, so the reader will "
            f"assume that date. The profile says {notice!r} — say so in the same breath",
            " ".join(match.group(0).split()) if match else "",
        ))
    return hits


def overclaim_hits(text: str) -> list[Violation]:
    """One experiment generalised into a law.

    The detail names what to write instead. A rejection with nowhere to go makes
    the model oscillate between neighbouring phrasings of the same mistake,
    which is exactly what three live attempts did.
    """
    out: list[Violation] = []
    for pattern, detail, instruction in OVERCLAIM:
        match = pattern.search(text)
        if match:
            out.append(Violation(
                "OVERCLAIM",
                f"{detail} — {instruction}",
                " ".join(match.group(0).split()),
            ))
    return out


def _personal_facts(profile: Profile, job: dict) -> list[str]:
    """Availability and where the role is — stated to the writer, so provable.

    These live in `personal.yaml` and in the posting rather than in the evidence
    bank, which is why they were missing from the whitelist. The relocation line
    is deliberately the POSTING's locations: `invented_location_hits` already
    refuses any other city, so naming one of these is the only legal form.
    """
    out: list[str] = []
    if notice := (profile.personal.notice_period or "").strip():
        out.append(f"the candidate is {notice}")
    if profile.personal.willing_to_relocate:
        where = str(job.get("location") or "").strip()
        out.append(
            "the candidate is willing to relocate"
            + (f", and this role is in {where}" if where else "")
        )
    return out


def writer_whitelist(blocks: list[EvidenceBlock]) -> list[str]:
    """Everything the writer was shown, and therefore everything the checker
    must accept.

    Facts, hooks AND stories. The writer is given all three — the story is the
    field that stops a letter sounding like a language model, so withholding it
    would defeat the point — and a checker shown only `facts` marks the writer's
    faithful use of a hook UNSUPPORTED. Both are Simon's own words; the two
    whitelists have to be the same one.
    """
    out: list[str] = []
    for block in blocks:
        out.extend(block.facts)
        if block.one_line_hook:
            out.append(block.one_line_hook.strip())
        if block.story:
            out.append(block.story.strip())
    return out


def _exemplars(profile: Profile) -> str:
    """Whole letters, good and bad, and nothing else.

    THIS LEADS THE PROMPT, and the ordering is the point. The first version put
    the style guide near the end and cut the samples to three fragments; the
    model followed every rule, avoided every banned phrase, and produced
    competent anonymous prose. It had never seen a finished letter in this
    voice, so shape, pacing and above all the closing paragraph had nothing to
    copy from.

    The `avoid` exemplar does more than the banned-phrase list, because it is
    the same person writing less well — which isolates what went wrong instead
    of which words to dodge.
    """
    entries = [e for e in profile.style.exemplars if isinstance(e, dict) and e.get("text")]
    good = [e for e in entries if e.get("verdict") == "good"]
    avoid = [e for e in entries if e.get("verdict") == "avoid"]
    if not entries:
        return ""

    parts: list[str] = []
    for entry in good[:2]:
        parts.append(
            "═══ A LETTER THIS CANDIDATE WROTE AND WAS HAPPY WITH ═══\n"
            "Match its register, its rhythm and the way it opens and closes.\n"
            "Do not reuse its content — the facts below are the only ones you may assert."
        )
        if entry.get("why"):
            parts.append(f"What makes it work: {' '.join(str(entry['why']).split())}")
        parts.append(str(entry["text"]).strip())
    for entry in avoid[:2]:
        parts.append(
            "\n═══ A LETTER THE SAME CANDIDATE WROTE THAT READS WORSE ═══\n"
            "Do NOT write like this. Same person, weaker letter — the difference is "
            "what you are being asked to avoid.\n"
            "Its CONTENT is not a fact source either: some of what it claims is not "
            "supported, which is part of why it is here. Assert nothing from it."
        )
        if entry.get("why"):
            parts.append(f"What goes wrong: {' '.join(str(entry['why']).split())}")
        parts.append(str(entry["text"]).strip())
    return "\n\n".join(parts)


def _style_section(profile: Profile) -> str:
    """The rules, which come AFTER the examples and matter less than they do."""
    style = profile.style
    parts = ["VOICE:"]
    parts += [f"  - {note}" for note in style.voice_notes]
    if style.signature_moves:
        parts.append("HABITS WORTH COPYING:")
        parts += [f"  - {move}" for move in style.signature_moves]
    parts.append(f"SPELLING: {style.spelling}")
    parts.append("NEVER USE THESE PHRASES:")
    parts.append("  " + "; ".join(style.banned_phrases))

    # Fragments, for when there is no whole exemplar to lead with. Every one of
    # them, not the first three: at ~90 words each they are cheap, and thinning
    # the voice signal was the original problem.
    samples = [s for s in style.writing_samples if isinstance(s, dict) and s.get("text")]
    if samples:
        parts.append("\nMORE OF THE CANDIDATE'S OWN WRITING — match this register:")
        for sample in samples:
            parts.append(f"  [{sample.get('id', 'sample')}] {' '.join(str(sample['text']).split())}")
    return "\n".join(parts)


def _facts_section(blocks: list[EvidenceBlock], truth: TruthLayer) -> str:
    parts = []
    for block in blocks:
        parts.append(f"[{block.id}] {block.title}")
        parts += [f"  fact: {f}" for f in block.facts]
        if block.one_line_hook:
            parts.append(f"  hook: {block.one_line_hook.strip()}")
        if block.story:
            parts.append(f"  the candidate's own words: {block.story.strip()}")
    if truth.no_project_citation:
        parts.append(
            "\nCLAIMABLE BUT NOT CITABLE — these are real, but no public project "
            "exists to name for them, so do not attach one: "
            + ", ".join(sorted(truth.no_project_citation))
        )
    return "\n".join(parts)


def _user_prompt(
    profile: Profile, plan: NarrativePlan, blocks: list[EvidenceBlock],
    truth: TruthLayer, job: dict, correction: str = "",
) -> str:
    company = str(job.get("posting_company") or job.get("company_name") or "")
    exemplars = _exemplars(profile)
    places = posting_locations(job)
    lines = [
        *( [exemplars, ""] if exemplars else [] ),
        f"ROLE: {job.get('title') or ''}",
        f"COMPANY: {company}",
        # Both of these were absent until v6, and the model filled the silence
        # with a guess. A wrong city reads as not having read the posting.
        "WHERE THIS ROLE IS: " + (
            ", ".join(places[:5]) + " — name one of these if you name anywhere at all"
            if places else
            "(the posting does not say — then do not name a destination)"
        ),
        f"AVAILABILITY — state this, do not infer it: {profile.personal.notice_period or 'unstated'}",
        f"LANGUAGE: write in {plan.language}",
        f"TARGET LENGTH: {plan.target_words} words, four paragraphs",
        "",
        "THE PLAN:",
        f"  main theme: {plan.main_theme}",
        f"  secondary:  {plan.secondary_theme}",
        f"  opening:    {plan.opening_strategy}",
        f"  why them:   {plan.company_angle or '(nothing specific — do not invent enthusiasm)'}",
        f"  what draws you to this work: {plan.role_attraction or '(none given)'}",
        f"  the bridge: {plan.domain_bridge or '(none needed)'}",
        f"  use, in this order: {', '.join(plan.evidence_order)}",
        "",
        "FACTS — the complete set of things you may assert:",
        _facts_section(blocks, truth),
        "",
        "AVOID — never claim, in any phrasing:",
        "  " + ", ".join(plan.avoid[:30]),
        "",
        "AVOID — never raise these subjects at all, even if the posting does:",
        "  " + ", ".join(truth.forbidden_topics),
        "",
        "NEVER USE THESE VERBS about the candidate's own work:",
        "  " + ", ".join(truth.gated_verbs),
        # A prohibition with nowhere to go is how the writer lost a whole letter
        # to the word "own". `preferred_verbs` and `approved_low_phrasings` are
        # computed on every run and, until now, were read by nothing — so the
        # model was told what it may not write and never what it may.
        #
        # This is the one place where naming words is safe rather than the trap
        # `OVERCLAIM` documents. There the names were BAD phrasings, and naming
        # them made them available. These are the good ones, and `forbidden.yaml`
        # describes them as "fine and describe what you actually did".
        *(["USE THESE INSTEAD — they describe what he actually did:",
           "  " + ", ".join(truth.preferred_verbs)] if truth.preferred_verbs else []),
        *(["", "THESE SKILLS MAY ONLY BE CLAIMED IN THESE EXACT WORDS:",
           *(f"  {name}: {phrasing}"
             for name, phrasing in sorted(truth.approved_low_phrasings.items()))]
          if truth.approved_low_phrasings else []),
        "",
        _style_section(profile),
        "",
        f"SIGN OFF AS: {profile.personal.name}",
    ]
    if correction:
        lines += ["", "YOUR PREVIOUS DRAFT WAS REJECTED. Fix exactly this:", correction]
    return "\n".join(lines)


def _gate(
    text: str, profile: Profile, truth: TruthLayer, job: dict | None = None
) -> tuple[list[Violation], list[str]]:
    violations = truth.check(text)
    banned = banned_phrase_hits(text, profile.style.banned_phrases)
    for placeholder in PLACEHOLDER.findall(text):
        violations.append(
            Violation("PLACEHOLDER", "an unfilled placeholder would ship as-is", placeholder)
        )
    # The profile's own location is a legitimate destination to name; the
    # posting's is the one that matters.
    violations += invented_location_hits(
        text, job or {}, allowed=[profile.personal.location or ""],
    )
    violations += stale_availability_hits(text, profile)
    violations += overclaim_hits(text)
    violations += summarising_close_hits(text)
    violations += stock_connector_hits(text)
    violations += shape_hits(text, profile)
    return violations, banned


def write_letter(
    profile: Profile,
    plan: NarrativePlan,
    blocks: list[EvidenceBlock],
    truth: TruthLayer,
    router: TaskRouter,
    job: dict,
    *,
    correction: str = "",
    progress=None,
    on_draft=None,
) -> Letter:
    """Draft, gate, and regenerate at most twice.

    A human staring at a rejected draft with three highlighted spans is a good
    outcome. Silently shipping a plausible lie is the only truly bad one.
    """
    system = (PROMPTS / "05b_letter_write.md").read_text(encoding="utf-8")
    letter = Letter()
    #: Every correction this letter has been given, across attempts, mapped to its
    #: severity rank so the budget is spent on claims before typography.
    seen: dict[str, int] = {}
    #: What the caller handed us — the fact checker's objection from the previous
    #: round. The `correction` local is rebuilt from `seen` on every gate failure,
    #: so without keeping this separately the objection vanished the first time the
    #: gate fired and the round was wasted re-learning it.
    inherited = correction.strip()

    for attempt in range(MAX_RETRIES + 1):
        letter.attempts = attempt + 1
        if progress is not None:
            progress(attempt + 1, MAX_RETRIES + 1)
        raw = router.complete_json(
            "letter_write",
            system + '\n\nReturn JSON: {"letter": "<the full text>"}',
            _user_prompt(profile, plan, blocks, truth, job, correction),
            temperature=0.6,
        )
        # BEFORE ANY GATE SEES IT, so there is no interaction to reason about:
        # every check, every repair and the file on disk all read the same
        # characters.
        text = ship_safe(str(raw.get("letter") or raw.get("text") or "").strip())
        letter.drafts.append(text)
        # Persisted here rather than after the loop. A draft that cost 5,937
        # tokens was lost on 25 Sep because the REWRITE hit the daily quota and
        # the exception propagated past the only write. Whatever has been
        # generated survives the next call failing.
        if on_draft is not None:
            on_draft(attempt + 1, text)
        letter.text = text
        letter.words = len(text.split())

        violations, banned = _gate(text, profile, truth, job)

        # GATE, THEN REPAIR, THEN GATE AGAIN, and the second verdict is the one
        # that counts. Repairing blind would mean the note could not say which
        # violation each repair answered; gating first anchors every `Repair` to
        # something that actually fired. `_gate` is pure regex over three hundred
        # words, so the second call costs microseconds against a 5,000-token
        # model call.
        repaired, repairs = repair_mechanics(
            text, profile, violations, language=plan.language
        )
        for violation in violations:
            if violation.code != "FORBIDDEN_TOPIC":
                continue
            shorter, removed = excise_sentence(repaired, violation.span)
            if removed:
                repaired = shorter
                repairs.append(Repair(
                    "FORBIDDEN_TOPIC", "excision",
                    "removed a sentence that raised work authorisation",
                    before=removed,
                ))
        if repairs:
            # `letter.text` was already set to the raw draft above, deliberately,
            # so a later failure cannot lose it. Overwrite it with the repaired
            # version now that one exists; `letter.drafts` keeps the model's words.
            text = repaired
            letter.text, letter.words = text, len(text.split())
            letter.repairs += repairs
            violations, banned = _gate(text, profile, truth, job)

        letter.violations, letter.banned = violations, banned
        if not violations and not banned:
            letter.accepted = True
            return letter

        # Quote the offence back. A model corrects a named span far more
        # reliably than it responds to a restated rule.
        #
        # ACCUMULATED, not replaced. Each attempt rewrites the whole letter, so
        # a correction that mentions only this round's violation lets the model
        # reintroduce the one it was told about last round. Three live attempts
        # produced three different violations in turn — an overclaim, then a
        # listing closing, then a missing signature — each fixing what it was
        # shown and breaking something it had already been told. The budget is
        # only three attempts, so forgetting one of them wastes a third of it.
        # ORDERED BY SEVERITY, NOT ALPHABETICALLY. `sorted(seen)` over a set of
        # strings sorted by the quoted span, and the budget was a flat
        # `violations[:6]` — while `_gate` appends `shape_hits` LAST. So a letter
        # could be rejected for a missing signature whose correction line had been
        # truncated away, and a fabricated number could queue behind five
        # typographic complaints. A dict keyed on the line with the severity rank
        # as its value fixes both: claims first, and every claim survives the cut.
        for violation in violations:
            rank = 0 if severity(violation.code) != "style" else 1
            seen[f"  - you wrote {violation.span!r}: {violation.detail}"] = rank
        for phrase in banned:
            seen[f"  - the phrase {phrase!r} is on the banned list"] = 1
        ordered = sorted(seen, key=lambda line: seen[line])
        claims = [line for line in ordered if seen[line] == 0]
        styles = [line for line in ordered if seen[line] == 1][:4]
        correction = "\n".join([
            # The objection the fact checker raised last round, which used to be
            # destroyed the first time the gate fired inside this loop.
            *([inherited] if inherited else []),
            *(claims + styles)[:12],
            "Rewrite the whole letter. Fix every one of these at once, and keep "
            "everything else you already had right.",
        ])
        log.info("letter attempt %d rejected: %d violation(s), %d banned phrase(s)",
                 attempt + 1, len(violations), len(banned))

    return letter


def draft_and_check(
    profile: Profile,
    plan: NarrativePlan,
    blocks: list[EvidenceBlock],
    truth: TruthLayer,
    router: TaskRouter,
    job: dict,
    *,
    written_by: str = "groq",
    audit: bool = True,
    max_rounds: int = MAX_RETRIES,
    progress=None,
    on_draft=None,
):
    """Write, gate, audit — and when the audit objects, write again.

    `write_letter` only sees the deterministic gates, which is the wrong place to
    stop: an auditor that finds an unsupported claim and has no way to say so
    leaves the human with a rejection and no attempt at a fix. The spec's policy
    is to regenerate with the offending sentence quoted, at most twice, and then
    hand it over.

    Returns `(letter, fact_check)`. The letter carries every draft, so a final
    rejection can be read rather than guessed at.
    """
    from .factcheck import check_letter

    correction = ""
    letter = fact = None
    # The evidence bank PLUS the two personal facts the writer is handed
    # directly in its prompt — availability and the posting's own locations.
    # Same argument as `writer_whitelist`'s own docstring: a checker shown less
    # than the writer marks the writer's faithful use of the rest UNSUPPORTED.
    # Without these, "I am available immediately and would relocate to Berlin"
    # was flagged on a real letter, and it is `personal.yaml` almost verbatim.
    facts = writer_whitelist(blocks) + _personal_facts(profile, job)
    cleared: set[str] = set()

    def say(message: str, fraction: float) -> None:
        if progress is not None:
            progress(message, fraction)

    for round_index in range(max_rounds + 1):
        rounds = max_rounds + 1
        base = round_index / rounds
        say(f"drafting the letter · round {round_index + 1} of {rounds}", base)
        letter = write_letter(
            profile, plan, blocks, truth, router, job, correction=correction,
            on_draft=(
                (lambda attempt, text, _r=round_index: on_draft(_r + 1, attempt, text))
                if on_draft is not None else None
            ),
            progress=lambda a, total, _b=base, _r=round_index, _n=rounds: say(
                f"drafting the letter · round {_r + 1} of {_n}, attempt {a} of {total}",
                _b + (a / total) * (0.5 / _n),
            ),
        )
        if not letter.accepted:
            # THE DOUBLE ANALYZE. The whole-letter retries are spent; try the one
            # thing they never do, which is to change a single sentence and leave
            # the rest alone. Fires once per letter and never on a clean one.
            repaired, surgery, surgery_notes = repair_violations(
                letter.text, profile, truth, router, job,
                language=plan.language, written_by=written_by,
            )
            if surgery:
                letter.text = repaired
                letter.words = len(repaired.split())
                letter.repairs += surgery
                letter.violations, letter.banned = _gate(repaired, profile, truth, job)
                letter.accepted = not letter.violations and not letter.banned
            letter.repair_notes += surgery_notes

        if not letter.shippable:
            # A CLAIM survived every retry, every repair and the surgery. Skip the
            # audit: it is thirteen calls at up to a minute each on a local model,
            # and it can tell Simon nothing he needs about a letter he must edit.
            return letter, None
        if not letter.accepted:
            # STYLE problems only. This used to return here too, on `accepted`,
            # which meant one banned phrase or one flat closing cost the letter its
            # audit AND every remaining round — `max_rounds` could never be reached
            # after the first gate failure. A letter that is going to ship deserves
            # to be checked, so it falls through.
            log.info("letter has %d style issue(s) and will ship with a note",
                     len(letter.violations) + len(letter.banned))

        def on_sentence(index: int, total: int, _text: str, _base: float = base,
                        _rounds: int = rounds, _r: int = round_index) -> None:
            # Half of each round's slice is drafting, half is checking.
            share = (index / max(total, 1)) / _rounds
            say(
                f"checking {total} sentence(s) · round {_r + 1} of {_rounds}",
                min(0.99, _base + share),
            )

        fact = check_letter(
            letter, truth, facts, router, written_by=written_by, audit=audit,
            on_sentence=on_sentence, already_supported=cleared,
        )
        fact.rounds = round_index + 1
        cleared |= fact.supported_text
        if fact.passed or round_index == max_rounds:
            return letter, fact

        # Quote the span, not the rule. A model corrects a named phrase far more
        # reliably than it responds to a restated instruction.
        correction = "\n".join([
            "A fact checker rejected these. Each one claims something the FACTS do not support:",
            *(
                f"  - {v.verdict}: {(v.span or v.sentence)!r}"
                for v in fact.failures[:5]
            ),
            "Rewrite the whole letter. Say only what the FACTS support, and where a "
            "sentence describes the ROLE rather than your own work, make that unmistakable.",
        ])
        log.info("fact check round %d rejected %d sentence(s); rewriting",
                 round_index + 1, len(fact.failures))

    return letter, fact
