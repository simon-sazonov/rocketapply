"""Is this sentence saying something its source actually says?

Pure text, no network, no database, no model — and that is the reason this is its
own module rather than living in `research.py`. Two callers need it now: the
research screen, and the planner, which has to check that `company_angle` does not
assert something neither the notes nor the posting contain.

`research.py` reaches `enrich.base`, which imports `pitwall.db` and the search
chain. Importing it from the planner would drag the whole database layer into
letter writing, which is the thing `research_cache` was split out to prevent.
`extract.normalize` is pure `re` and `unicodedata`, so this is safe from anywhere.
"""

from __future__ import annotations

import re

from ..extract.normalize import squash

__all__ = ["is_claim_bearing", "ungrounded_terms", "_STEM"]

#: Ordinary long words that carry no claim, so their absence from the sources
#: proves nothing. Everything else of six characters or more must be traceable.
_GROUNDING_STOPWORDS: frozenset[str] = frozenset({
    "develops", "developing", "development", "building", "builds", "engineering",
    "engineers", "software", "platform", "platforms", "systems", "system",
    "technology", "technologies", "customers", "including", "company", "companys",
    "products", "product", "teams", "solutions", "designed", "designs", "provides",
    "focused", "working", "operates", "produces", "created", "creates", "based",
    "across", "between", "through", "within", "alongside", "research", "several",
})

#: How much of a word has to match a source word for the two to count as the
#: same claim. Six characters: enough that "partner" covers "partnering" and
#: "launch" covers "launched", short of letting an unrelated noun in.
_STEM = 6

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+.#-]*")
_NUM = re.compile(r"\d+(?:[.,]\d+)*")

#: Endings that mark a word as doing grammar rather than making a claim. A
#: sentence's verbs and plurals are how it is phrased; its nouns are what it
#: asserts, and only the assertions have to be in a source.
_INFLECTIONS: tuple[str, ...] = ("ing", "ed", "es", "ly", "s")


def is_claim_bearing(word: str) -> bool:
    """Does this word assert something, or is it just how the sentence reads?

    Grounding every long word was the first rule and it was wrong in a way that
    looked right: the first real six-company run rejected "The team publishes an
    open-source spec for orchestration called Symphony" for *"publishes"* and
    *"called"*, and threw away an entire employer over two ordinary verbs. The
    invented word in that sentence was "Symphony", and nothing about the rule
    knew the difference.

    So the test is what a word DOES, not how long it is:

    * capitalised away from the start of the sentence — a proper noun, which is
      the whole fabrication surface: Symphony, Twurst, Centaur, Germany;
    * carries a digit — HX-2, GPT-4, a version, a model name;
    * otherwise long enough to be specific AND not inflected, because "publishes"
      and "delivers" are grammar while "diffusion" and "orchestration" are
      claims.

    A plural common noun ("models", "networks") slips through as grammar. That is
    the deliberate cost: the claim in "discrete diffusion models" is carried by
    "diffusion", and that is the word this still catches.
    """
    plain = word.strip(".,;:")
    if not plain:
        return False
    if any(ch.isdigit() for ch in plain):
        return True
    lowered = squash(plain.lower())
    if len(lowered) < 6 or lowered in _GROUNDING_STOPWORDS:
        return False
    return not any(lowered.endswith(suffix) for suffix in _INFLECTIONS)


def ungrounded_terms(sentence: str, corpus: str) -> list[str]:
    """Words the sentence asserts that no source contains.

    The single strongest gate, and the one that kills the real fabrication: no
    kept snippet contained "diffusion", so "develops and deploys discrete
    diffusion models" cannot survive it.

    Checked against ONE source at a time — see `best_source`, which is where the
    real rule lives. Checking against the pooled corpus was the first version and
    it let through "the HX-2 was first launched from a coastal vessel in the
    waters off the U.S. coast", because "coast", "vessel" and "waters" each
    appeared on some page and the launch had actually happened off Plymouth. A
    sentence assembled from two pages is a recombination, and a recombination is
    how a true-sounding false claim gets made.

    MATCHED ON A STEM, not on the whole word. The first version compared exact
    forms and threw away a correct sentence for saying "partnering" where the
    source said "partner", and another for "launched" against "launch" — both
    rejections were the filter being wrong, not the model. A shared six-character
    prefix covers the inflections and is still far too narrow to let an invented
    noun through: "diffusion" needs a source word starting "diffus".
    """
    haystack = f" {squash(corpus)} "
    stems = {w[:_STEM] for w in haystack.split() if len(w) >= _STEM}
    words = _WORD.findall(sentence)
    out = []
    for position, word in enumerate(words):
        capitalised = position > 0 and word[:1].isupper()
        if not (capitalised or is_claim_bearing(word)):
            continue
        plain = squash(word.lower().strip(".,;:"))
        if not plain or plain in _GROUNDING_STOPWORDS:
            continue
        if f" {plain} " in haystack or (len(plain) >= _STEM and plain[:_STEM] in stems):
            continue
        out.append(word)
    return out
