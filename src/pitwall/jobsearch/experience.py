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
  `required=False`, so it lowers the score instead of filtering the job out —
  but only when the softener governs the years: "5+ years in calibration,
  ideally on diesel engines" asks for five years; the "ideally" is about the
  engines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .sections import split_requirements

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15,
    "ein": 1, "einem": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "funf": 5,
    "un": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
}
_NUM = r"(\d{1,2}(?:[.,]5)?|" + "|".join(_WORD_NUMBERS) + r")"
# "YOE" is "years of experience" in one word: "5+ YOE outside of internships".
_YEARS = r"(?:years?|yrs?|yoe|jahre?n?|ans|année?s?)"
_EXPERIENCE_WORD = re.compile(
    r"experience|expérience|experiencia|esperienza|erfahrung|berufserfahrung|"
    r"track record|in (?:industry|a similar role|a relevant role)|professional|"
    r"hands-on|working (?:in|with|on|as)|\byoe\b",
    re.I,
)
_PREFERRED = re.compile(
    r"prefer|nice[- ]to[- ]have|is a plus|a plus|bonus|ideally|desirable|"
    r"advantage|wünschenswert|von vorteil|idealerweise|un plus|souhaitable|optional",
    re.I,
)

#: (pattern, kind) — kind decides how the groups map onto min/max. Every
#: family is read in every sentence; where two overlap, the earlier (more
#: specific) one keeps the span — a range beats the "3 years" inside it.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 0-2 years, 1 to 3 years, 2–4 Jahre, 3-5 ans
    (re.compile(rf"\b{_NUM}\s*(?:-|–|—|to|bis|à)\s*{_NUM}\+?\s*{_YEARS}\b", re.I), "range"),
    # at least 3 years, minimum of 3 years, mindestens 3 Jahre, au moins 3 ans
    (re.compile(
        rf"\b(?:at least|a minimum of|minimum of|minimum|min\.?|no less than|"
        rf"mindestens|au moins|minimum de)\s+{_NUM}\+?\s*{_YEARS}\b", re.I), "min"),
    # 3+ years, 3 + years
    (re.compile(rf"\b{_NUM}\s*\+\s*{_YEARS}\b", re.I), "min"),
    # "Bachelors + 8 years or Masters + 6 years" (Stripe), "+3 years of …"
    (re.compile(rf"(?:\+|\bplus)\s*{_NUM}\s*{_YEARS}\b", re.I), "min"),
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
_TITLE_MID = re.compile(r"\b(?:mid[- ]?level|medior|intermediate|level (?:ii|2)|l[23])\b", re.I)

#: A level the body states outright rather than implies: "We are seeking a
#: Staff level Engineer" (Anthropic), "We are hiring Senior and Staff ML
#: Engineers" (Wayve), "suited to engineers operating at Senior or Staff
#: level" (Mistral), "open to Senior and Principal level candidates", "Mentor
#: senior engineers". Each was listed on 29 Sep 2026 with an unknown level.
_ROLE_NOUN = (r"(?:engineers?|scientists?|developers?|researchers?|analysts?|architects?|"
              r"candidates?|hires?|professionals?|roles?|positions?)")
_STATED_SENIOR = re.compile(
    rf"\b(?:hiring|seeking|looking\s+for|recruiting|requires?|searching\s+for)\s+(?:an?\s+)?"
    rf"(?:(?:highly\s+)?experienced\s+)?(?:senior|staff|principal|lead)"
    rf"(?:\s*(?:/|and|or|&)\s*(?:senior|staff|principal|lead))?(?:[- ]level)?\s+"
    rf"(?:[\w&/+.-]+\s+){{0,3}}?{_ROLE_NOUN}\b|"
    rf"\b(?:senior|staff|principal)[- ]level\s+(?:[\w&/+.-]+\s+){{0,2}}?{_ROLE_NOUN}\b|"
    rf"\bopen\s+to\s+(?:senior|staff|principal)\b|"
    rf"\boperating\s+at\s+(?:a\s+)?(?:senior|staff|principal)\b|"
    rf"\bmentor(?:ing)?\s+(?:other\s+)?senior\s+engineers\b",
    re.I)
#: "Mid-level AI engineer" opening the posting, "As a Medior Data & AI
#: Engineer you…" (Atos, 29 Sep 2026).
_STATED_MID = re.compile(
    rf"\b(?:hiring|seeking|looking\s+for|recruiting|requires?|as)\s+(?:an?\s+)?"
    rf"(?:mid[- ]?level|medior|intermediate)\s+(?:[\w&/+.-]+\s+){{0,3}}?{_ROLE_NOUN}\b|"
    rf"\A\W*(?:mid[- ]?level|medior)\b", re.I)

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


_YEARS_RE = re.compile(_YEARS, re.I)

#: The company's history, not the candidate's: "a global leader … with nearly
#: 20 years of experience" (InfoCepts) and "the leading Spanish company in the
#: IT sector, with more than 25 years of experience" (Arsys) were both
#: rejected on 29 Sep 2026 for asking 20 and 25 years.
_HISTORY = re.compile(
    r"\bfounded\b|\bsince\s+(?:19|20)\d\d\b|\banniversary\b|\bwe\s+have\s+been\b|"
    r"\bfor\s+over\s+\d+\s+years,?\s+we\b", re.I)
#: Words that address the applicant, which a company's history never does.
_APPLICANT = re.compile(
    r"\b(?:you|your|candidates?|applicants?|required|requirements?|minimum|must|should|"
    r"need|bring|looking\s+for|seeking|ideal)\b", re.I)
#: "…with nearly 20 years of experience" is history when a company is what
#: has them, and a requirement when a person is.
_WITH_YEARS = re.compile(r"\b(?:with|has|have|brings?|boasts?)\s+(?:nearly|almost|over|"
                         r"more\s+than|about|around|close\s+to|some)?\s*$", re.I)
_COMPANY_NOUN = re.compile(
    r"\b(?:leader|leading|company|firm|group|organi[sz]ation|business|provider|"
    r"consultancy|agency|studio|brand|institution|bank|we|our|us)\b", re.I)
_PERSON_NOUN = re.compile(
    r"\b(?:you|your|engineers?|scientists?|developers?|candidates?|someone|professionals?|"
    r"individuals?|person|practitioners?|analysts?|researchers?)\b", re.I)


def _company_history(before: str) -> bool:
    """Is the years phrase that follows `before` a company's record? The
    nearer noun decides: "…join the leading Spanish company in the IT sector,
    with more than 25 years" is the company's; "We need an engineer with 5+
    years" is the engineer's."""
    if not _WITH_YEARS.search(before):
        return False
    context = before[-120:]
    company = [m.end() for m in _COMPANY_NOUN.finditer(context)]
    person = [m.end() for m in _PERSON_NOUN.finditer(context)]
    return bool(company) and (not person or company[-1] > person[-1])

#: What a years phrase may not be about: an age, a contract, a degree, a plan.
_NOT_TENURE_AFTER = re.compile(
    r"^\W*(?:old|of\s+age|ago|warranty|guarantee|contract|fixed|term|period|programm?e|"
    r"degree|course|bachelor|master|phd|studies|study|plan|roadmap|strategy|lease|"
    r"assignment|mission|duration|horizon|budget|later|alt\b|betriebszugehörigkeit)", re.I)
_NOT_TENURE_BEFORE = re.compile(
    r"\b(?:within|after|next|every|nach|ab|après|dans|since|aged?|age\s+of|term\s+of|"
    r"period\s+of)\s*$", re.I)
#: With no "experience" in the sentence, the phrase has to lead into work:
#: "5+ years building and shipping production software", "6-10 yrs software
#: engineering" — both missed before 29 Sep 2026.
_WORK_AFTER = re.compile(
    r"^\W*(?:of\s+)?(?:[\w-]+\s+){0,2}?(?:in|as|at|on|with|building|working|developing|"
    r"designing|shipping|delivering|writing|running|leading|managing|doing|using|deploying|"
    r"programming|coding|software|engineering|data|machine|ml|ai|python|sql|analytics|"
    r"research|production|industry|industrial|professional|commercial|relevant|related|"
    r"hands|backend|cloud|devops|deep|quantitative|statistical|modell?ing|consulting|"
    r"development|full[- ]?stack|infrastructure|platform)\b", re.I)
#: Where a softener stops governing: a comma, a dash, "and", "or".
_CLAUSE_END = re.compile(r"[,;:()\[\]–—]|\s-\s|\b(?:and|but|or|while|whereas)\b", re.I)
#: A sentence that ends on a softener softens all of it: "…with more than 2
#: years of experience in data analysis, data mining, or ML projects preferred".
_SOFT_TAIL = re.compile(
    r"\b(?:preferred|preferable|a\s+plus|desirable|advantageous|an\s+advantage|"
    r"nice\s+to\s+have|bonus|welcome|von\s+vorteil|wünschenswert|un\s+plus|souhaitable)"
    r"\W*$", re.I)


def _softened(before: str, after: str) -> bool:
    """Does a softener govern the years between `before` and `after`?

    "Ideally 3+ years", "Preferably 5+ years", "Nice to have: 3+ years" (it
    comes first in the clause) and "5+ years is a plus" (it follows before the
    clause ends). Not "5+ years in calibration – ideally on diesel engines",
    where it qualifies something else; that sentence used to make five years
    optional, and the job passed (Hitachi, OpenAI, Stripe; 29 Sep 2026)."""
    head = re.split(r"[;.]", before)[-1][-40:]
    if _PREFERRED.search(head):
        return True
    end = _CLAUSE_END.search(after)
    return bool(_PREFERRED.search(after[: end.start()] if end else after))


def _years_mentions(text: str) -> list[tuple[float, float | None, bool, str]]:
    """Every explicit years requirement: (min, max, preferred, sentence).

    Every pattern family is read in every sentence, and the largest required
    minimum decides later — "4+ years of ML, including at least 2 years in
    computer vision" asks for four. It used to be read as two: the first
    family to match a sentence ended the search (Skylum, 29 Sep 2026).
    """
    found: list[tuple[float, float | None, bool, str]] = []
    # Headings only: a "Nice to have" section softens everything under it. A
    # wish word inside a sentence is judged by `_softened` instead.
    wishes = " ".join(split_requirements(text, by_sentence=False)[1].split())
    for sentence in _sentences(text):
        if not _YEARS_RE.search(sentence):
            continue
        if _HISTORY.search(sentence) and not _APPLICANT.search(sentence):
            continue
        worded = bool(_EXPERIENCE_WORD.search(sentence))
        in_wishes = bool(wishes) and " ".join(sentence.split()) in wishes
        soft_tail = bool(_SOFT_TAIL.search(sentence))
        spans: list[tuple[int, int]] = []
        for pattern, kind in _PATTERNS:
            for m in pattern.finditer(sentence):
                if any(m.start() < end and start < m.end() for start, end in spans):
                    continue
                before, after = sentence[:m.start()], sentence[m.end():]
                if _NOT_TENURE_AFTER.search(after) or _NOT_TENURE_BEFORE.search(before):
                    continue
                if _company_history(before):
                    continue
                if not worded and not _WORK_AFTER.search(after):
                    continue
                try:
                    if kind == "range":
                        lo, hi = _to_num(m.group(1)), _to_num(m.group(2))
                        if hi < lo:
                            lo, hi = hi, lo
                    elif kind == "max":
                        lo, hi = 0.0, _to_num(m.group(1))
                    else:
                        lo, hi = _to_num(m.group(1)), None
                except (KeyError, ValueError):
                    continue
                spans.append((m.start(), m.end()))
                preferred = in_wishes or soft_tail or _softened(before, after)
                found.append((lo, hi, preferred, sentence))
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
    # A level the title or the body states outright. Evidence that starts
    # "title:" or "stated:" is what the seniority rule acts on (§ filters).
    if t_level != "unknown":
        return Experience(level=t_level, confidence=0.5, evidence=f"title: {title}")
    for pattern, level in ((_STATED_SENIOR, "senior"), (_STATED_MID, "mid")):
        stated = pattern.search(text)
        if stated:
            return Experience(level=level, confidence=0.6, evidence="stated: " + _clip(
                _sentence_around(text, stated.start())))
    qualitative = _QUALITATIVE_MID.search(text)
    if qualitative:
        # "mehrjährige Berufserfahrung" — several years, no number. Scored down,
        # never filtered: the filter only acts on a number or a stated level.
        return Experience(
            level="mid", confidence=0.5,
            evidence=_clip(_sentence_around(text, qualitative.start())),
        )
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
