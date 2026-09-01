"""Finding a company's mail domain — the input `enrich/domains.py` reads.

§2.1 resolves a domain per candidate, at lookup time, from whatever it can
find. That works, but its weakest source — guessing from stored search results
— is the one that produced `alpine-cars.co.uk` for a race team called Alpine.
A car dealership. Every address derived from it would have been plausible,
wrong, and aimed at somebody else's mail server.

`companies.domain` and `company_domains` are checked before any guessing, so
establishing them once removes that failure for good. This module is how they
get established: it gathers candidates, scores them, and says why — and it
**narrows rather than decides**. The owner reads the evidence and confirms.

Lives in the package rather than in `scripts/` because both the CLI
(`scripts/resolve_domains.py`) and the Setup page drive it, and a scoring rule
that differed between them would be a bug nobody could see.

A works team is several organisations sharing a name, so this returns **all** of
a company's domains, not one: Alpine's F1 entry and its sportscar programme are
different employers and a profile that says "Alpine" does not say which.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..db import Repos
from ..extract.normalize import company_tokens
from .base import SerpSearcher
from .checks import mx_check
from .domains import (
    domain_from_url,
    domain_label,
    domain_matches_company,
    is_company_domain,
)

log = logging.getLogger(__name__)

#: Three queries per company. The first asks the open web who they are; the
#: second is qualified by the racing series, which is the most useful
#: disambiguator this database already holds — "Alpine" is ambiguous, "Alpine
#: WEC" is not; the third goes at the contact page, which is where a company
#: puts its own domain in prose.
QUERIES = (
    '"{name}" official website',
    '"{name}" {series} team',
    '"{name}" contact us email',
)

#: Engines pinned for this script, rather than the rotating groups in
#: `SEARXNG_ENGINES`.
#:
#: Rotation exists so a long sweep does not put every query through one upstream
#: engine and collect captchas. This is not a sweep — 49 companies, once — and
#: it wants the *best* answer each time rather than a polite spread. A rotated
#: query landing on a blocked group is what made AF Corse resolve to nothing on
#: one run and to `afcorse.it` on the next.
#:
#: **Qwant leads, not Google, and that is a finding rather than a preference.**
#: Everything here turns on quoted phrases surviving to the upstream engine —
#: `"Iron Lynx"` must not be read as a query about the element — and of the
#: engines this instance can currently reach:
#:
#:   · **google** honours quotes and is the best answer while it lasts. It
#:     CAPTCHA-suspended after roughly 150 queries during this script's own
#:     development, which is about three full passes.
#:   · **qwant** honours quotes, is not rate-limited here, and returned
#:     `ironlynx.com` first for the query Google was suspended on.
#:   · **bing** answers reliably and **ignores quoted phrases**, so on its own
#:     it returns the open web. Harmless as a second engine — the name and
#:     motorsport filters discard its noise — and useful for recall.
#:   · brave, duckduckgo, mojeek, startpage: all blocked on this instance.
#:
#: **Which engines are healthy changes by the hour, so the script probes rather
#: than trusting a list.** Over one afternoon of development: Google worked,
#: then CAPTCHA-suspended after ~150 queries; Qwant took over and answered
#: perfectly, then CAPTCHA-suspended in turn; Brave was "too many requests" at
#: the start and had recovered by the end, returning `manthey-racing.com` first
#: for a query the other two had just failed. Meanwhile **Bing answered every
#: time and was worse than useless** — it ignores quoted phrases, and for
#: `"Manthey" racing official` it returned pornography.
#:
#: So `--engines` is a *candidate pool*, `probe_engines()` picks the ones
#: actually working right now, and an engine that fails the probe is skipped
#: rather than silently polluting the results.
#: `google cse` is listed separately from `google` on purpose: SearXNG treats
#: them as different engines with **separate rate limits**, and it was the only
#: thing still answering after a run had CAPTCHA'd everything else. Worth
#: knowing when the pool looks empty.
DEFAULT_ENGINES = "google,google cse,qwant,brave,duckduckgo,startpage,mojeek"

#: Sent to each candidate engine before the run. Two properties are checked:
#: the engine answers at all, and it **honours a quoted phrase**. The second is
#: what disqualifies Bing, which answers reliably and reads `"Iron Lynx"` as a
#: question about the element.
PROBE_QUERY = '"Scuderia Ferrari" official website'
PROBE_EXPECT = "ferrari"

#: How many domains may be recorded for one company. Matches `enrich.domains`'
#: own cap: a works team is an F1 entry, a sportscar programme and a parent,
#: and past that the extra addresses are noise rather than coverage.
MAX_COMPANY_DOMAINS = 3

#: How far behind the winner a domain may score and still be treated as a
#: *second organisation* rather than a runner-up.
#:
#: Deliberately generous. `alpinef1.com` and `alpinecars.com` are both Alpine;
#: whichever the scorer happens to prefer, discarding the other would hide half
#: the answer for anyone whose profile just says "Alpine". A false extra domain
#: costs one more clearly-labelled candidate address; a missing one costs the
#: right answer entirely.
SIBLING_MARGIN = 2.5

#: …but a sibling must still be *mostly* the company's name. Coverage alone is
#: too easy to satisfy: "ART Grand Prix" reduces to the token `art` once the
#: generic motorsport words are stripped, so `grandprixart.co.uk` — an art shop
#: — covers 100% of it while being only 25% name. Being recorded as a second
#: domain means addresses get generated at it, so the bar is higher than for
#: merely appearing in the alternatives list.
SIBLING_MIN_PRECISION = 0.45

#: Never a company's staff-email domain, whatever else it is. A merchandise
#: shop or a parts store is a separate property on a separate domain, and
#: `toyotagazooracingb2bshop.com` matches the full team name perfectly while
#: being exactly the wrong answer.
SHOP_MARKERS = (
    "shop", "store", "merch", "merchandise", "parts", "spares", "tickets",
    "hire", "rental", "dealer", "dealership", "usedcars", "boutique",
    # A works team runs several *web properties* as well as several
    # organisations, and the difference matters: nobody's staff mail lives on
    # a parts catalogue or an anniversary microsite. Manthey resolved to
    # `manthey-kit.com` and `manthey-25years.com` ahead of `manthey-racing.com`
    # until these were added.
    "kit", "kits", "collection", "experience", "museum", "heritage",
    "anniversary", "jubilee", "fanshop", "tickets", "hospitality", "academy",
)

#: `manthey-25years.com`, `team50years.de` — an anniversary site is a campaign,
#: not an employer.
ANNIVERSARY = re.compile(r"\d{1,3}(years|jahre|anni|ans)\b")

#: Words that say "this domain belongs to the racing organisation", as opposed
#: to another property of the same brand. A modest bonus, but it is exactly
#: what separates `manthey-racing.com` from `manthey-kit.com` — and the team is
#: the employer, which is the only thing being asked about here.
TEAM_WORDS = (
    "racing", "motorsport", "motorsports", "sport", "sports", "team",
    "competition", "competizioni", "corse", "rennsport", "engineering",
    "grandprix", "gp", "f1", "wec", "endurance",
)

#: Softer: a *retail arm*, which is a real risk for a manufacturer-backed team
#: — `alpine-cars.co.uk` is Alpine's UK sales site, not the race team. Only a
#: penalty, and waived when the page calls itself the official home, because a
#: car maker's own domain reasonably contains "cars".
RETAIL_MARKERS = (
    "cars", "car", "auto", "autos", "motors", "garage", "sales",
    "finance", "insurance", "lease", "tyres", "used",
)

#: HQ country -> the TLDs a company there actually uses. `companies.csv`
#: already records the country and it was going unused: AF Corse is Italian, so
#: `afcorse.it` is its home domain and `afcorseusa.com` is a satellite.
COUNTRY_TLDS: dict[str, tuple[str, ...]] = {
    "italy": ("it",), "uk": ("uk", "co.uk"), "united kingdom": ("uk", "co.uk"),
    "germany": ("de",), "france": ("fr",), "switzerland": ("ch",),
    "austria": ("at",), "belgium": ("be",), "netherlands": ("nl",),
    "spain": ("es",), "portugal": ("pt",), "usa": ("com", "us"),
    "united states": ("com", "us"), "japan": ("jp", "co.jp"),
    "sweden": ("se",), "denmark": ("dk",), "poland": ("pl",),
    "czech republic": ("cz",), "monaco": ("mc",), "ireland": ("ie",),
}

#: Hosts that pass `is_company_domain` but are still never a team's own site.
EXTRA_NOISE = frozenset(
    {
        "motorsport.com", "motorsportweek.com", "autosport.com", "racefans.net",
        "the-race.com", "espn.com", "bbc.co.uk", "skysports.com", "formula1.com",
        "fia.com", "fiawec.com", "imsa.com", "24h-lemans.com", "gt-world-challenge.com",
        "racingnews365.com", "motor1.com", "carscoops.com", "topgear.com",
        "autocar.co.uk", "pistonheads.com", "wikiwand.com", "britannica.com",
        "companieshouse.gov.uk", "opencorporates.com", "endurance-info.com",
        "sportscar365.com", "dailysportscar.com", "f1i.com", "planetf1.com",
    }
)


#: A page that says so in its own title is telling you it is the company's own
#: site, which is exactly the question being asked.
#: Multilingual for the same reason `MOTORSPORT_WORDS` is: half these teams are
#: Italian, French or German, and `afcorse.it` describes itself as the *sito
#: ufficiale*. English-only, this bonus went to the English-language satellite
#: office instead of the actual headquarters.
OFFICIAL_MARKERS = (
    "official", "home of", "welcome to", "| home",
    "ufficiale", "sito ufficiale",              # it
    "officiel", "site officiel",                # fr
    "offizielle", "offizielles", "webseite",    # de
    "oficial", "sitio oficial",                 # es
    "officiële",                                # nl
)

#: **The filter that makes this usable below tier 1.** A name match alone
#: cannot resolve "Century Motorsport", "Optimum Motorsport", "Trident" or
#: "Garage 59": every one of them shares its distinctive word with a bigger,
#: better-linked business, and the first run of this script duly returned
#: `century.tech`, `optimum.com`, `proton.me` and a coffee roaster called
#: Campos. So a candidate must come from a page that is *about motorsport*.
#: Multilingual on purpose — half these teams are Italian, French or German,
#: and `afcorse.it` describes itself as a scuderia, not as a racing team.
MOTORSPORT_WORDS = (
    "racing", "motorsport", "motorsports", "race team", "raceteam",
    "scuderia", "écurie", "ecurie", "equipe", "équipe", "rennteam", "renn",
    "gt3", "gt4", "gt2", "lmp2", "lmp3", "hypercar", "lmdh", "lmh",
    "le mans", "endurance", "wec", "imsa", "elms", "gt world challenge",
    "formula 1", "formula one", "formula 2", "formula 3", "formula e",
    "f1 ", "f2 ", "f3 ", " f1", " f2", " f3", "grand prix", "championship",
    "paddock", "pit lane", "pitlane", "chassis", "driver line-up", "drivers",
    "podium", "circuit", "autodromo", "rennsport", "karting", "single-seater",
    "sportscar", "sports car", "touring car", "dtm", "nürburgring", "spa-francorchamps",
)


def is_motorsport(text: str) -> bool:
    lowered = f" {str(text or '').lower()} "
    return any(word in lowered for word in MOTORSPORT_WORDS)


@dataclass
class Candidate:
    domain: str
    serp_count: int = 0
    best_rank: int = 99
    seen_in_search: bool = False
    official_title: bool = False
    #: The page this domain came from is about motorsport. Effectively
    #: mandatory — see `MOTORSPORT_WORDS`.
    motorsport: bool = False
    mx_valid: bool | None = None
    mx_host: str | None = None

    #: Share of the company's distinctive tokens the domain label contains.
    coverage: float = 0.0
    #: Share of the domain label that IS the company's name. `afcorse` is 71%
    #: name; `afcorseusa` is 50%; `alpineperformanceparts` is 27%.
    precision: float = 0.0
    home_tld: bool = False
    team_word: bool = False
    retail: bool = False

    @property
    def score(self) -> float:
        """Coverage *and* precision. Everything else only breaks ties.

        Both halves were learned by running this against the real 49 and
        reading the mistakes, in two rounds:

        * **Stored SERP frequency is weak, and used to be weighted strongly.**
          Those rows count every page that *mentioned* the company — news, a
          retail arm, a dealership — so Alpine's 11 stored hits on
          `alpinef1.com` outvoted `alpinecars.com`, whose own title reads
          "Official Home of Alpine Cars & Racing".
        * **Coverage alone is cheap.** Once it dominated, `afcorseusa.com` beat
          `afcorse.it` and `toyotagazooracingb2bshop.com` beat everything:
          each contains the whole company name and then some. Precision is the
          missing half — the canonical domain is the name and *nothing else* —
          and the HQ country in `companies.csv` settles the rest.
        """
        score = 3.5 * self.coverage + 2.5 * self.precision
        score += min(0.6, self.serp_count / 20.0)
        if self.seen_in_search:
            score += 1.2 - min(0.9, self.best_rank / 10.0)
        if self.official_title:
            score += 1.5
        if self.home_tld:
            score += 1.0
        if self.team_word:
            score += 1.25
        if self.mx_valid:
            score += 0.75
        elif self.mx_valid is False:
            # A domain that accepts no mail cannot be the answer to a question
            # about email, whatever else it is.
            score -= 2.5
        if self.retail and not self.official_title:
            score -= 2.0
        return score

    def why(self) -> str:
        bits = [f"name {self.coverage:.0%}/{self.precision:.0%}"]
        if self.official_title:
            bits.append("says “official”")
        if self.home_tld:
            bits.append("home TLD")
        if self.team_word:
            bits.append("names the team")
        if self.seen_in_search:
            bits.append(f"search #{self.best_rank + 1}")
        if self.serp_count:
            bits.append(f"{self.serp_count} stored")
        if self.mx_valid:
            bits.append(f"MX {self.mx_host or 'ok'}")
        elif self.mx_valid is False:
            bits.append("no MX")
        if self.retail:
            bits.append("looks retail")
        return " · ".join(bits)


@dataclass
class Result:
    name: str
    series: str | None
    current: str | None
    chosen: Candidate | None = None
    #: Other domains this company genuinely uses — a second works organisation,
    #: not a runner-up. Stored alongside the primary, because the person may be
    #: in either. See `SIBLING_MARGIN`.
    siblings: list[Candidate] = field(default_factory=list)
    alternatives: list[Candidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def domains(self) -> list[str]:
        return ([self.chosen.domain] if self.chosen else []) + [
            c.domain for c in self.siblings
        ]
    searches_run: int = 0
    searches_empty: int = 0
    searches_failed: int = 0

    @property
    def search_worked(self) -> bool:
        """Did any query this company ran come back with anything at all?"""
        return self.searches_run > self.searches_empty and not self.searches_failed

    @property
    def changed(self) -> bool:
        return self.chosen is not None and self.chosen.domain != (self.current or "")


def probe_engines(settings, candidates: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Which of these engines is answering *and* honouring quotes right now?

    One query each, against a company whose answer is not in doubt. An engine
    that is captcha'd, that returns nothing, or that returns something with no
    "ferrari" in it has failed the only two tests that matter here.
    """
    import httpx

    good: list[str] = []
    bad: list[tuple[str, str]] = []
    for engine in candidates:
        try:
            response = httpx.get(
                f"{settings.searxng_base_url.rstrip('/')}/search",
                params={"q": PROBE_QUERY, "engines": engine, "format": "json"},
                timeout=settings.searxng_timeout_seconds,
            )
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - a dead engine is a state
            bad.append((engine, f"unreachable: {str(exc)[:40]}"))
            continue

        dead = payload.get("unresponsive_engines") or []
        if dead:
            reason = dead[0][1] if isinstance(dead[0], list | tuple) and len(dead[0]) > 1 else "down"
            bad.append((engine, str(reason)[:40]))
            continue
        results = payload.get("results") or []
        if not results:
            bad.append((engine, "no results"))
            continue
        blob = " ".join(
            f"{r.get('url', '')} {r.get('title', '')}" for r in results[:5]
        ).lower()
        if PROBE_EXPECT not in blob:
            bad.append((engine, "ignores quoted phrases"))
            continue
        good.append(engine)
    return good, bad


def name_forms(company: dict) -> list[str]:
    """The company's name and its aliases — the owner's own disambiguators.

    `companies.csv` already carries "TGR-E|Toyota Gazoo Racing Europe" against
    Toyota Gazoo Racing, which is better information than any query this script
    could invent, and it was going unused.
    """
    forms = [str(company["name"])]
    for alias in company.get("aliases") or []:
        alias = str(alias).strip()
        if alias and alias.casefold() not in {f.casefold() for f in forms}:
            forms.append(alias)
    return forms


def queries_for(company: dict) -> list[str]:
    """Every search this company gets. Aliases get the plain "official" one."""
    name = str(company["name"])
    series = str(company.get("series") or "").strip()
    out = [
        template.format(name=name, series=series)
        for template in QUERIES
        if "{series}" not in template or series
    ]
    # Two aliases at most: this is 49 companies and each query is a second.
    out += [f'"{alias}" official website' for alias in name_forms(company)[1:3]]
    return out


def token_sets(form: str) -> tuple[set[str], set[str]]:
    """Two readings of one name form: canonicalised, and as written.

    They disagree in a way that matters. `company_tokens` runs the canonical
    map, which turns the alias "TGR-E" back into "toyota gazoo racing" — the
    long form the alias exists to replace, and the one that does not appear in
    `tgr-e.com`. The canonical map is right for matching a person's employer to
    a company; it is wrong for matching a company to its domain.
    """
    canonical = {t for t in company_tokens(form) if len(t) >= 3}
    raw = "".join(ch if ch.isalnum() else " " for ch in str(form).lower())
    return canonical, {t for t in raw.split() if len(t) >= 3}


def name_match(domain: str, forms: list[str]) -> tuple[float, float]:
    """`(coverage, precision)` for the best-matching reading of the name.

    * **coverage** — how much of the company's name the domain carries.
    * **precision** — how much of the domain is the company's name, and nothing
      else. `afcorse` is mostly "corse"; `afcorseusa` carries a region as well;
      `toyotagazooracingb2bshop` carries a whole business line.

    Both come from the *same* token set, so they cannot describe different
    readings of the same domain. The canonical reading is tried first and the
    raw one only when it scores nothing — unioning them looked simpler and
    quietly reopened a hole: with "grand" and "prix" back in play,
    `grandprixart.co.uk` (an art shop) matched "ART Grand Prix" perfectly.
    """
    label = domain_label(domain)
    if not label:
        return 0.0, 0.0
    best = (0.0, 0.0)
    for form in forms:
        for tokens in token_sets(form):
            if not tokens:
                continue
            hit = {t for t in tokens if t in label}
            if not hit:
                continue
            coverage = len(hit) / len(tokens)
            precision = min(1.0, sum(len(t) for t in hit) / len(label))
            best = max(best, (coverage, precision))
            break          # this reading answered; do not fall back to the next
    return best


def coverage_of(domain: str, forms: list[str]) -> float:
    return name_match(domain, forms)[0]


def precision_of(domain: str, forms: list[str]) -> float:
    return name_match(domain, forms)[1]


def looks_retail(domain: str, name: str) -> bool:
    label = domain_label(domain)
    lowered = name.lower()
    return any(m in label and m not in lowered for m in RETAIL_MARKERS)


def is_shop(domain: str) -> bool:
    label = domain_label(domain)
    return any(m in label for m in SHOP_MARKERS) or bool(ANNIVERSARY.search(label))


def has_team_word(domain: str, name: str) -> bool:
    """Does the domain say it belongs to the racing organisation?

    Ignored when the company's own name contains the word, because then it
    carries no information: every candidate for "PREMA Racing" would score it.
    """
    label = domain_label(domain)
    lowered = name.lower()
    return any(w in label and w not in lowered for w in TEAM_WORDS)


def home_tld(domain: str, country: str | None) -> bool:
    suffix = domain.split(".", 1)[-1]
    wanted = COUNTRY_TLDS.get(str(country or "").strip().lower(), ())
    return bool(wanted) and (suffix in wanted or suffix.split(".")[-1] in wanted)


def usable(domain: str | None) -> bool:
    if not domain or not is_company_domain(domain) or domain in EXTRA_NOISE:
        return False
    # A merchandise shop matches the team name perfectly and is never where
    # staff email lives, so it is excluded rather than merely penalised.
    return not is_shop(domain)


def from_stored_serps(repos, company_id: int) -> tuple[Counter[str], set[str]]:
    """Hosts this database already fetched for that company. Costs nothing.

    Returns the counts and the subset whose stored title or snippet was about
    motorsport — the same context test the live searches get, applied to
    evidence that was already paid for.
    """
    counts: Counter[str] = Counter()
    racing: set[str] = set()
    for row in repos.conn.execute(
        "SELECT sr.url, sr.title, sr.snippet FROM search_results sr"
        "  JOIN queries q ON q.id = sr.query_id WHERE q.company_id = ?",
        (company_id,),
    ):
        domain = domain_from_url(row["url"])
        if domain is None or not usable(domain):
            continue
        counts[domain] += 1
        if is_motorsport(f"{row['title'] or ''} {row['snippet'] or ''}"):
            racing.add(domain)
    return counts, racing


def resolve(repos, searcher, company: dict, *, check_mx: bool = True) -> Result:
    name = str(company["name"])
    series = str(company.get("series") or "").strip()
    result = Result(name=name, series=series or None, current=company.get("domain") or None)

    pool: dict[str, Candidate] = {}

    counts, racing = from_stored_serps(repos, int(company["id"]))
    for domain, count in counts.items():
        item = pool.setdefault(domain, Candidate(domain))
        item.serp_count = count
        item.motorsport = item.motorsport or domain in racing

    forms = name_forms(company)
    queries = queries_for(company)

    def confident() -> bool:
        """A domain that carries the whole name, on a motorsport page, is not
        going to be improved on by another query. Stopping there is what keeps
        the run inside one engine's patience — every extra query is a step
        closer to the CAPTCHA that ends the whole pass."""
        for item in pool.values():
            if not item.motorsport:
                continue
            if coverage_of(item.domain, forms) >= 1.0 and precision_of(item.domain, forms) >= 0.5:
                return True
        return False

    for index, query in enumerate(queries):
        if index and confident():
            break
        try:
            hits = searcher.search(query, max_results=12)
        except Exception as exc:  # noqa: BLE001 - one dead query is not fatal
            result.warnings.append(f"search failed: {str(exc)[:80]}")
            result.searches_failed += 1
            continue
        result.searches_run += 1
        if not hits:
            # A live engine returning nothing for a quoted company name is
            # unusual; a *suspended* one returns nothing every time. Counted so
            # the caller can tell the difference across several companies.
            result.searches_empty += 1
        for rank, hit in enumerate(hits):
            hit_domain = domain_from_url(hit.url)
            if hit_domain is None or not usable(hit_domain):
                continue
            item = pool.setdefault(hit_domain, Candidate(hit_domain))
            item.seen_in_search = True
            item.best_rank = min(item.best_rank, rank)
            blurb = f"{hit.title} {hit.snippet}"
            if any(m in blurb.lower() for m in OFFICIAL_MARKERS):
                item.official_title = True
            if is_motorsport(blurb):
                item.motorsport = True

    # Only domains that actually carry the company's name — or one of its
    # aliases — are considered. This is the whole guard against picking a news
    # article's host, and the alias half matters: "TGR-E" and "Toyota Gazoo
    # Racing Europe" are the same organisation and the owner already wrote both
    # down in companies.csv.
    named = [
        c for c in pool.values()
        if any(domain_matches_company(c.domain, form) for form in forms)
    ]

    # **The hard filter.** A domain must have come from a page about
    # motorsport, not merely from a page whose host shares a word with the
    # team's name. Without this the tail of the list resolves to whichever
    # bigger business owns the short name — `optimum.com`, `proton.me`,
    # `century.tech`, a coffee roaster called Campos — and each of those would
    # generate confident, plausible addresses aimed at total strangers.
    #
    # When nothing survives, the answer is "no domain", not the best of a bad
    # set. §2.1: a wrong domain poisons everything downstream.
    racing_only = [c for c in named if c.motorsport]
    if named and not racing_only:
        result.warnings.append(
            f"{len(named)} name match(es) but none on a motorsport page — "
            "probably a different company with the same word in its name"
        )
    named = racing_only
    country = str(company.get("country") or "")
    for item in named:
        item.coverage = coverage_of(item.domain, forms)
        item.precision = precision_of(item.domain, forms)
        item.home_tld = home_tld(item.domain, country)
        item.team_word = has_team_word(item.domain, name)
        item.retail = looks_retail(item.domain, name)

    # MX only on the plausible few: it is free but it is still 49 x N lookups.
    if check_mx:
        for item in sorted(named, key=lambda c: -c.score)[:4]:
            mx = mx_check(item.domain)
            item.mx_valid = mx.valid
            item.mx_host = mx.provider

    ranked = sorted(named, key=lambda c: -c.score)
    if ranked:
        result.chosen = ranked[0]
        result.alternatives = ranked[1:4]
        # A works team is several organisations sharing a name — Alpine's F1
        # entry and its sportscar programme are different domains and a profile
        # saying "Alpine" does not say which. Anything close behind the winner
        # is kept as a real second answer rather than as a footnote, and
        # `--apply` records all of them.
        result.siblings = [
            c for c in ranked[1:MAX_COMPANY_DOMAINS]
            if c.score >= ranked[0].score - SIBLING_MARGIN
            and c.coverage >= 0.5
            and c.precision >= SIBLING_MIN_PRECISION
        ]
        if result.chosen.retail:
            result.warnings.append("the winner looks like a retail site — check it")
        if result.chosen.coverage < 0.5:
            result.warnings.append("weak name match — check it")
        if len(ranked) > 1 and ranked[0].score - ranked[1].score < 0.75:
            result.warnings.append(f"close call with {ranked[1].domain}")
    elif not result.warnings:
        result.warnings.append("nothing matched the company name")
    return result


# ─────────────────────────── driving the whole thing ─────────────────────


@dataclass
class DiscoveryRun:
    """What a pass over several companies produced, and how it ended."""

    results: list[Result] = field(default_factory=list)
    engines: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    #: True when the search engines gave out partway. The results so far are
    #: still worth keeping — that is the whole reason this is reported rather
    #: than raised.
    aborted: bool = False
    remaining: int = 0

    @property
    def resolved(self) -> list[Result]:
        return [r for r in self.results if r.chosen]

    @property
    def needs_a_look(self) -> list[Result]:
        return [r for r in self.results if r.chosen and r.warnings]


class NoUsableEngine(RuntimeError):
    """Nothing is both reachable and honouring quoted phrases right now."""


def discover_domains(
    repos: Repos,
    companies: Sequence[dict[str, Any]],
    settings: Settings | None = None,
    *,
    engines: str = DEFAULT_ENGINES,
    qps: float = 0.4,
    check_mx: bool = True,
    use_cache: bool = False,
    on_progress: Callable[[int, int, Result | None, str], None] | None = None,
) -> DiscoveryRun:
    """Resolve domains for a list of companies, reporting as it goes.

    `on_progress(done, total, result, message)` is called before and after each
    company so a caller can stream the working rather than showing a bar and a
    surprise. The CLI prints it; the Setup page renders it live.

    Stops early and honestly when the engines give out — see `barren` below.
    """
    settings = settings or get_settings()
    run = DiscoveryRun()

    healthy, rejected = probe_engines(settings, [e.strip() for e in engines.split(",") if e.strip()])
    run.engines, run.rejected = healthy, rejected
    if not healthy:
        raise NoUsableEngine(
            "no search engine is both reachable and honouring quoted phrases. "
            "They recover on their own — a rate limit clears in about 15 minutes "
            "and a CAPTCHA in about an hour."
        )

    searcher = SerpSearcher(
        settings=settings.with_overrides(
            searxng_engines=[",".join(healthy)], searxng_queries_per_second=qps
        ),
        use_cache=use_cache,
        max_results=12,
    )

    #: Consecutive companies whose every search came back empty. An engine that
    #: dies mid-run does not announce it — it answers nothing, forever, while
    #: tenacity spends ~30s retrying each query. Counting is the only way to
    #: notice before the whole pass has been spent on silence.
    barren = 0
    total = len(companies)
    for index, company in enumerate(companies, start=1):
        if on_progress:
            on_progress(index - 1, total, None, f"looking for {company['name']}")
        result = resolve(repos, searcher, company, check_mx=check_mx)
        run.results.append(result)
        if on_progress:
            on_progress(index, total, result, "")

        barren = 0 if result.search_worked else barren + 1
        if barren < 4:
            continue
        still_ok, _ = probe_engines(settings, healthy)
        if still_ok:
            barren = 0
            continue
        run.aborted = True
        run.remaining = total - index
        break

    return run


def apply_domains(
    repos: Repos,
    results: Sequence[Result],
    *,
    overwrite: bool = False,
    chosen: dict[str, list[str]] | None = None,
) -> tuple[int, int]:
    """Write the confirmed domains. Returns (primaries in CSV, rows recorded).

    Two destinations, because they answer different questions. The CSV's single
    `domain` column is the company's main one and is the file the owner edits;
    `company_domains` holds **every** organisation the company runs, and is what
    the enrichment chain actually reads.

    `chosen` lets a caller override what to write per company — which is what
    the Setup page's tick boxes produce, and the reason this takes results
    rather than doing its own resolution.
    """
    from ..inputs import load_companies, save_companies

    picked: dict[str, list[str]] = {}
    for result in results:
        domains = (chosen or {}).get(result.name, result.domains)
        if domains:
            picked[result.name] = [d for d in domains if d]

    # `load_companies` returns (companies, report), not a list. Iterating the
    # tuple raised on the first element — so `--apply` had never actually
    # worked, and nothing caught it because `scripts/` is outside mypy's scope.
    # Moving this into the package is what surfaced it.
    models, report = load_companies()
    if report.errors:
        log.warning(
            "config/companies.csv has %d error(s); rewriting it would drop those "
            "rows. Fix them with `pitwall validate` first.", len(report.errors)
        )
        return 0, _record(repos, picked)

    written = 0
    for model in models:
        if model.name in picked and (overwrite or not (model.domain or "").strip()):
            model.domain = picked[model.name][0]
            written += 1
    if written:
        save_companies(models)

    return written, _record(repos, picked)


def _record(repos: Repos, picked: dict[str, list[str]]) -> int:
    """Every domain into `company_domains` — the table that holds more than one."""
    recorded = 0
    rows = {c["name"]: c for c in repos.companies.all(active_only=False)}
    with repos.writing():
        for name, domains in picked.items():
            company = rows.get(name)
            if not company:
                continue
            for domain in domains:
                repos.domains.upsert(int(company["id"]), domain, source="resolved")
                recorded += 1
    return recorded
