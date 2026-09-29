"""How much a posting reads like the owner's CV: TF-IDF cosine, local, pure Python.

The skill vocabulary matches named things ("PyTorch", "SQL"). It cannot see
that a posting about "demand forecasting for fleet operations" is close to a
CV about "time-series models for race strategy", when the two share words but
no listed skill. This component is that second opinion: the posting's words,
weighted by how rare they are across every stored posting, compared with the
CV's words.

It reports a PERCENTILE, not the raw cosine. Raw cosines between a CV and job
ads sit between about 0.02 and 0.3, and depend on text length and language. The
rank among today's postings is what means something ("closer to your CV than
92% of what is open").

The CV text is `config/my_profile.md` plus the narrative parts of `profile/`:
experience, projects, evidence, education, summaries. Contact details
(`personal.yaml`) and the style and layout files are never read. Nothing
leaves the machine.
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

PROFILE_YAML = ("experience.yaml", "projects.yaml", "evidence.yaml", "education.yaml",
                "summaries.yaml")

_TOKEN = re.compile(r"[a-zà-ÿ][a-zà-ÿ0-9+#]{2,}")

#: Function words, plus the words every job ad uses and so tell nothing apart.
STOPWORDS = frozenset(["the", "and", "for", "with", "you", "your", "our", "are", "will", "this", "that", "have", "from", "who", "what", "when", "where", "which", "their", "they", "them", "its", "into", "about", "over", "also", "more", "most", "such", "than", "then", "there", "these", "those", "been", "being", "can", "could", "should", "would", "may", "might", "must", "not", "but", "all", "any", "each", "other", "some", "very", "able", "ability", "work", "working", "team", "teams", "role", "roles", "job", "jobs", "company", "join", "help", "build", "building", "strong", "good", "great", "new", "including", "include", "across", "within", "based", "well", "using", "use", "used", "make", "making", "part", "year", "years", "experience", "experienced", "skills", "skill", "knowledge", "opportunity", "opportunities", "environment", "looking", "candidate", "candidates", "position", "apply", "please", "required", "requirements", "preferred", "plus", "benefits", "offer", "offers", "equal", "employer", "diversity", "inclusion", "etc", "per", "via", "one", "two", "three", "day", "days", "time", "full", "like", "just", "und", "der", "die", "das", "mit", "für", "wir", "sie", "ist", "eine", "ein", "auf", "von", "zu", "bei", "den", "dem", "oder", "auch", "sich", "les", "des", "une", "pour", "avec", "nous", "vous", "est", "dans", "sur"])


def tokens(text: str | None) -> list[str]:
    return [t for t in _TOKEN.findall((text or "").lower()) if t not in STOPWORDS]


def _strings(node: Any) -> Iterable[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def profile_text(config_dir: Path, profile_dir: Path) -> str:
    """The CV as one text, from the files above. Empty if none exist."""
    parts: list[str] = []
    cv = config_dir / "my_profile.md"
    if cv.exists():
        text = cv.read_text(encoding="utf-8")
        if not text.lstrip().startswith("<!-- template"):
            parts.append(text)
    for name in PROFILE_YAML:
        path = profile_dir / name
        if path.exists():
            try:
                parts.extend(_strings(yaml.safe_load(path.read_text(encoding="utf-8"))))
            except yaml.YAMLError:
                continue
    return "\n".join(parts)


class CVSimilarity:
    """Built once per scoring pass over the postings being scored."""

    def __init__(self, cv: str, documents: Iterable[str]) -> None:
        df: Counter[str] = Counter()
        n = 0
        for doc in documents:
            df.update(set(tokens(doc)))
            n += 1
        self.n = max(1, n)
        self.idf = {term: math.log((1 + self.n) / (1 + count)) + 1.0 for term, count in df.items()}
        self.cv = self._vector(tokens(cv))
        self.cv_norm = math.sqrt(sum(v * v for v in self.cv.values())) or 1.0

    def _vector(self, words: list[str]) -> dict[str, float]:
        counts = Counter(words)
        default = math.log(1 + self.n) + 1.0     # a word no posting uses: rarest
        return {t: (1 + math.log(c)) * self.idf.get(t, default) for t, c in counts.items()}

    def cosine(self, text: str | None) -> float:
        vec = self._vector(tokens(text))
        if not vec or not self.cv:
            return 0.0
        dot = sum(w * self.cv[t] for t, w in vec.items() if t in self.cv)
        norm = math.sqrt(sum(w * w for w in vec.values())) or 1.0
        return dot / (norm * self.cv_norm)

    def rank(self, cosines: list[float]) -> list[float]:
        """Each cosine as its percentile among all of them, 0-1."""
        ordered = sorted(cosines)
        total = len(ordered) or 1
        return [bisect_left(ordered, c) / total for c in cosines]
