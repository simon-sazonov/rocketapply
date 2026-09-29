"""The truth layer — what may be said, and what may not.

Deterministic, and the most important module in the package. Every downstream
stage and the fact checker consume one `TruthLayer`.

THE KEY CLAUSE is that `forbidden_claims` includes every requirement the matcher
found MISSING for this particular job. The moment the matcher notices a posting
wants AWS and the profile has none, AWS becomes actively forbidden for this run —
because the most dangerous hallucination is precisely the one that closes the gap
the model just noticed.

PROFILE-SPECIFIC RULES. Simon's profile encodes constraints a generic
implementation would never find, and they are enforced here rather than left to a
prompt:

* `never_mention` is a TOPIC block, not a phrase block. Nothing may state, imply
  or hint at work authorisation, visas, residency, nationality or the right to
  work — including where a posting asks directly. He answers that one himself.
* `evidence_private` skills (R, TypeScript, C#, Forecasting) are real and
  claimable in prose, but no PUBLIC project exists to cite for them, so a letter
  must never attach a named project to one.
* `not_my_work` names three specific attributions belonging to teammates.
* `never_claim_specifically` blocks reinforcement learning in the trading work —
  a conflation that invites "walk me through the reward function", which has no
  answer.
* `letter_policy: skills_row_only` is supported but currently unused; Simon
  superseded it on 24 Sep 2026. Re-adding the lines re-arms it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .models import Profile
from .validate import NUM_RE, validate

__all__ = [
    "Fact", "Violation", "TruthLayer", "build_truth",
    "CLAIM_VIOLATIONS", "POLICY_VIOLATIONS", "STYLE_VIOLATIONS", "NO_SURGERY",
    "INTEREST_FRAME",
    "severity", "blocks_sending",
]


#: `never_mention` entries are prose descriptions of a topic, not patterns. Each
#: key is a distinctive substring of the entry; the value is what actually gets
#: blocked. Curated rather than derived, because a topic block built by splitting
#: the sentence into words would match "work" and "need" and refuse everything.
TOPIC_PATTERNS: dict[str, list[str]] = {
    "work authorisation": [
        r"work\s+authoris\w*", r"work\s+authoriz\w*", r"authoris\w*\s+to\s+work",
        r"authoriz\w*\s+to\s+work", r"work\s+permit", r"working\s+permit",
        r"legally\s+(?:able|entitled|permitted)\s+to\s+work",
    ],
    "visa": [r"\bvisas?\b", r"\bsponsorship\b", r"\bsponsor(?:ing|ed)?\b", r"\btier\s*2\b"],
    "residence permit": [
        r"residence\s+permit", r"residency\s+status", r"resident\s+status",
        r"permanent\s+residen\w*", r"\bresidency\b",
    ],
    "nationality": [r"\bnationality\b", r"\bcitizenship\b", r"\bcitizen\b", r"\bpassport\b"],
    "right to work": [
        r"right\s+to\s+work", r"eligib\w*\s+to\s+work", r"eligibility\s+to\s+work",
    ],
}


#: Capitalised words that are ordinary English, not organisations. Without this
#: every sentence-initial word and every month reads as an unknown employer.
_NOT_AN_ENTITY = {
    "i", "a", "an", "the", "my", "me", "we", "our", "dear", "best", "kind",
    "regards", "sincerely", "yours", "thank", "thanks", "hi", "hello",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday",
    "msc", "bsc", "phd", "ma", "ba", "mba", "eu", "uk", "us", "usa",
    "at", "in", "on", "for", "to", "from", "with", "and", "but", "that",
    "this", "these", "those", "it", "its", "as", "is", "was", "были",
    "what", "when", "where", "while", "after", "before", "because", "so",
    "their", "they", "you", "your", "he", "she", "his", "her",
}

#: An entity worth checking: CamelCase, an acronym, or a capitalised run.
_ENTITY = re.compile(
    r"\b(?:[A-Z][a-z]+(?:[A-Z][a-z]+)+"      # CamelCase — Snowit, BeeSurfing, PySpark
    r"|[A-Z]{2,}(?:[-/][A-Z]{2,})*"            # acronyms — AWS, ETH, VLM
    r"|[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})+)"  # Title Case runs — Red Bull Racing
)


#: Capitalised words in the exemplars that are ordinary English, a month, or a
#: technical term the profile already licenses elsewhere. Anything not here and
#: not in the profile becomes a contaminant.
_COMMON_CAPITALISED = {
    "dear", "hiring", "manager", "team", "best", "regards", "sincerely", "kind",
    "yours", "thank", "thanks", "hello", "everyone", "however", "through",
    "before", "more", "recently", "most", "what", "while", "alongside", "the",
    "this", "that", "these", "those", "there", "from", "with", "after", "when",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "msc", "bsc", "phd", "python", "spark", "formula", "one",
    "over", "since", "also", "and", "but", "for", "was", "were",
    "his", "her", "their", "its", "not", "have", "has", "had", "can", "would",
}

#: Distinctive-entity patterns, used to turn a `not_my_work` sentence into
#: something that actually blocks a paraphrase.
_CAMEL = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b")           # GraphFrames
_ACRONYM = re.compile(r"\b[A-Z]{2,}(?:[-\w]+)?\b")                # EMA, BTC
_TITLE_RUN = re.compile(r"\b(?:[A-Z][a-z]+)(?:\s+[A-Z][a-z]+)+\b")  # Spark Structured Streaming


def derive_block_terms(claim: str, own_work: str) -> list[str]:
    """Blockable entities from a `not_my_work` sentence.

    `claim_to_block` is written for a human to read, and matching the whole
    sentence blocks nothing — a letter would paraphrase it and sail through. So
    pull out the distinctive entities instead, and drop any that ALSO describe
    Simon's own work: he really did contribute to Breakout Strategy V2 and he
    really does use EMAs, so neither token alone may be blocked. Where derivation
    cannot separate the two, the record carries explicit `block_terms`.
    """
    terms = set(_CAMEL.findall(claim)) | set(_ACRONYM.findall(claim)) | set(_TITLE_RUN.findall(claim))
    return sorted(t for t in terms if t.lower() not in own_work)


#: Splits "X in Y" claims into the technique and the places it may not be
#: claimed. Prose, so this is a documented heuristic rather than a parser.
_SPLIT = re.compile(r"\s+in\s+(?:the\s+)?", re.I)
_ALTERNATIVES = re.compile(r"\s*(?:,|/|\bor\b)\s*", re.I)
_LEADING_ARTICLE = re.compile(r"^(?:an?|the)\s+", re.I)


def derive_cooccurrence(claim: str) -> tuple[list[str], list[str]]:
    """Turn "RL in breakout-strategy-v1 or v2" into (techniques, contexts).

    `never_claim_specifically` blocks a technique IN A PLACE, not the technique
    anywhere: Simon really does have reinforcement-learning evidence — the sepsis
    project is strong and defensible — so blocking the phrase outright would
    delete a genuine strength. What must never be claimed is RL in the TRADING
    work, where the conflation invites "walk me through the reward function" and
    there is no answer. Hence a co-occurrence rule: both halves, same sentence.
    """
    parts = _SPLIT.split(claim, maxsplit=1)
    if len(parts) != 2:
        return [], []
    techniques = [
        _LEADING_ARTICLE.sub("", t).strip()
        for t in _ALTERNATIVES.split(parts[0]) if t.strip()
    ]
    contexts = [c.strip() for c in _ALTERNATIVES.split(parts[1]) if c.strip()]
    return [t for t in techniques if t], [c for c in contexts if c]


#: Past tenses of the gated verbs that are not formed by adding -ed.
_IRREGULAR_VERBS = {"led": "lead", "drove": "drive"}

#: Inflections of a gated verb that are ordinary English rather than a claim.
#: "autonomous driving" is a field and "this drives the decision" is a figure of
#: speech; neither says anything about seniority. "leads to" is the commonest
#: construction in technical prose. The past and the participle — drove, led,
#: leading — stay gated, because those really do claim something.
_NOT_A_CLAIM = {"driving", "drives", "leads"}


def verb_forms(verb: str) -> list[str]:
    """A gated verb and the inflections that mean the same thing.

    `forbidden.yaml` lists past tenses — "owned", "led" — and a letter that says
    "owns experiments from design to deployment" walks straight past a literal
    match. So the third person and the participle are added too.

    THE BARE STEM IS DELIBERATELY EXCLUDED. "own", "head", "direct" and "lead"
    are all ordinary English outside any seniority claim — "my own work", "ahead
    of the deadline", "direct experience", "lead time" — and blocking them would
    reject good sentences far more often than bad ones. The inflected forms
    carry the claim; the bare ones mostly do not.
    """
    lowered = verb.lower()
    stem = _IRREGULAR_VERBS.get(lowered)
    if stem is None and lowered.endswith("ed"):
        trimmed = lowered[:-2]
        # English drops a silent 'e' before '-ed', so "manage" becomes "managed"
        # by adding a 'd' alone. The letters below effectively require that 'e':
        # "manag" is not a word, "own" is.
        stem = lowered[:-1] if trimmed.endswith(("g", "c", "v", "s", "z", "u")) else trimmed
    stem = stem or lowered
    if not stem:
        return [verb]
    gerund = f"{stem[:-1]}ing" if stem.endswith("e") else f"{stem}ing"
    return sorted({lowered, f"{stem}s", gerund} - _NOT_A_CLAIM)


def bare_stem(verb: str) -> str:
    """The uninflected form, which `verb_forms` deliberately leaves out."""
    lowered = verb.lower()
    stem = _IRREGULAR_VERBS.get(lowered)
    if stem is None and lowered.endswith("ed"):
        trimmed = lowered[:-2]
        stem = lowered[:-1] if trimmed.endswith(("g", "c", "v", "s", "z", "u")) else trimmed
    return stem or lowered


#: A bare stem is only a seniority claim when something makes it a verb. "I own
#: the pipeline" is one; "my own harness", "lead time" and "ahead of schedule"
#: are not, and blocking those would reject good sentences constantly. So the
#: bare form is checked only after a subject or an infinitive marker.
_SUBJECT = r"(?:I|we|who|which|that|to|and|then|also|currently|now)\s+"


@dataclass(frozen=True)
class Fact:
    """One assertion the profile licenses, and the id that backs it."""

    id: str
    text: str
    kind: str  # bullet | variant | metric | evidence | education


@dataclass(frozen=True)
class Violation:
    code: str
    detail: str
    span: str = ""

    def __str__(self) -> str:
        span = f" — {self.span!r}" if self.span else ""
        return f"{self.code}: {self.detail}{span}"


# ── what blocks a letter, and what merely marks it ──
#
# Every gate used to be equal: one narrow no-break space in "1 000" failed the
# same test as a fabricated metric, and half of all runs ended with nothing on
# disk at all — not even the CV, which is deterministic and was already correct.
# That is not caution, it is a coin flip, and it cost Simon letters he could have
# sent.
#
# So the gates are classified once, here, and the shipping rule is stated in two
# sentences. A CLAIM or POLICY violation may never produce a sendable letter. A
# STYLE violation may never cost a letter.

#: The sentence the writer prompt MANDATES: "what draws you to this work". It
#: necessarily names the role's subject matter, so a forbidden term inside it is
#: an expression of interest rather than a claim of experience. Matched at the
#: start of a sentence only — "I have built RL environments and am drawn to more
#: of it" is a claim wearing the frame as a coat, and does not match.
INTEREST_FRAME = re.compile(
    r"^(?:what (?:draws|excites|attracts|appeals to|interests) me"
    r"|what i (?:find|like) most"
    r"|i(?:'m| am) drawn to"
    r"|the part (?:of this|that))\b",
    re.I,
)

#: The letter asserts something the profile does not support, or says something
#: untrue about the employer. These can never ship as a clean letter.
CLAIM_VIOLATIONS: frozenset[str] = frozenset({
    "NOT_MY_WORK", "FORBIDDEN_PAIRING", "CONTAMINATION", "FORBIDDEN_CLAIM",
    "INVENTED_LOCATION", "STALE_AVAILABILITY", "OVERCLAIM",
    "UNSUPPORTED_NUMBER", "PLACEHOLDER",
})

#: Nothing false, but a subject Simon answers himself and a letter may not raise.
#: Treated as strictly as a claim.
POLICY_VIOLATIONS: frozenset[str] = frozenset({"FORBIDDEN_TOPIC"})

#: Everything true but badly phrased, badly formatted, or merely graceless. Worth
#: fixing, never worth losing a letter over.
STYLE_VIOLATIONS: frozenset[str] = frozenset({
    "UNSUPPORTED_SENIORITY_VERB", "UNSTATED_AVAILABILITY", "SUMMARISING_CLOSE",
    "NO_GREETING", "NOT_SIGNED", "NUMBER_FORM", "UNSUPPORTED_YEAR",
    "BANNED_PHRASE", "STOCK_CONNECTOR",
    # Not a phrasing problem, but it belongs to the same pile for the same
    # reason: it must never cost the letter. See `TruthLayer.forbidden_hits`.
    "DOMAIN_IN_INTEREST",
})

#: Reported, never operated on. Surgery here would strip the role's own subject
#: matter out of the one sentence whose job is to name it, which is a worse
#: letter than the one that prompted the repair.
NO_SURGERY: frozenset[str] = frozenset({"DOMAIN_IN_INTEREST"})


def severity(code: str) -> str:
    """`"claim"`, `"policy"` or `"style"` for one violation code.

    AN UNRECOGNISED CODE IS A CLAIM. This is the most important line in the
    taxonomy and it is deliberately the default: a gate someone adds in six
    months blocks the letter until a human has decided which pile it belongs in.
    Defaulting the other way would let a new check silently ship.
    """
    if code in STYLE_VIOLATIONS:
        return "style"
    if code in POLICY_VIOLATIONS:
        return "policy"
    return "claim"


def blocks_sending(violations: Iterable[Violation]) -> list[Violation]:
    """The subset that may never reach a sendable letter."""
    return [v for v in violations if severity(v.code) != "style"]


@dataclass
class TruthLayer:
    """Everything sayable, and everything not. Built per job, per run."""

    allowed_facts: list[Fact] = field(default_factory=list)
    #: Printable in a CV skills row.
    allowed_skills_cv: set[str] = field(default_factory=set)
    #: Claimable as an elaborated statement in letter prose. A strict subset:
    #: one token in a comma-separated list is a smaller claim than a sentence.
    allowed_skills_letter: set[str] = field(default_factory=set)
    allowed_orgs: set[str] = field(default_factory=set)
    allowed_numbers: set[str] = field(default_factory=set)
    forbidden_claims: list[str] = field(default_factory=list)
    #: Topics that may not be raised at all, however phrased.
    forbidden_topics: list[str] = field(default_factory=list)
    gated_verbs: list[str] = field(default_factory=list)
    preferred_verbs: list[str] = field(default_factory=list)
    approved_low_phrasings: dict[str, str] = field(default_factory=dict)
    #: Skill names with no public project behind them. Claimable; not citable.
    no_project_citation: set[str] = field(default_factory=set)
    #: Attributions belonging to teammates, with the reason, so a rejection can
    #: explain itself rather than just refusing.
    not_my_work: list[dict] = field(default_factory=list)
    #: (term, record id, reason) — what blocks, and what to say when it fires.
    not_my_work_terms: list[tuple[str, str, str]] = field(default_factory=list)
    #: (techniques, contexts, original claim). Blocked only together.
    cooccurrence_blocks: list[tuple[list[str], list[str], str]] = field(default_factory=list)
    #: Names that are legitimate for THIS run but absent from the profile — the
    #: employer being written to, and the words of the role title.
    allowed_entities: set[str] = field(default_factory=set)
    #: Names present in the style exemplars but not in the profile. Blocked,
    #: because the exemplars are shown to the writer as prose to imitate and a
    #: model imitating prose will reuse its nouns.
    contaminants: set[str] = field(default_factory=set)
    #: Which job requirements drove the run-specific additions to forbidden_claims.
    missing_requirements: list[str] = field(default_factory=list)
    #: (term, evidence) — proposed as gaps, but the profile proves them. NOT
    #: forbidden. Surfaced so the disagreement is visible.
    contradicted_gaps: list[tuple[str, str]] = field(default_factory=list)
    #: Numeric tokens the POSTING states. Not an allowlist — see
    #: `unsupported_numbers`, which uses it only to tell an incidental year apart
    #: from a fabricated metric.
    posting_numbers: set[str] = field(default_factory=set)

    # ── the gates ──

    @staticmethod
    def normalise_numerals(text: str) -> str:
        """Fold typographic thousands separators so a figure is one token.

        A model writes "63\u202f000" — narrow no-break space, the European
        separator — and the gate then sees "63" and "000", neither of which is
        on the allowlist, and rejects a letter for correctly quoting the profile.
        Only the unambiguous separators are folded: a NARROW or NO-BREAK space
        between digits is never a word boundary, whereas an ordinary space
        between "2" and "533" might be two different numbers.
        """
        return re.sub(r"(?<=\d)[\u00a0\u2007\u2009\u202f](?=\d)", "", text or "")

    def unsupported_numbers(self, text: str) -> list[Violation]:
        """Every numeric token must match the profile corpus exactly.

        No rounding and no derivation: "over 99%" from 15,832→63 is a NEW number
        unless that string is itself in a metric record. Deriving one is where
        exaggeration creeps in, so software never does it.

        ONE CARVE-OUT, AND IT IS NOT A LOOPHOLE. A bare four-digit year that the
        POSTING states is reported as `UNSUPPORTED_YEAR` rather than
        `UNSUPPORTED_NUMBER`, which puts it in the style pile instead of the claim
        pile. The reason is that this gate is the one place where a fabricated
        metric and a harmless date look identical, and treating "the 2027 cohort"
        as a lie cost letters that were true.

        It cannot launder a figure. A metric is not a bare four-digit integer —
        63,000 has a comma, 0.90 has a point, 90% has a sign — and a year the
        posting never mentions is not in `posting_numbers`, so it still blocks.
        """
        out = []
        for number in NUM_RE.findall(self.normalise_numerals(text)):
            if number in self.allowed_numbers or number.replace(",", "") in self.allowed_numbers:
                continue
            if (
                number.isdigit() and len(number) == 4 and 1900 <= int(number) <= 2099
                and number in self.posting_numbers
            ):
                out.append(Violation(
                    "UNSUPPORTED_YEAR",
                    "the posting mentions this year but no profile record does — "
                    "drop it or name a date the profile carries",
                    number,
                ))
                continue
            out.append(Violation("UNSUPPORTED_NUMBER", "no profile record carries this figure", number))
        return out

    def forbidden_hits(self, text: str) -> list[Violation]:
        """Forbidden terms, split by whether the sentence CLAIMS one or WANTS one.

        THE GATE AND THE PROMPT USED TO CONTRADICT EACH OTHER. `05b_letter_write`
        requires a closing paragraph answering "what draws you to this work",
        which cannot be written without naming the role's subject matter — and
        this check then blocked the letter for naming it. Two of three blocked
        letters on 28 Sep 2026 were exactly that:

            "What excites me most about this role is the blend of research and
             engineering: designing new RL environments, ..."   -> 'RL environments'
            "...solve concrete problems in real estate and energy"  -> 'Real estate'

        Neither says he has done the thing. `factcheck._FIRST_PERSON` cannot tell
        them apart either, because both contain "me".

        So a term inside the attraction sentence is REPORTED AND DOWNGRADED, not
        excused: the letter ships as NEEDS_REVIEW with the sentence named, rather
        than as DO_NOT_SEND. A fabricated version — "What draws me to this role
        is my experience building RL environments" — still reaches Simon's eye
        here, and is a first-person claim the audit reads as well.
        """
        from .factcheck import split_sentences

        sentences = split_sentences(text)
        # Anything that is NOT the attraction sentence. A term appearing here is
        # a claim however it reads in the closing, so one mention outside still
        # blocks the letter.
        rest = " ".join(s for s in sentences if not INTEREST_FRAME.match(s.strip()))

        out = []
        for term in self.forbidden_claims:
            pattern = rf"\b{re.escape(term)}\b"
            # A short all-caps acronym is matched as written: "MS" (the degree a
            # posting asks for) is not "110 ms per race". Found 29 Sep 2026, when
            # exactly that sent a Waymo letter to DO_NOT_SEND.
            flags = 0 if _ACRONYM.fullmatch(term) else re.I
            if not re.search(pattern, text, flags):
                continue
            if not re.search(pattern, rest, flags):
                out.append(Violation(
                    "DOMAIN_IN_INTEREST",
                    f"{term} — named as what attracts you rather than as something "
                    f"you have done; read the sentence and decide",
                    term,
                ))
                continue
            why = (
                " (the posting asks for it and you do not have it)"
                if term in self.missing_requirements else ""
            )
            out.append(Violation("FORBIDDEN_CLAIM", f"{term}{why}", term))
        return out

    def topic_hits(self, text: str) -> list[Violation]:
        """Work authorisation and friends. A topic block, so paraphrase does not
        get around it — and it applies even when a posting raises the subject."""
        out = []
        for topic in self.forbidden_topics:
            for pattern in _patterns_for(topic):
                match = re.search(pattern, text, re.I)
                if match:
                    out.append(Violation(
                        "FORBIDDEN_TOPIC",
                        f"{topic} — never raise this, even if the posting does",
                        match.group(0),
                    ))
                    break
        return out

    def gated_verb_hits(self, text: str) -> list[Violation]:
        """A small model reaches for "led the development of" as filler. For a
        student and contract contributor it is a lie, and interviewers notice.

        A gated verb is allowed only where a bullet already uses it — which, for
        this profile, is nowhere.

        THE DETAIL NAMES ALTERNATIVES AND NEVER QUOTES THE LEMMA. It used to say
        `f"nothing in the profile says {verb!r}"`, where `verb` is the word in
        `forbidden.yaml` rather than the word the letter used. A letter wrote
        "own", the lemma is "owned", and the correction read *"you wrote 'to own':
        nothing in the profile says 'owned'"* — quoting a word the letter never
        used, naming no replacement. The writer had nowhere to go and failed three
        identical times, which cost the whole letter.

        `preferred_verbs` comes from `forbidden.yaml` under the comment "These
        verbs are fine and describe what you actually did", and until now it was
        computed on every run and read by nothing. The correction template already
        quotes `v.span`, so the detail's whole job is to say what to write instead.
        """
        backed = " ".join(f.text for f in self.allowed_facts).lower()
        instead = ", ".join(self.preferred_verbs[:8])
        out = []
        for verb in self.gated_verbs:
            patterns = [rf"\b{re.escape(f)}\b" for f in verb_forms(verb)]
            # The bare form, but only with a subject in front of it.
            patterns.append(rf"\b{_SUBJECT}{re.escape(bare_stem(verb))}\b")
            for pattern in patterns:
                match = re.search(pattern, text, re.I)
                if not match or re.search(pattern, backed, re.I):
                    continue
                out.append(Violation(
                    "UNSUPPORTED_SENIORITY_VERB",
                    "nothing in the profile records the candidate in that role — say "
                    "what he actually did"
                    + (f": {instead}" if instead else ""),
                    match.group(0).strip(),
                ))
                break
        return out

    def contamination_hits(self, text: str) -> list[Violation]:
        """A name the model picked up from an example rather than from the facts.

        WHY NOT A GENERAL "unknown entity" GATE. The spec asks for one, and the
        first attempt flagged "MSc", "COVID" and "NOVA IMS" — because deciding
        whether a capitalised word is an organisation is genuinely hard, and a
        blocking gate cannot afford to be wrong that often.

        The real risk is narrower and exactly identifiable. The writer is shown
        Simon's weaker letters as examples of what NOT to do, and a model shown
        any text will absorb its nouns. One of those letters names "Snowit" as
        an employer, and Snowit appears nowhere in `profile/`. So the gate
        checks the names that are actually in front of the model and absent from
        the profile — a short, exact list rather than a guess.
        """
        out = []
        for name in sorted(self.contaminants):
            # "Placement" is a contaminant from an old letter's job title, and
            # perfectly legitimate when THIS posting is also a placement.
            if name.lower() in self.allowed_entities:
                continue
            match = re.search(rf"\b{re.escape(name)}\b", text, re.I)
            if match:
                out.append(Violation(
                    "CONTAMINATION",
                    f"{name!r} appears in a letter shown to the model as an example, "
                    f"but nowhere in the profile — it must not be repeated as fact",
                    match.group(0),
                ))
        return out

    def not_my_work_hits(self, text: str) -> list[Violation]:
        """Work a teammate did. The reason travels with the rejection, because
        "this is not yours" is only useful alongside "here is what is"."""
        out = []
        for term, record_id, reason in self.not_my_work_terms:
            match = re.search(rf"\b{re.escape(term)}\b", text, re.I)
            if match:
                out.append(Violation("NOT_MY_WORK", f"{record_id} — {reason}", match.group(0)))
        return out

    def cooccurrence_hits(self, text: str) -> list[Violation]:
        """A technique claimed in a context where it did not happen.

        Blocking the technique outright would be wrong — the sepsis project is
        real reinforcement-learning evidence and one of the strongest things in
        the profile. It is the pairing that is false.
        """
        out = []
        for techniques, contexts, claim in self.cooccurrence_blocks:
            technique = next(
                (t for t in techniques if re.search(_loose(t), text, re.I)), None
            )
            if technique is None:
                continue
            context = next(
                (c for c in contexts if re.search(_loose(c), text, re.I)), None
            )
            if context is not None:
                out.append(Violation(
                    "FORBIDDEN_PAIRING",
                    f"{claim} — the technique is fine elsewhere, this pairing is not",
                    f"{technique} … {context}",
                ))
        return out

    def check(self, text: str) -> list[Violation]:
        """Every deterministic gate, in the order that makes failures readable."""
        return [
            *self.topic_hits(text),
            *self.not_my_work_hits(text),
            *self.cooccurrence_hits(text),
            *self.contamination_hits(text),
            *self.forbidden_hits(text),
            *self.gated_verb_hits(text),
            *self.unsupported_numbers(text),
        ]

    def fact(self, fact_id: str) -> Fact | None:
        return next((f for f in self.allowed_facts if f.id == fact_id), None)


#: Two to four capitals: MS, AWS, GCP, SQL — case is the meaning.
_ACRONYM = re.compile(r"[A-Z]{2,4}")


def _loose(phrase: str) -> str:
    """A multi-word phrase with room for an article wedged into it.

    "learned a policy" is the same claim as "learned policy", and a
    word-boundary match on the exact phrase sails straight past it. One
    intervening word is enough for the determiners and adjectives that actually
    show up, and few enough that unrelated words do not accidentally join up.
    """
    words = [re.escape(w) for w in phrase.split()]
    return r"\b" + r"\W+(?:\w+\W+)?".join(words) + r"\b"


def _profile_text(profile: Profile) -> str:
    """Everything written in `profile/`, flattened. Used only to decide which
    words are already the candidate's own."""
    import yaml

    return yaml.safe_dump(
        profile.model_dump(exclude={"style"}), allow_unicode=True, default_flow_style=False
    )


def _exemplar_contaminants(profile: Profile, truth: TruthLayer) -> set[str]:
    """Distinctive names in the style exemplars that the profile cannot back.

    Computed once per run from the exemplars themselves, so adding a letter to
    `style.yaml` automatically protects against whatever it happens to name.
    """
    # EVERY word the profile uses anywhere, not just org and skill names. A
    # first attempt built this from names alone and flagged "IMS", "RMSE",
    # "COVID" and "Science" — all of which appear in the education block, the
    # metric claims and the project titles. If a word occurs anywhere in the
    # profile, Simon has already licensed it.
    known = {w.lower() for w in re.findall(r"[A-Za-z0-9]+", _profile_text(profile))}

    corpus = " ".join(
        str(e.get("text", "")) for e in profile.style.exemplars if isinstance(e, dict)
    )
    # A WIDER NET THAN ELSEWHERE, and affordable because the corpus is small and
    # known: three letters, about a thousand words. Any capitalised word the
    # profile cannot account for is a candidate, so "Snowit" — one capital,
    # invisible to a CamelCase pattern — is caught alongside "IndraMind".
    #
    # THE LOWERCASE TEST is what keeps that from over-blocking. A real proper
    # noun is essentially never written in lower case, while an ordinary word
    # that happens to sit in a job title is. "Placement" comes from "Race
    # Strategy Student Placement" and looks exactly like a name — until you
    # notice the Alpine letter also says "the month the placement starts". That
    # one occurrence is the difference, and it is in the data rather than in a
    # list someone has to maintain.
    #
    # A real rejection came from this: a Waymo letter was refused for the word
    # "Placement".
    everyday = {
        m.group(0).lower()
        for m in re.finditer(r"\b[a-z][a-z0-9]{2,}\b", corpus + " " + _profile_text(profile))
    }
    out: set[str] = set()
    for match in re.finditer(r"\b[A-Z][A-Za-z0-9]{2,}\b", corpus):
        name = match.group(0)
        lowered = name.lower()
        if lowered in known or lowered in _COMMON_CAPITALISED or lowered in everyday:
            continue
        out.add(name)
    return out


def _patterns_for(topic: str) -> list[str]:
    """Regexes for one `never_mention` entry.

    Falls back to the entry's own distinctive words so a topic added to
    `forbidden.yaml` tomorrow is blocked today, if bluntly.
    """
    lowered = topic.lower()
    for key, patterns in TOPIC_PATTERNS.items():
        if key in lowered:
            return patterns
    words = [w for w in re.findall(r"[a-z]{4,}", lowered) if w not in {"that", "with", "need", "either", "their", "status"}]
    return [rf"\b{re.escape(w)}\b" for w in words[:3]] or [re.escape(lowered)]


def build_truth(
    profile: Profile,
    *,
    missing_requirements: list[str] | None = None,
    profile_dir=None,
    verify: bool = True,
    job: dict | None = None,
) -> TruthLayer:
    """Assemble the truth layer for one run.

    `missing_requirements` comes from the matcher. Passing it is what makes the
    layer job-specific rather than merely profile-specific.

    Every proposed gap is verified against the profile first, unless `verify` is
    off. Forbidding a requirement is a strong action — it gags the subject for
    the whole run — and a caller may be passing an inventory narrower than
    `profile/`, as `job_matches.missing_skills` is. A term the profile can prove
    is dropped from the list and recorded in `contradicted_gaps`.
    """
    from .matcher import verify_gaps

    proposed = list(missing_requirements or [])
    missing, contradicted = (
        verify_gaps(profile, proposed) if verify else (proposed, [])
    )
    truth = TruthLayer(missing_requirements=missing, contradicted_gaps=contradicted)

    # ── allowed facts: everything reachable from the profile ──
    for entry in profile.experience:
        truth.allowed_orgs.add(entry.org)
        for bullet in entry.bullets:
            truth.allowed_facts.append(Fact(bullet.id, bullet.text, "bullet"))
            for variant in bullet.variants:
                truth.allowed_facts.append(Fact(variant.id, variant.text, "variant"))
    for project in profile.projects:
        truth.allowed_orgs.add(project.name)
        for bullet in project.bullets:
            truth.allowed_facts.append(Fact(bullet.id, bullet.text, "bullet"))
    for metric in profile.metrics:
        truth.allowed_facts.append(Fact(metric.id, metric.claim, "metric"))
    for block in profile.evidence:
        for i, statement in enumerate(block.facts):
            truth.allowed_facts.append(Fact(f"{block.id}.f{i + 1}", statement, "evidence"))
    for school in profile.education:
        if institution := school.get("institution"):
            truth.allowed_orgs.add(institution)

    # ── skills, split by the size of claim each one licenses ──
    for skill in profile.skills:
        if skill.printable and skill.confidence != "none":
            truth.allowed_skills_cv.add(skill.name)
        if skill.letter_claimable:
            truth.allowed_skills_letter.add(skill.name)
        if skill.evidence_private:
            # Real work, but in a repository that is not published. The skill may
            # be claimed; naming a project for it would be an invention.
            truth.no_project_citation.add(skill.name)
        if skill.confidence == "low" and skill.low_phrasing:
            truth.approved_low_phrasings[skill.name] = skill.low_phrasing

    # ── numbers: derived from the profile, never hand-maintained ──
    truth.allowed_numbers = validate(profile_dir).allowed_numbers

    # ── forbidden ──
    forbidden = profile.forbidden
    claims: list[str] = [
        *forbidden.never_claim,
        *(s.name for s in profile.skills if s.confidence == "none"),
        *missing,
    ]
    # `never_claim_specifically` entries read "TECHNIQUE in PLACE", and matching
    # the whole sentence blocks nothing while blocking the technique alone would
    # delete real evidence. Both halves, or neither.
    for claim in forbidden.never_claim_specifically:
        techniques, contexts = derive_cooccurrence(claim)
        if techniques and contexts:
            truth.cooccurrence_blocks.append((techniques, contexts, claim))
        else:
            claims.append(claim)
    own_work = " ".join(f.text for f in truth.allowed_facts).lower()
    for record in forbidden.not_my_work:
        claim = record.get("claim_to_block") or ""
        if claim:
            claims.append(claim)
        # Explicit terms where the profile supplies them — the EMA record needs a
        # phrase, because both of its tokens describe Simon's own work too.
        terms = record.get("block_terms") or derive_block_terms(claim, own_work)
        reason = record.get("reason") or "this is a teammate's work"
        for term in terms:
            truth.not_my_work_terms.append((term, record.get("id", "?"), reason))
    truth.not_my_work = list(forbidden.not_my_work)

    # De-duplicated case-insensitively while keeping the declared order, so the
    # report reads the way forbidden.yaml is written.
    seen: set[str] = set()
    deduped: list[str] = []
    for claim in claims:
        if claim and claim.lower() not in seen:
            seen.add(claim.lower())
            deduped.append(claim)
    truth.forbidden_claims = deduped

    truth.forbidden_topics = list(forbidden.never_mention)
    # `personal.yaml` records the policy as well as the list, and the two must
    # agree — a policy with no entries would silently permit the topic.
    if profile.personal.work_authorization_policy == "never_mention" and not truth.forbidden_topics:
        truth.forbidden_topics = ["work authorisation"]

    # The employer being applied to, and the words of the role, are legitimate
    # names that the profile could not possibly contain.
    for value in (
        (job or {}).get("posting_company"), (job or {}).get("company_name"),
        (job or {}).get("title"), (job or {}).get("location"),
    ):
        for word in str(value or "").replace("/", " ").replace(",", " ").split():
            if word.strip():
                truth.allowed_entities.add(word.strip().lower())
        if value:
            truth.allowed_entities.add(str(value).strip().lower())

    # Numbers the POSTING itself states. Not an allowlist — a figure here is
    # still not something the profile can back. It exists so that an incidental
    # year the posting mentions ("the 2027 internship cohort") is classified as a
    # typographic slip rather than as a fabricated metric. See
    # `unsupported_numbers`.
    posting_text = " ".join(
        str((job or {}).get(key) or "")
        for key in ("title", "description", "location", "url")
    )
    truth.posting_numbers = set(NUM_RE.findall(TruthLayer.normalise_numerals(posting_text)))

    truth.contaminants = _exemplar_contaminants(profile, truth)
    truth.gated_verbs = list(forbidden.gated_verbs)
    truth.preferred_verbs = list(forbidden.preferred_verbs)
    return truth
