"""How much experience a posting asks for — §15 of the spec.

Read from the title AND the description, because they disagree more often than
anyone would like: "Junior Data Scientist" over a body that says "3+ years of
professional experience" is common, and §4.1 is explicit that the body wins.

Three rules the rest of the system relies on:

* **No explicit number means unknown**, never "0" and never a rejection. A
  sentence like "experience with ML projects preferred" has no years in it.
* **A number must sit next to the word experience** (in English, German or
  French). "Founded 20 years ago" and "a 5-year roadmap" are not requirements.
* **"Preferred" years are soft.** "3+ years is a plus" is kept as evidence but
  `required=False`, so it lowers the score instead of filtering the job out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15,
    "ein": 1, "einem": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "funf": 5,
    "un": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
}
_NUM = r"(\d{1,2}(?:[.,]5)?|" + "|".join(_WORD_NUMBERS) + r")"
_YEARS = r"(?:years?|yrs?|jahre?n?|ans|année?s?)"
_EXPERIENCE_WORD = re.compile(
    r"experience|expérience|experiencia|esperienza|erfahrung|berufserfahrung|"
    r"track record|in (?:industry|a similar role|a relevant role)|professional|"
    r"hands-on|working (?:in|with|on|as)",
    re.I,
)
_PREFERRED = re.compile(
    r"prefer|nice[- ]to[- ]have|is a plus|a plus|bonus|ideally|desirable|"
    r"advantage|wünschenswert|von vorteil|idealerweise|un plus|souhaitable|optional",
    re.I,
)

#: (pattern, kind) — kind decides how the groups map onto min/max.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 0-2 years, 1 to 3 years, 2–4 Jahre, 3-5 ans
    (re.compile(rf"\b{_NUM}\s*(?:-|–|—|to|bis|à)\s*{_NUM}\+?\s*{_YEARS}\b", re.I), "range"),
    # at least 3 years, minimum of 3 years, mindestens 3 Jahre, au moins 3 ans
    (re.compile(
        rf"\b(?:at least|a minimum of|minimum of|minimum|min\.?|no less than|"
        rf"mindestens|au moins|minimum de)\s+{_NUM}\+?\s*{_YEARS}\b", re.I), "min"),
    # 3+ years, 3 + years
    (re.compile(rf"\b{_NUM}\s*\+\s*{_YEARS}\b", re.I), "min"),
    # 3 or more years, more than 3 years, over 3 years
    (re.compile(rf"\b{_NUM}\s*(?:or more|and above|or above)\s*{_YEARS}\b", re.I), "min"),
    (re.compile(rf"\b(?:more than|over|mehr als|über|plus de)\s+{_NUM}\s*{_YEARS}\b", re.I),
     "over"),
    # up to 2 years, less than 2 years, max 2 years
    (re.compile(rf"\b(?:up to|less than|under|max(?:imum)?\.?|bis zu|jusqu'à)\s+{_NUM}\s*"
                rf"{_YEARS}\b", re.I), "max"),
    # 3 years of experience, 3 years' experience, 3 Jahre Berufserfahrung
    (re.compile(rf"\b{_NUM}\s*{_YEARS}(?:'|’)?\s+(?:of\s+)?(?:\w+\s+){{0,3}}?"
                rf"(?:experience|erfahrung|berufserfahrung|d'expérience|expérience)", re.I),
     "exact"),
    # experience of 3 years
    (re.compile(rf"experience of\s+{_NUM}\s*{_YEARS}\b", re.I), "exact"),
)

#: Requirements with no number that still say "not your first job". German
#: postings rarely give years ("mehrjährige Berufserfahrung"), and frontier-lab
#: postings often refuse to ("years of experience will correlate with the
#: internal job level") while asking for a track record, deep expertise, or
#: mentoring. Scored down as `mid`; never filtered — the filter needs a number.
_QUALITATIVE_MID = re.compile(
    r"mehrjährige\w*\s+(?:berufs)?erfahrung|langjährige\w*\s+(?:berufs)?erfahrung|"
    r"several years of|many years of|multiple years of|extensive experience|"
    r"(?:strong |proven |demonstrated |established )?track record (?:of|in)\b|"
    r"(?:significant|substantial|considerable|deep) (?:industry |professional |hands-on )?"
    r"(?:experience|expertise)|solide berufserfahrung|plusieurs années d'expérience|"
    r"mentor(?:ing|ed)? (?:junior|other|more junior|less experienced)|technical leadership|"
    r"(?:you have|you've) led\b|experience leading (?:a |teams|projects|engineers)",
    re.I,
)

_ENTRY_DESC = re.compile(
    r"\b(?:new grad(?:uate)?s?|recent(?:ly)? graduat\w*|graduate programme|graduate program|"
    r"entry[- ]level|early[- ]career|no (?:prior |previous )?(?:professional )?experience "
    r"(?:is )?(?:required|necessary)|0\s*(?:-|–|to)\s*[12]\s*years|berufseinsteiger\w*|"
    r"absolvent\w*|jeune diplômé\w*|junior profile|first job)\b",
    re.I,
)

# Title vocabulary. Order matters: the most specific level wins.
_TITLE_INTERN = re.compile(
    r"\b(?:intern(?:ship)?|praktikum|praktikant\w*|werkstudent\w*|working student|"
    r"stagiaire|stage|alternan\w*|apprentice\w*|trainee|thesis|masterarbeit|"
    r"student|placement|co-?op)\b", re.I)
_TITLE_ENTRY = re.compile(
    r"\b(?:graduate|grad|new grad|entry[- ]level|junior|jr\.?|early[- ]career|"
    r"associate|einsteiger|berufseinsteiger\w*|absolvent\w*|débutant|"
    r"level (?:i|1)|l1)\b", re.I)
_TITLE_SENIOR = re.compile(
    r"\b(?:senior|sr\.?|staff|principal|lead|head|director|manager|vp|chief|"
    r"distinguished|expert|architect|level (?:iii|iv|3|4|5)|l[4-7])\b", re.I)
_TITLE_MID = re.compile(r"\b(?:mid[- ]?level|intermediate|level (?:ii|2)|l[23])\b", re.I)

#: US ladder suffixes after the role noun: "Machine Learning Engineer II",
#: "Data Scientist IV", "Machine Learning Engineer 5 (IC)". I is entry, III
#: mid, IV and up senior. II is left to the body: it means one to three years
#: at some employers and two to four at others, and "entry level" would
#: overclaim. The first live runs on Adzuna US and Jooble brought hundreds of
#: these, and none read as a level.
_TITLE_LADDER = re.compile(
    r"\b(?:engineer|scientist|analyst|developer|researcher|specialist|statistician)"
    r"\s*,?\s*(?:level\s*)?(i{1,3}|iv|v|vi|[1-6])(?![\w.])", re.I)
_LADDER = {"i": "entry", "1": "entry", "ii": "unknown", "2": "unknown", "iii": "mid", "3": "mid",
           "iv": "senior", "4": "senior", "v": "senior", "5": "senior", "vi": "senior",
           "6": "senior"}

#: What the Arbeitnow / SmartRecruiters level tags say, normalised.
_LEVEL_TAGS = {
    "entry": "entry", "entry level": "entry", "entry_level": "entry",
    "berufseinsteiger": "entry", "graduate": "entry", "junior": "entry",
    "intern": "intern", "internship": "intern", "student": "intern",
    "working student": "intern", "werkstudent": "intern",
    "mid": "mid", "mid_senior_level": "mid", "mid-senior level": "mid",
    "experienced": "mid", "professional / experienced": "mid", "berufserfahren": "mid",
    "senior": "senior", "senior_level": "senior", "executive": "senior", "director": "senior",
    # The boards added in the source expansion (Sep 2026): Himalayas, DevJobs,
    # Jobicy. MyCareersFuture's grades are mapped by its adapter instead.
    "entry-level": "entry", "mid-level": "mid", "regular": "mid", "midweight": "mid",
    "lead": "senior", "principal": "senior", "manager": "senior",
}


@dataclass(frozen=True)
class Experience:
    min_years: float | None = None
    max_years: float | None = None
    #: intern | entry | mid | senior | unknown
    level: str = "unknown"
    #: False when every explicit number sat in a "preferred / a plus" sentence.
    required: bool = False
    #: 0-1. An explicit, required number is high; a title word alone is low.
    confidence: float = 0.0
    evidence: str | None = None

    @property
    def known(self) -> bool:
        return self.min_years is not None or self.level != "unknown"


def _to_num(text: str) -> float:
    text = text.lower().replace(",", ".")
    return float(_WORD_NUMBERS[text]) if text in _WORD_NUMBERS else float(text)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?;•])\s+|\n+|\s[-•·]\s", text)
    return [p.strip() for p in parts if p and p.strip()]


def _years_mentions(text: str) -> list[tuple[float, float | None, bool, str]]:
    """Every explicit years requirement: (min, max, preferred, sentence)."""
    found: list[tuple[float, float | None, bool, str]] = []
    for sentence in _sentences(text):
        if not re.search(_YEARS, sentence, re.I):
            continue
        if not _EXPERIENCE_WORD.search(sentence):
            continue
        # Company history and product timelines are not requirements.
        if re.search(r"\b(?:founded|since|history|roadmap|anniversary|old|"
                     r"warranty|contract (?:of|for)|fixed[- ]term|befristet|"
                     r"we have been|for over \d+ years we)\b", sentence, re.I) \
                and not re.search(r"experience|erfahrung|expérience", sentence, re.I):
            continue
        preferred = bool(_PREFERRED.search(sentence))
        for pattern, kind in _PATTERNS:
            for m in pattern.finditer(sentence):
                try:
                    if kind == "range":
                        lo, hi = _to_num(m.group(1)), _to_num(m.group(2))
                        if hi < lo:
                            lo, hi = hi, lo
                        found.append((lo, hi, preferred, sentence))
                    elif kind == "min" or kind == "over":
                        found.append((_to_num(m.group(1)), None, preferred, sentence))
                    elif kind == "max":
                        found.append((0.0, _to_num(m.group(1)), preferred, sentence))
                    else:
                        n = _to_num(m.group(1))
                        found.append((n, None, preferred, sentence))
                except (KeyError, ValueError):
                    continue
            # One interpretation per sentence: the first pattern family that
            # matched is the most specific (a range beats the "3 years" inside it).
            if found and found[-1][3] == sentence:
                break
    # Implausible values are parsing accidents (a year, a salary band).
    return [f for f in found if 0 <= f[0] <= 25]


def title_level(title: str | None) -> str:
    """The level a title claims on its own — before the body is read."""
    if not title:
        return "unknown"
    # A flat title at several AI labs, not a level — "staff" here is not Staff.
    title = re.sub(r"member of (?:the )?technical staff", " ", title, flags=re.I)
    if _TITLE_INTERN.search(title):
        return "intern"
    if _TITLE_SENIOR.search(title):
        return "senior"
    ladder = _TITLE_LADDER.search(title)
    if ladder and _LADDER[ladder.group(1).lower()] != "unknown":
        return _LADDER[ladder.group(1).lower()]
    if _TITLE_ENTRY.search(title):
        return "entry"
    if _TITLE_MID.search(title):
        return "mid"
    return "unknown"


def level_from_tags(tags: list[str] | None) -> str:
    levels = [_LEVEL_TAGS.get(str(t).strip().lower()) for t in tags or []]
    order = ("senior", "mid", "entry", "intern")
    for level in order:
        if level in levels:
            return level
    return "unknown"


def extract(title: str | None, description: str | None,
            tags: list[str] | None = None) -> Experience:
    """The experience requirement of one posting."""
    text = description or ""
    mentions = _years_mentions(text)
    required = [m for m in mentions if not m[2]]
    t_level = title_level(title)
    tag_level = level_from_tags(tags)

    if required:
        # The binding requirement is the largest minimum stated as required:
        # "2+ years of Python and 5+ years in industry" asks for five.
        best = max(required, key=lambda m: m[0])
        lo, hi, _, sentence = best
        level = _level_for_years(lo, hi)
        return Experience(
            min_years=lo, max_years=hi, level=level, required=True,
            confidence=0.9, evidence=_clip(sentence),
        )
    if mentions:
        best = min(mentions, key=lambda m: m[0])
        lo, hi, _, sentence = best
        return Experience(
            min_years=lo, max_years=hi,
            level=t_level if t_level != "unknown" else _level_for_years(lo, hi),
            required=False, confidence=0.4, evidence=_clip(sentence),
        )

    entry = _ENTRY_DESC.search(text)
    if t_level in {"intern", "entry"} or entry:
        level = t_level if t_level in {"intern", "entry"} else "entry"
        evidence = f"title: {title}" if t_level in {"intern", "entry"} else _clip(
            _sentence_around(text, entry.start()) if entry else "")
        return Experience(level=level, confidence=0.6 if entry else 0.5, evidence=evidence)
    qualitative = _QUALITATIVE_MID.search(text)
    if qualitative:
        # "mehrjährige Berufserfahrung" — several years, no number. Scored down,
        # never filtered: the filter only acts on a number.
        return Experience(
            level="mid", confidence=0.5,
            evidence=_clip(_sentence_around(text, qualitative.start())),
        )
    if t_level != "unknown":
        return Experience(level=t_level, confidence=0.5, evidence=f"title: {title}")
    if tag_level != "unknown":
        return Experience(level=tag_level, confidence=0.4, evidence=f"tag: {tag_level}")
    return Experience()


def _level_for_years(lo: float, hi: float | None) -> str:
    if lo <= 0 and (hi is None or hi <= 2):
        return "entry"
    if lo <= 2:
        return "entry" if (hi is not None and hi <= 3) or lo <= 1 else "mid"
    if lo <= 4:
        return "mid"
    return "senior"


def _sentence_around(text: str, index: int) -> str:
    start = max(text.rfind(".", 0, index), text.rfind("\n", 0, index)) + 1
    end_candidates = [i for i in (text.find(".", index), text.find("\n", index)) if i != -1]
    end = min(end_candidates) if end_candidates else len(text)
    return text[start:end].strip()


def _clip(text: str, limit: int = 220) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
