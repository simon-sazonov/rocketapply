"""What a posting says about visa sponsorship — §22 of the spec.

Five states, and the first rule is that **silence is UNKNOWN**. It is never
converted into "no sponsorship", and nothing here ever presents an inference as
a fact: every non-UNKNOWN status carries the sentence it was read from, and the
UI shows that sentence.

    SPONSORSHIP_EXPLICIT  the posting says it sponsors
    NO_SPONSORSHIP        the posting says it does not, or that the right to
                          work must already be held
    LIKELY_SUPPORTED      the posting is silent, but the owner's own note on the
                          company says it sponsors — a lead, not a promise
    UNKNOWN               nothing either way
    NOT_APPLICABLE        decided later, per owner: the job is in a country they
                          can already work in (see `scoring.visa_fit`)

Relocation support is read separately (§60): "relocation package" is useful
and is not a visa.

Negative phrasings are matched first, because "we are unable to sponsor"
contains "sponsor". A posting that says both — "we sponsor visas … sponsorship
is not available for contractors" — is reported as UNKNOWN with both sentences:
contradictory text is a reason to read the posting, not to reject it (§90).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SPONSORSHIP_EXPLICIT = "SPONSORSHIP_EXPLICIT"
NO_SPONSORSHIP = "NO_SPONSORSHIP"
LIKELY_SUPPORTED = "LIKELY_SUPPORTED"
UNKNOWN = "UNKNOWN"
NOT_APPLICABLE = "NOT_APPLICABLE"

_NEGATIVE = [
    r"(?:unable|not able|cannot|can ?not|can't|do not|don't|does not|doesn't|will not|"
    r"won't|are not in a position|is not in a position|not currently)\s+(?:to\s+)?"
    r"(?:offer|provide|support|consider|accept)?\s*(?:any\s+)?(?:employment\s+|work\s+)?"
    r"(?:visa\s+|immigration\s+)?sponsor",
    r"\bno\s+(?:visa\s+|immigration\s+|work permit\s+)?sponsorship",
    r"\bno\s+(?:visa|immigration|work permit)\s+(?:support|assistance)",
    r"not\s+(?:be\s+)?(?:eligible|available)\s+for\s+(?:visa\s+|immigration\s+)?sponsorship",
    r"sponsorship\s+(?:will|can)\s+not\s+be\s+(?:provided|offered|available|considered)",
    r"sponsorship\s+(?:is\s+)?(?:not|unavailable|isn't)\s*(?:available|offered|provided|possible)?",
    r"without\s+(?:the\s+need\s+for\s+)?(?:current\s+or\s+future\s+|now\s+or\s+in\s+the\s+"
    r"future\s+)?(?:employer\s+|company\s+)?(?:visa\s+)?sponsorship",
    r"(?:must|need to|should)\s+(?:already\s+)?(?:have|hold|possess)\s+(?:the\s+|a\s+)?"
    r"(?:full\s+|existing\s+|valid\s+|unrestricted\s+|legal\s+)*(?:right\s+to\s+work|"
    r"work\s+(?:permit|authori[sz]ation|visa)|authori[sz]ation\s+to\s+work)",
    r"(?:must|need to)\s+be\s+(?:legally\s+)?(?:authori[sz]ed|eligible|permitted|entitled)\s+"
    r"to\s+work",
    r"(?:require|requires|requiring)\s+(?:a\s+|the\s+)?(?:valid\s+|existing\s+)?"
    r"(?:right\s+to\s+work|work\s+(?:permit|authori[sz]ation))",
    r"right\s+to\s+work\s+(?:in\s+[\w. ]{2,30}?\s+)?(?:is\s+)?(?:required|essential|"
    r"mandatory|needed|a\s+(?:must|requirement))",
    r"(?:valid\s+)?work\s+(?:permit|authori[sz]ation|visa)\s+(?:is\s+)?(?:required|"
    r"essential|mandatory|needed)",
    r"(?:us|u\.s\.)\s+citizens?(?:\s+or\s+(?:permanent\s+residents?|green\s+card\s+holders?))?"
    r"\s+only",
    r"only\s+(?:us|u\.s\.|eu|uk)\s+citizens",
    r"(?:active|current)\s+(?:security\s+)?clearance\s+(?:is\s+)?required",
    r"gültige\s+arbeitserlaubnis|arbeitserlaubnis\s+(?:für\s+\w+\s+)?(?:ist\s+)?"
    r"(?:erforderlich|voraussetzung|notwendig)",
    r"keine\s+(?:visums?)?(?:unterstützung|sponsoring)",
    r"autorisation\s+de\s+travail\s+(?:valide\s+)?(?:est\s+)?(?:requise|obligatoire)",
]
_POSITIVE = [
    r"(?:we|will|can|able to|happy to|do|does|glad to|willing to)\s+(?:also\s+)?"
    r"(?:offer\s+|provide\s+)?(?:visa\s+|work permit\s+)?sponsor",
    r"visa\s+sponsorship\s+(?:is\s+)?(?:available|provided|offered|possible)",
    r"sponsorship\s+(?:is\s+)?(?:available|provided|offered|possible)",
    r"(?:we\s+)?sponsor\s+(?:work\s+)?visas",
    r"(?:offer|provide|including|incl\.?|with)\s+(?:full\s+)?(?:visa|immigration|work permit)"
    r"\s+(?:support|sponsorship|assistance)",
    r"(?:visa|immigration|work permit)\s+(?:support|assistance)",
    r"support\s+(?:you\s+)?(?:with|for|in)\s+(?:your\s+|the\s+)?(?:visa|work permit|immigration)",
    r"(?:visa|immigration)\s+(?:and|&)\s+relocation\s+(?:support|assistance|package)",
    r"relocation\s+(?:and|&)\s+(?:visa|immigration)\s+(?:support|assistance)",
    r"unterstützung\s+(?:beim|bei\s+der|bei)\s+(?:visum|visa|aufenthaltstitel|arbeitserlaubnis)",
    r"(?:visum|visa)[- ]?(?:unterstützung|sponsoring)",
    r"blue card",
    r"accompagnement\s+(?:pour\s+)?(?:le\s+)?visa",
]
_RELOCATION = re.compile(
    r"relocation\s+(?:package|support|assistance|bonus|allowance|budget|help)|"
    r"(?:support|help|assist)\s+(?:you\s+)?(?:with|in)\s+(?:your\s+)?relocat|"
    r"relocation\s+(?:is\s+)?(?:provided|offered|covered|available)|"
    r"umzugs(?:kosten|unterstützung|hilfe|pauschale)|aide\s+(?:à|au)\s+(?:la\s+)?"
    r"(?:relocalisation|déménagement)",
    re.I,
)

_NEG_RE = [re.compile(p, re.I) for p in _NEGATIVE]

#: Requirements only a citizen or a cleared resident can meet: a refusal with
#: its country built in, whatever the rest of the posting says about visas.
#: Each was a job that reached the top of the list on 28 Sep 2026 and could not
#: be applied for: "Requirement: Singaporean ONLY, due to G50 security
#: clearance", "Clearance Required: TS/SCI … U.S. citizenship", "Data Engineer -
#: Active SC and NPPV3", "Data Engineer SC Cleared".
_CITIZENSHIP: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsingaporeans?\b(?:\s*(?:citizens?|/\s*PRs?|and\s+(?:singapore\s+)?PRs?))?"
                r"\s*only\b|\bsingapore\s+citizens?\s+(?:and\s+PRs?\s+)?only\b|"
                r"\bonly\s+(?:open\s+to\s+)?singaporeans?\b", re.I), "SG"),
    (re.compile(r"(?-i:U\.S\.|US)\s+[Cc]itizenship\b"
                r"(?!\s+(?:is\s+)?not\s+required|\s*(?:required)?\s*:\s*(?:no|not)\b)|"
                r"\bmust\s+be\s+(?:a\s+)?(?-i:U\.S\.|US)\s+citizen|\bTS\s*/\s*SCI\b|"
                r"\btop\s+secret\b[^.\n]{0,30}\bclearance\b", re.I), "US"),
    (re.compile(r"\b(?:active\s+)?(?:SC|DV)\s+(?:cleared|clearance)\b|\bNPPV\s?3\b|"
                r"\b(?:british|UK)\s+(?:citizens?|nationals?)\s+only\b", re.I), "GB"),
)
_POS_RE = [re.compile(p, re.I) for p in _POSITIVE]

#: A sentence about US work authorisation, for the "authorized to work in the
#: US" case — it only matters where the job is.
#: Case-sensitive on the abbreviations: "join us" is not the United States.
_COUNTRY_SCOPE = {
    "US": re.compile(r"\b(?:USA|US)\b|\bU\.S\.|(?i:\bunited states\b)"),
    "GB": re.compile(r"\bUK\b|\bU\.K\.|(?i:\b(?:united kingdom|britain)\b)"),
}


def _scope_for(sentences: list[str]) -> str | None:
    """The one place a refusal is about: ISO2, `EU`, or None when unnamed."""
    from .locations import COUNTRIES

    for code, scope_re in _COUNTRY_SCOPE.items():
        if any(scope_re.search(n) for n in sentences):
            return code
    joined = " ".join(sentences)
    if re.search(r"\b(?:EU|EEA)\b|(?i:\beuropean union\b)", joined):
        return "EU"
    lowered = joined.lower()
    for code, (name, aliases) in COUNTRIES.items():
        for word in (name.lower(), *aliases):
            if len(word) >= 4 and re.search(rf"\b{re.escape(word)}\b", lowered):
                return code
    return None


@dataclass(frozen=True)
class Visa:
    status: str = UNKNOWN
    evidence: str | None = None
    relocation: bool = False
    #: ISO2 (or `EU`) when a negative sentence names a place ("authorized to
    #: work in the US") — lets the filter apply it only to jobs there.
    scope: str | None = None


#: A sentence ends at . ! ? or a newline — but not at the dots of "U.S." or
#: "e.g.", whose period follows a single letter.
_BOUNDARY = re.compile(r"(?<!\b[A-Za-z])\.(?=\s|$)|[!?](?=\s|$)|\n")
_BOUNDARY_SPLIT = re.compile(r"(?<=[^\W\d_][^\W\d_]\.)\s+|(?<=[!?])\s+|\n+")


def _sentence(text: str, start: int, end: int) -> str:
    left = 0
    for found in _BOUNDARY.finditer(text, 0, start):
        left = found.end()
    after = _BOUNDARY.search(text, end)
    right = after.end() if after else len(text)
    sentence = " ".join(text[left:right].split())
    return sentence if len(sentence) <= 260 else sentence[:259] + "…"


#: Words without which no visa pattern can match. Only sentences containing one
#: are searched: the patterns are long alternations, and running thirty of them
#: over every word of a 5 KB posting was a second's work per 250 postings.
_TRIGGER = re.compile(
    r"sponsor|visa|visum|authori[sz]|permit|right to work|citizen|clearance|"
    r"eligible to work|entitled to work|permitted to work|arbeitserlaubnis|aufenthalt|"
    r"autorisation|blue card|immigration|relocat|umzug|déménagement|relocalisation",
    re.I,
)


def _relevant_text(text: str) -> str:
    """Only the sentences that could say anything about visas, in order."""
    if not _TRIGGER.search(text):
        return ""
    keep: list[str] = []
    for m in _BOUNDARY_SPLIT.split(text):
        if m and _TRIGGER.search(m):
            keep.append(m.strip())
    return "\n".join(keep)


def extract(description: str | None, *, company_sponsorship: str | None = None) -> Visa:
    """The visa status one posting states, with the sentence that states it.

    `company_sponsorship` is the owner's own note from target_companies.csv
    (`yes|sometimes|no|unknown`). It can only lift UNKNOWN to LIKELY_SUPPORTED —
    what the posting itself says always wins over what the owner remembers.
    """
    text = _relevant_text(description or "")
    relocation = bool(_RELOCATION.search(text))

    for pattern, country in _CITIZENSHIP:
        hit = pattern.search(description or "")
        if hit:
            return Visa(NO_SPONSORSHIP, _sentence(description or "", hit.start(), hit.end()),
                        relocation, country)

    negatives: list[str] = []
    for pattern in _NEG_RE:
        for m in pattern.finditer(text):
            sentence = _sentence(text, m.start(), m.end())
            if sentence not in negatives:
                negatives.append(sentence)
    positives = [
        _sentence(text, m.start(), m.end()) for m in (r.search(text) for r in _POS_RE) if m
    ]
    positives = [p for p in dict.fromkeys(positives) if p not in negatives]
    if negatives and positives:
        return Visa(
            UNKNOWN, "mixed signals — " + " … ".join([positives[0], negatives[0]]),
            relocation,
        )
    if negatives:
        # "Must be authorized to work in the US. We cannot sponsor." — the
        # country is in one sentence and the refusal in the other, so the
        # scope is read across all of them.
        return Visa(NO_SPONSORSHIP, " … ".join(negatives[:2]), relocation,
                    _scope_for(negatives))

    if positives:
        return Visa(SPONSORSHIP_EXPLICIT, positives[0], relocation)

    note = (company_sponsorship or "").strip().lower()
    if note in {"yes", "sometimes"}:
        return Visa(
            LIKELY_SUPPORTED,
            "the posting says nothing; your company note says it "
            + ("sponsors" if note == "yes" else "sometimes sponsors"),
            relocation,
        )
    return Visa(UNKNOWN, None, relocation)
