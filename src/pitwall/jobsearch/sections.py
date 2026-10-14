"""Which part of a posting is a requirement and which is a wish.

"Kubernetes is a plus" and "you have shipped Kubernetes in production" name
the same skill with very different weight. Scoring treated them alike, so a
posting with a long "nice to have" list looked like a poor fit, when those are
exactly the skills an employer expects a graduate not to have yet.

Two signals, each cheap and each honest about being a heuristic:

* **Headings.** A short line such as "Nice to have", "Bonus points", "Von
  Vorteil" or "Atouts" starts a wish section. It runs until the next
  heading-like line.
* **Sentences.** Outside those sections, a sentence that says "a plus",
  "ideally", "preferred" or "wünschenswert" is a wish.

What is left is the core text. A skill named anywhere in the core is
required, even if a wish sentence also names it.
"""

from __future__ import annotations

import re

_WISH_WORDS = (
    r"nice[- ]to[- ]haves?|good[- ]to[- ]haves?|bonus(?: points)?|(?:is |are )?a (?:big |huge )?plus"
    r"|pluspunkte?|preferred(?: qualifications| skills| experience)?|desirable|desired"
    r"|ideally|advantageous|an advantage|extra credit|would be (?:great|nice|beneficial)"
    r"|stand out|differentiate your candidacy|set you apart|even better if|extra points"
    r"|von vorteil|wünschenswert|idealerweise|atouts?|un plus|serait un plus"
    r"|souhaité\w*|será valorizad\w*|valoriza(?:do|da|mos)?|é um plus|es un plus"
    r"|se valorará|gradito|costituisce un plus|pré-requisitos desejáveis|meriterende"
    r"|meriterande|meriterende|pluspoint"
)

_WISH_HEAD = re.compile(
    rf"^\s*(?:[#*•\-–]\s*)?(?:{_WISH_WORDS})\b[^\n]{{0,40}}:?\s*$|"
    # A heading with the wish inside it: "Skills that will differentiate your
    # candidacy", "What would make you stand out". Not a bullet, no full stop.
    rf"^\s*(?![•\-–*])[^\n.!?]{{0,45}}\b(?:{_WISH_WORDS})\b[^\n.!?]{{0,30}}:?\s*$", re.I | re.M)

#: Any short line that reads as a heading ends a wish section.
_ANY_HEAD = re.compile(
    r"^\s*(?:[#*]+\s*)?[A-ZÀ-Ý][^\n.!?]{2,60}:?\s*$|^\s*(?:[#*]+\s*)?[^\n.!?]{2,60}:\s*$",
    re.M)

_WISH_SENTENCE = re.compile(rf"\b(?:{_WISH_WORDS})\b", re.I)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")


def split_requirements(text: str | None, *, by_sentence: bool = True) -> tuple[str, str]:
    """(core, wishes). Pure. Everything in `text` lands in exactly one.

    `by_sentence=False` leaves every sentence outside a wish section in the
    core: the experience reader judges a wish word inside a sentence itself,
    because "5+ years of experience, ideally in fintech" still asks for five."""
    if not text:
        return "", ""
    lines = text.splitlines()
    core_lines: list[str] = []
    wish_lines: list[str] = []
    in_wish = False
    for line in lines:
        if _WISH_HEAD.match(line):
            in_wish = True
            wish_lines.append(line)
            continue
        if in_wish and _ANY_HEAD.match(line) and not line.lstrip().startswith(("•", "-", "*")):
            in_wish = False
        (wish_lines if in_wish else core_lines).append(line)
    if not by_sentence:
        return "\n".join(core_lines), "\n".join(wish_lines)

    core_sentences: list[str] = []
    for sentence in _SENTENCE_SPLIT.split("\n".join(core_lines)):
        if _WISH_SENTENCE.search(sentence):
            wish_lines.append(sentence)
        elif sentence.strip():
            core_sentences.append(sentence)
    return "\n".join(core_sentences), "\n".join(wish_lines)
