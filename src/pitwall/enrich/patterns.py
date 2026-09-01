"""Address-format inference and application — §2.3 of the v3 brief.

The idea the whole free engine turns on: *you do not need the target person's
address to help the target person — you need any two addresses at their
company, because two addresses reveal the format.*

**Sample size is the confidence signal.** §2.3 is explicit — "a pattern from one
address is a guess, from three is close to fact. Never present n=1 and n=5
identically." So this module's job is not really to guess a format; it is to
report honestly how much is behind the guess, and `PatternGuess.sample_size` is
the number the status ladder in §4 reads.

One rule that shapes everything here: **a sample only counts toward
`sample_size` if it was matched against a real person's name.** An address
harvested without a name — `j.doe@alpine.com` on a contact page with no context
— tells you the *shape* (`initial.word`) but cannot distinguish `f.last` from
`f.first`, and counting two of those as "two samples agree" would promote a
guess to `probable` on evidence that never existed. Shape-only samples are kept,
recorded, and used to break ties; they never raise the count.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .base import EmailCandidate, EnrichmentContext, StepOutcome
from .checks import domain_of, is_free_provider, is_role_address, local_part

log = logging.getLogger(__name__)

#: Particles that belong to a surname but are dropped in most mail formats.
#: "Max van der Berg" is `max.vanderberg@`, `mvanderberg@` or `max.berg@`
#: depending on the company, so both readings are generated and whichever one
#: the samples support wins.
NAME_PARTICLES: frozenset[str] = frozenset(
    {
        "van", "von", "der", "den", "de", "del", "della", "di", "da", "dos",
        "das", "du", "la", "le", "les", "el", "al", "bin", "ibn", "ter", "te",
        "op", "aan", "mac", "mc", "st", "saint",
    }
)

#: Honorifics and suffixes that are not part of a name.
NAME_NOISE: frozenset[str] = frozenset(
    {
        "mr", "mrs", "ms", "miss", "dr", "prof", "professor", "sir", "dame",
        "eng", "ing", "phd", "msc", "bsc", "meng", "beng", "mba", "cend",
        "jr", "sr", "ii", "iii", "iv", "cv", "cta", "mimeche", "ceng",
    }
)


# ─────────────────────────────── the name ────────────────────────────────


@dataclass(frozen=True)
class NameParts:
    """A person's name reduced to the pieces a mail format is built from."""

    first: str
    last: str
    middle: tuple[str, ...] = ()
    #: The surname with its particles attached — `vanderberg` for "van der
    #: Berg". `last` holds the bare final word. Companies use both.
    last_with_particles: str = ""
    raw: str = ""

    @property
    def fi(self) -> str:
        return self.first[:1]

    @property
    def li(self) -> str:
        return self.last[:1]

    @property
    def mi(self) -> str:
        return self.middle[0][:1] if self.middle else ""

    def surnames(self) -> tuple[str, ...]:
        """Both readings of the surname, commonest first, deduped."""
        out = [self.last]
        if self.last_with_particles and self.last_with_particles != self.last:
            out.append(self.last_with_particles)
        return tuple(dict.fromkeys(o for o in out if o))


def _strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def split_name(full_name: str | None) -> NameParts | None:
    """"Dr. Jean-Pierre van der Berg III" -> first `jeanpierre`, last `berg`.

    Returns None for anything that is not usably a person's name — a single
    word, an empty string, a headline that ended up in the name column. A
    format applied to half a name produces a plausible address for nobody.
    """
    if not full_name:
        return None
    text = _strip_accents(str(full_name))
    # Drop anything parenthesised or after a separator: LinkedIn names arrive
    # as "Jane Doe | Aerodynamicist" and "Jane Doe (she/her)" often enough that
    # not handling it would poison a third of the corpus.
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.split(r"[|,·•/–—]| - ", text)[0]
    text = re.sub(r"[^A-Za-z'\-\s]", " ", text)

    words = [w.strip("-'").lower() for w in text.split()]
    words = [w for w in words if w and w.strip(".") not in NAME_NOISE]
    if len(words) < 2:
        return None

    first = words[0]
    rest = words[1:]
    # Trailing particles belong to the surname, not to the middle names.
    tail_start = len(rest)
    for i in range(len(rest) - 1, -1, -1):
        if rest[i] in NAME_PARTICLES:
            tail_start = i
        elif i < tail_start - 1:
            break
    surname_words = rest[tail_start:] if tail_start < len(rest) else rest[-1:]
    middle = tuple(rest[: len(rest) - len(surname_words)])

    last = surname_words[-1].replace("-", "").replace("'", "")
    joined = "".join(surname_words).replace("-", "").replace("'", "")
    first_clean = first.replace("-", "").replace("'", "")
    if not first_clean or not last:
        return None
    return NameParts(
        first=first_clean,
        last=last,
        middle=tuple(m.replace("-", "").replace("'", "") for m in middle if m),
        last_with_particles=joined,
        raw=str(full_name).strip(),
    )


# ────────────────────────────── the formats ──────────────────────────────

#: The standard set, **ordered by real-world frequency**. The order is load
#: bearing in exactly one place: when two formats explain the same evidence
#: equally well, the commoner one wins. It is never used to invent confidence —
#: a tie broken this way is still reported at its true sample size.
FORMATS: tuple[str, ...] = (
    "first.last",       # jane.doe
    "flast",            # jdoe
    "first",            # jane
    "firstlast",        # janedoe
    "first_last",       # jane_doe
    "f.last",           # j.doe
    "firstl",           # janed
    "last.first",       # doe.jane
    "lastf",            # doej
    "first-last",       # jane-doe
    "lastfirst",        # doejane
    "last_first",       # doe_jane
    "last",             # doe
    "f_last",           # j_doe
    "first.l",          # jane.d
    "fl",               # jd
    "f.l",              # j.d
    "firstmlast",       # janeadoe
    "first.middle.last",
)

_BUILDERS: dict[str, Callable[[NameParts, str], str]] = {
    "first.last": lambda n, s: f"{n.first}.{s}",
    "flast": lambda n, s: f"{n.fi}{s}",
    "first": lambda n, s: n.first,
    "firstlast": lambda n, s: f"{n.first}{s}",
    "first_last": lambda n, s: f"{n.first}_{s}",
    "f.last": lambda n, s: f"{n.fi}.{s}",
    "firstl": lambda n, s: f"{n.first}{s[:1]}",
    "last.first": lambda n, s: f"{s}.{n.first}",
    "lastf": lambda n, s: f"{s}{n.fi}",
    "first-last": lambda n, s: f"{n.first}-{s}",
    "lastfirst": lambda n, s: f"{s}{n.first}",
    "last_first": lambda n, s: f"{s}_{n.first}",
    "last": lambda n, s: s,
    "f_last": lambda n, s: f"{n.fi}_{s}",
    "first.l": lambda n, s: f"{n.first}.{s[:1]}",
    "fl": lambda n, s: f"{n.fi}{s[:1]}",
    "f.l": lambda n, s: f"{n.fi}.{s[:1]}",
    "firstmlast": lambda n, s: f"{n.first}{n.mi}{s}",
    "first.middle.last": lambda n, s: f"{n.first}.{n.mi}.{s}" if n.mi else "",
}

#: Formats that carry so little of the name that they collide constantly.
#: `fl` gives `jd` for both Jane Doe and John Davies, and applying it produces
#: an address that is as likely to reach a stranger as the person you meant.
#: Inferable from evidence, never *offered* as a generic permutation.
LOW_INFORMATION_FORMATS: frozenset[str] = frozenset({"fl", "f.l", "first", "last"})

#: What a `not_found` person gets offered when the domain has no pattern at
#: all: §4's "generic permutation", which is a `guess` and labelled as one.
GENERIC_PERMUTATIONS: tuple[str, ...] = ("first.last", "flast", "firstlast", "f.last")


def format_names() -> tuple[str, ...]:
    return FORMATS


def apply_format(pattern: str, name: NameParts, domain: str) -> str | None:
    """Build the address this format implies. None if it cannot be built."""
    builder = _BUILDERS.get(pattern)
    if builder is None or not domain:
        return None
    local = builder(name, name.last).strip(" .-_")
    if not local:
        return None
    return f"{local}@{domain}".lower()


def all_locals(pattern: str, name: NameParts) -> list[str]:
    """Every local part this format could produce, across surname readings."""
    builder = _BUILDERS.get(pattern)
    if builder is None:
        return []
    out: list[str] = []
    for surname in name.surnames():
        local = builder(name, surname).strip(" .-_")
        if local and local not in out:
            out.append(local)
    return out


def formats_explaining(email_local: str, name: NameParts) -> list[str]:
    """Every format that turns this name into this local part.

    More than one is normal — for a person whose first and last names begin
    with the same letter, or a single-syllable surname, several formats collide.
    All of them are returned and the caller aggregates across samples, which is
    exactly how a second sample resolves what the first could not.
    """
    target = (email_local or "").strip().lower()
    # A trailing digit is a disambiguator the company added (`j.doe2@`), not a
    # different format. Strip it before comparing, or a real sample is lost.
    target = re.sub(r"\d+$", "", target)
    if not target:
        return []
    return [fmt for fmt in FORMATS if target in all_locals(fmt, name)]


# ──────────────────────────────── shapes ─────────────────────────────────


def shape_of(email_or_local: str) -> str:
    """`j.doe` -> `i.w`, `jane.doe` -> `w.w`, `jdoe` -> `w`.

    `i` is a single initial, `w` a word, and the separators are kept. This is
    all an address with no known owner can tell you — and it is genuinely
    useful, because it rules formats out even when it cannot rule one in.
    """
    local = local_part(email_or_local) if "@" in email_or_local else (email_or_local or "").lower()
    local = re.sub(r"\d+$", "", local.strip())
    if not local:
        return ""
    parts = re.split(r"([._\-])", local)
    out: list[str] = []
    for part in parts:
        if part in "._-":
            out.append(part)
        elif len(part) == 1:
            out.append("i")
        elif part:
            out.append("w")
    return "".join(out)


def formats_with_shape(shape: str) -> list[str]:
    """Which formats could produce this shape, for a typical two-part name."""
    probe = NameParts(first="jane", last="doe", middle=("ann",), last_with_particles="doe")
    out: list[str] = []
    for fmt in FORMATS:
        locals_ = all_locals(fmt, probe)
        if any(shape_of(local) == shape for local in locals_):
            out.append(fmt)
    return out


# ───────────────────────────── the inference ─────────────────────────────


@dataclass
class PatternGuess:
    """What the corpus says the company's format is, and how much is behind it."""

    pattern: str | None
    #: Samples matched against a real person's NAME that support this pattern.
    #: The number §4's status ladder reads. Shape-only samples never raise it.
    sample_size: int = 0
    #: Supporting named samples / named samples that could be matched at all.
    agreement: float | None = None
    #: Other formats the same evidence would also explain.
    alternatives: list[str] = field(default_factory=list)
    #: Samples whose owner is unknown but whose shape is consistent. Recorded
    #: for the owner to see; never counted as confidence.
    shape_support: int = 0
    #: Samples that were usable but contradict the winning pattern.
    conflicts: int = 0
    #: Every address considered, including the ones no format explained.
    examined: int = 0

    @property
    def is_confident(self) -> bool:
        """Deliberately has no default threshold — the caller passes the
        configured `enrich_min_samples_for_probable` to `meets`."""
        return self.sample_size >= 2

    def meets(self, minimum: int) -> bool:
        return bool(self.pattern) and self.sample_size >= minimum

    def as_evidence(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pattern": self.pattern,
            "sample_size": self.sample_size,
        }
        if self.agreement is not None:
            out["pattern_agreement"] = round(self.agreement, 3)
        if self.alternatives:
            out["pattern_alternatives"] = self.alternatives[:3]
        if self.shape_support:
            out["shape_only_samples"] = self.shape_support
        if self.conflicts:
            out["conflicting_samples"] = self.conflicts
        return out

    def describe(self) -> str:
        """The sentence the UI shows. Always carries the sample size."""
        if not self.pattern:
            return "no address format could be worked out"
        n = self.sample_size
        if n == 0:
            return f"{self.pattern} — a standard format, nothing at this company confirms it"
        if n == 1:
            return f"{self.pattern} — from a single known address, so this is a guess"
        return f"{self.pattern} — {n} known addresses at this company agree"


def infer_pattern(
    samples: Sequence[dict[str, Any] | tuple[str, str | None]],
    *,
    known_people: Sequence[dict[str, Any]] | None = None,
) -> PatternGuess:
    """Work out the company's address format from its known addresses.

    `samples` are `domain_address_samples` rows (or `(email, person_name)`
    pairs). `known_people` are candidates already in the database at this
    company: an address harvested without a name can often be matched to one of
    them, which turns a shape-only sample into a real one — free, and using
    evidence that was collected independently, so it is not circular.
    """
    named: list[tuple[str, NameParts]] = []
    unnamed: list[str] = []
    examined = 0

    people: list[tuple[dict[str, Any], NameParts]] = []
    for person in known_people or []:
        parts = split_name(person.get("full_name"))
        if parts is not None:
            people.append((person, parts))

    for sample in samples:
        email, person_name = _as_sample(sample)
        if not email:
            continue
        examined += 1
        # Role addresses are structural, not personal: `info@` would "match"
        # the `first` format for anyone called Info. §8 requires them flagged
        # and excluded, and letting one into the corpus corrupts the inference.
        if is_role_address(email):
            continue
        local = local_part(email)
        parts = split_name(person_name)
        if parts is None:
            parts = _match_to_known_person(local, people)
        if parts is not None:
            named.append((local, parts))
        else:
            unnamed.append(local)

    votes: Counter[str] = Counter()
    matched = 0
    for local, parts in named:
        explaining = formats_explaining(local, parts)
        if explaining:
            matched += 1
            votes.update(explaining)

    if not votes:
        return _shape_only_guess(unnamed, examined)

    top = max(votes.values())
    winners = [fmt for fmt in FORMATS if votes.get(fmt, 0) == top]

    # Break a tie on the shape the unnamed samples show, before falling back to
    # frequency order. Two `w.w` addresses cannot say whether the company uses
    # first.last or last.first, but five more of the same shape at least
    # confirm they are not using `flast`.
    shape_counts = Counter(shape_of(local) for local in unnamed if shape_of(local))
    if len(winners) > 1 and shape_counts:
        preferred = [
            fmt for fmt in winners
            if any(shape_of(local) in shape_counts for local in all_locals(fmt, _PROBE))
        ]
        if preferred:
            winners = preferred

    pattern = winners[0]
    supporting_shape = sum(
        n for shape, n in shape_counts.items()
        if shape in {shape_of(local) for local in all_locals(pattern, _PROBE)}
    )
    return PatternGuess(
        pattern=pattern,
        sample_size=top,
        agreement=(top / matched) if matched else None,
        alternatives=list(winners[1:]) + [
            f for f, v in votes.most_common() if v < top and f not in winners
        ][:2],
        shape_support=supporting_shape,
        conflicts=max(0, matched - top),
        examined=examined,
    )


_PROBE = NameParts(first="jane", last="doe", middle=("ann",), last_with_particles="doe")


def _as_sample(sample: Any) -> tuple[str, str | None]:
    if isinstance(sample, dict):
        return str(sample.get("email") or "").lower(), sample.get("person_name")
    email, person_name = sample
    return str(email or "").lower(), person_name


def _match_to_known_person(
    local: str, people: Sequence[tuple[dict[str, Any], NameParts]]
) -> NameParts | None:
    """Can this nameless address be attributed to somebody we already have?

    Only when exactly ONE known person explains it. Two people it could belong
    to means it identifies neither, and picking one would invent a sample.
    """
    hits = [parts for _, parts in people if formats_explaining(local, parts)]
    return hits[0] if len(hits) == 1 else None


def _shape_only_guess(unnamed: Sequence[str], examined: int) -> PatternGuess:
    """No sample could be tied to a name — report the shape, and n=0.

    This is the case §2.3 warns about most directly. There is real information
    here (`w.w` rules out `flast`), and there is no confidence at all, so the
    pattern is returned with `sample_size=0` and the UI renders it as a guess.
    """
    shapes = Counter(shape_of(local) for local in unnamed if shape_of(local))
    if not shapes:
        return PatternGuess(pattern=None, examined=examined)
    shape, _ = shapes.most_common(1)[0]
    options = [f for f in formats_with_shape(shape) if f not in LOW_INFORMATION_FORMATS]
    if not options:
        return PatternGuess(pattern=None, shape_support=sum(shapes.values()), examined=examined)
    return PatternGuess(
        pattern=options[0],
        sample_size=0,
        agreement=None,
        alternatives=options[1:3],
        shape_support=sum(shapes.values()),
        examined=examined,
    )


# ─────────────────────────── applying a pattern ──────────────────────────


def address_for(name: str | NameParts | None, domain: str, pattern: str) -> str | None:
    """The address this pattern implies for this person at this domain."""
    parts = name if isinstance(name, NameParts) else split_name(name)
    if parts is None or not domain:
        return None
    if is_free_provider(domain):
        # A company format applied to gmail.com is nonsense: personal mailboxes
        # are chosen by their owner, not generated by an IT policy.
        return None
    return apply_format(pattern, parts, domain)


def generic_addresses(name: str | NameParts | None, domain: str, limit: int = 4) -> list[str]:
    """§4's "generic permutation" — what to offer when nothing is known.

    Every one of these is a `guess` and must be labelled as one. They are
    ordered by real-world frequency, and `LOW_INFORMATION_FORMATS` are excluded:
    offering `jd@alpine.com` is offering an address as likely to reach a
    stranger as the person meant.
    """
    parts = name if isinstance(name, NameParts) else split_name(name)
    if parts is None or not domain or is_free_provider(domain):
        return []
    out: list[str] = []
    for fmt in GENERIC_PERMUTATIONS:
        address = apply_format(fmt, parts, domain)
        if address and address not in out and not is_role_address(address):
            out.append(address)
        if len(out) >= limit:
            break
    return out


def format_of(email: str, name: str | NameParts | None) -> str | None:
    """Which format explains this person's own known address? For the corpus."""
    parts = name if isinstance(name, NameParts) else split_name(name)
    if parts is None:
        return None
    explaining = formats_explaining(local_part(email), parts)
    return explaining[0] if explaining else None


def sample_domain(sample: dict[str, Any]) -> str:
    return domain_of(str(sample.get("email") or ""))


# ─────────────────────────────── the step ────────────────────────────────


class PatternStep:
    """Infer the company's format, then apply it to this person.

    The §4 status table, implemented literally:

    * `probable` — pattern from >= `enrich_min_samples_for_probable` samples,
      MX valid, not a role or disposable address;
    * `guess` — pattern from one sample, or a generic permutation, MX valid;
    * nothing at all — the domain publishes no MX records, which is a verdict
      about the *domain* and usually means it was resolved wrongly.

    An MX check that could not run is not the same as one that failed. It
    downgrades `probable` to `guess`, because §4 lists MX validity as part of
    what earns `probable` and an unchecked address has not earned it — but it
    never deletes a finding, because a resolver timeout is evidence of nothing.
    """

    name = "pattern"
    spends_credits = False

    def __init__(self, *, resolver: Any | None = None, check_mx: bool = True) -> None:
        #: Injected in tests so the suite never touches DNS.
        self.resolver = resolver
        self.check_mx = check_mx

    def run(self, ctx: EnrichmentContext) -> StepOutcome:
        from .harvest import known_people_at

        domains = ctx.domain_list()
        if not domains:
            return StepOutcome(
                step=self.name, outcome="skipped", detail="no domain resolved"
            )
        parts = split_name(ctx.full_name)
        if parts is None:
            return StepOutcome(
                step=self.name, outcome="skipped",
                detail=f"{ctx.full_name!r} is not a usable first-and-last name",
            )

        people = known_people_at(ctx)
        found: list[EmailCandidate] = []
        details: list[str] = []
        for domain in domains:
            candidate, detail = self._for_domain(ctx, domain, parts, people)
            details.append(f"{domain}: {detail}")
            if candidate:
                found.append(candidate)

        if len(domains) > 1 and len(found) > 1:
            # Several works organisations share this name, and a headline that
            # says "Alpine" does not say which one. Both addresses are shown,
            # each with its own evidence, rather than one being picked silently.
            ctx.note(
                f"{ctx.company_name} uses {len(domains)} domains "
                f"({', '.join(domains)}), so there is one candidate address per "
                "organisation. Check which one this person actually works for."
            )

        return StepOutcome(
            step=self.name,
            outcome="ok" if found else "empty",
            detail=" · ".join(details),
            found=found,
        )

    def _for_domain(
        self,
        ctx: EnrichmentContext,
        domain: str,
        parts: NameParts,
        people: list[dict[str, Any]],
    ) -> tuple[EmailCandidate | None, str]:
        """The §4 status table, applied at one domain."""
        from .checks import check_address

        samples = ctx.samples_for(domain)
        guess = infer_pattern(samples, known_people=people)
        domain_id = ctx.domain_id_for(domain)

        if domain == ctx.domain:
            ctx.pattern = guess.pattern
            ctx.pattern_sample_size = guess.sample_size
            ctx.pattern_agreement = guess.agreement
        if domain_id is not None and guess.pattern:
            with ctx.repos.writing():
                ctx.repos.domains.set_pattern(
                    domain_id, guess.pattern, guess.sample_size, guess.agreement
                )

        minimum = ctx.settings.enrich_min_samples_for_probable
        if guess.pattern:
            address = address_for(parts, domain, guess.pattern)
            evidence = guess.as_evidence()
            generic = False
        else:
            options = generic_addresses(parts, domain, limit=3)
            address = options[0] if options else None
            evidence = {
                "pattern": GENERIC_PERMUTATIONS[0],
                "sample_size": 0,
                "pattern_alternatives": options[1:],
                "generic_permutation": True,
            }
            generic = True

        if not address:
            return None, f"no address could be built ({guess.describe()})"

        checks = check_address(address, check_mx=self.check_mx, resolver=self.resolver)
        if not checks.usable:
            return None, "not usable: " + "; ".join(checks.reasons())
        if checks.mx is not None and checks.mx.conclusive and not checks.mx.valid:
            # A domain that publishes no MX records accepts no mail. Every
            # address derived from it is worthless, and the likeliest
            # explanation is that the domain itself is wrong.
            ctx.note(
                f"{domain} publishes no MX records, so it accepts no mail — "
                "that domain is probably wrong. Correct it and try again."
            )
            return None, "accepts no mail (no MX records)"

        mx_confirmed = bool(checks.mx and checks.mx.valid)
        status = "guess"
        if not generic and guess.meets(minimum) and mx_confirmed:
            status = "probable"
        elif not generic and guess.meets(minimum) and not mx_confirmed:
            ctx.note(
                f"the mail server for {domain} could not be checked, so this "
                "stays a guess rather than becoming probable"
            )

        evidence.update(checks.as_evidence())
        evidence["derived_from_domain"] = domain
        evidence["domain_source"] = ctx.domain_source
        if len(ctx.domains) > 1:
            # Which of the company's organisations this address belongs to is
            # part of the evidence, not a footnote: the reader has to be able
            # to tell `jane.doe@alpinef1.com` from `jane.doe@alpinecars.com`.
            evidence["company_domains"] = list(ctx.domains)

        return (
            EmailCandidate(
                email=address,
                status=status,
                source="pattern",
                evidence=evidence,
                # Deliberately None. §4: never render a bare confidence
                # number for a locally-derived guess.
                confidence=None,
                mx_valid=checks.mx_valid,
                is_role_address=checks.is_role,
                is_disposable=checks.is_disposable,
            ),
            f"{status} from {guess.describe()}",
        )
