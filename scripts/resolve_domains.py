#!/usr/bin/env python
"""Fill `companies.domain` — the highest-value free improvement available.

    .venv/bin/python scripts/resolve_domains.py                  # review only
    .venv/bin/python scripts/resolve_domains.py --apply          # write the CSV
    .venv/bin/python scripts/resolve_domains.py --companies "Alpine,Ferrari"
    .venv/bin/python scripts/resolve_domains.py --show           # what is known

**Why this exists.** §2.1 of PITWALL_V3_EMAIL_ENRICHMENT.md resolves a domain
per candidate, at lookup time, from whatever it can find. That works, but the
weakest source in the chain — guessing from stored search results — is the one
that produced `alpine-cars.co.uk` for a race team called Alpine. A car
dealership. Every address derived from it would have been plausible, wrong, and
aimed at somebody else's mail server.

`companies.domain` is checked before any guessing, so filling it once removes
that failure for good. It is also the cheapest possible fix: 49 rows of CSV.

**This script narrows the field. It does not decide.** It ranks candidates,
says why each won, flags the ones that look like a retailer rather than a
racing team, and prints the alternatives. Read the table before `--apply`.
Anything it gets wrong you can fix in `config/companies.csv` by hand, and the
per-candidate override in the UI still outranks everything either way.
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import typer  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn  # noqa: E402
from rich.table import Table  # noqa: E402

from pitwall.config import get_settings  # noqa: E402
from pitwall.db import open_repos  # noqa: E402
from pitwall.enrich.base import SerpSearcher  # noqa: E402
from pitwall.enrich.checks import mx_check  # noqa: E402
from pitwall.enrich.domains import (  # noqa: E402
    domain_from_url,
    domain_label,
    domain_matches_company,
    is_company_domain,
)
from pitwall.extract.normalize import company_tokens  # noqa: E402
from pitwall.inputs import load_companies, save_companies  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__)
console = Console()

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
DEFAULT_ENGINES = "google,qwant,brave,duckduckgo,startpage,mojeek"

#: Sent to each candidate engine before the run. Two properties are checked:
#: the engine answers at all, and it **honours a quoted phrase**. The second is
#: what disqualifies Bing, which answers reliably and reads `"Iron Lynx"` as a
#: question about the element.
PROBE_QUERY = '"Scuderia Ferrari" official website'
PROBE_EXPECT = "ferrari"

#: Never a company's staff-email domain, whatever else it is. A merchandise
#: shop or a parts store is a separate property on a separate domain, and
#: `toyotagazooracingb2bshop.com` matches the full team name perfectly while
#: being exactly the wrong answer.
SHOP_MARKERS = (
    "shop", "store", "merch", "merchandise", "parts", "spares", "tickets",
    "hire", "rental", "dealer", "dealership", "usedcars", "boutique",
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
OFFICIAL_MARKERS = ("official", "home of", "welcome to", "| home")

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
    alternatives: list[Candidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.chosen) and (self.chosen.domain != (self.current or ""))


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


def coverage_of(domain: str, forms: list[str]) -> float:
    """How much of the company's name does this domain carry, at best?

    Scored against every name form and the best one wins: `tgr-e.com` covers
    none of "Toyota Gazoo Racing" and all of "TGR-E", and the second reading is
    the true one.
    """
    label = domain_label(domain)
    best = 0.0
    for form in forms:
        tokens = {t for t in company_tokens(form) if len(t) >= 3}
        if not tokens:
            continue
        best = max(best, sum(1 for t in tokens if t in label) / len(tokens))
    return best


def precision_of(domain: str, forms: list[str]) -> float:
    """How much of the domain label is the company's name, and nothing else?

    The counterweight to coverage. `afcorse` is mostly "corse"; `afcorseusa`
    carries a region as well; `toyotagazooracingb2bshop` carries a whole
    business line. A canonical domain is the name and little more.
    """
    label = domain_label(domain)
    if not label:
        return 0.0
    best = 0.0
    for form in forms:
        matched = sum(
            len(t) for t in {t for t in company_tokens(form) if len(t) >= 3} if t in label
        )
        best = max(best, min(1.0, matched / len(label)))
    return best


def looks_retail(domain: str, name: str) -> bool:
    label = domain_label(domain)
    lowered = name.lower()
    return any(m in label and m not in lowered for m in RETAIL_MARKERS)


def is_shop(domain: str) -> bool:
    return any(m in domain_label(domain) for m in SHOP_MARKERS)


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
        if not usable(domain):
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
            continue
        for rank, hit in enumerate(hits):
            domain = domain_from_url(hit.url)
            if not usable(domain):
                continue
            item = pool.setdefault(domain, Candidate(domain))
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
        if result.chosen.retail:
            result.warnings.append("the winner looks like a retail site — check it")
        if result.chosen.coverage < 0.5:
            result.warnings.append("weak name match — check it")
        if len(ranked) > 1 and ranked[0].score - ranked[1].score < 0.75:
            result.warnings.append(f"close call with {ranked[1].domain}")
    elif not result.warnings:
        result.warnings.append("nothing matched the company name")
    return result


# ────────────────────────────── the command ──────────────────────────────


@app.command()
def main(
    companies: str | None = typer.Option(None, help="comma-separated names; default all"),
    apply: bool = typer.Option(False, "--apply", help="write config/companies.csv"),
    show: bool = typer.Option(False, "--show", help="print what is already known and stop"),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="replace domains that are already filled in"
    ),
    no_mx: bool = typer.Option(False, "--no-mx", help="skip the MX check"),
    engines: str = typer.Option(
        DEFAULT_ENGINES, help="SearXNG engines to pin; see DEFAULT_ENGINES for why"
    ),
    qps: float = typer.Option(
        0.5, help="queries per second. Lower than the app default on purpose: "
        "this runs ~200 queries through one engine and the whole point is not "
        "to get that engine suspended halfway through",
    ),
    plain: bool = typer.Option(
        False, "--plain", help="tab-separated, untruncated — for reading and piping"
    ),
    cached: bool = typer.Option(
        False, "--cached",
        help="reuse cached SERPs. Off by default: a re-run is usually an attempt "
        "to get a better answer than the cached one",
    ),
) -> None:
    """Resolve one domain per company, and show the working."""
    settings = get_settings()
    repos = open_repos(settings)
    rows = repos.companies.all(active_only=False)

    if companies:
        wanted = {n.strip().casefold() for n in companies.split(",") if n.strip()}
        rows = [r for r in rows if r["name"].casefold() in wanted]
    if not rows:
        console.print("[red]no matching companies[/]")
        raise typer.Exit(1)

    if show:
        table = Table(title="known domains")
        table.add_column("company")
        table.add_column("domain")
        for row in rows:
            table.add_row(row["name"], row.get("domain") or "[dim]—[/]")
        console.print(table)
        filled = sum(1 for r in rows if r.get("domain"))
        console.print(f"{filled} of {len(rows)} filled in")
        raise typer.Exit(0)

    todo = rows if overwrite else [r for r in rows if not (r.get("domain") or "").strip()]
    if not todo:
        console.print("[green]every company already has a domain[/] — "
                      "use --overwrite to redo them")
        raise typer.Exit(0)

    candidates = [e.strip() for e in engines.split(",") if e.strip()]
    console.print(f"probing {len(candidates)} engine(s)…")
    healthy, rejected = probe_engines(settings, candidates)
    for engine, why in rejected:
        console.print(f"  [dim]·[/] {engine}: [yellow]{why}[/]")
    if not healthy:
        console.print(
            "\n[red]no search engine is both reachable and honouring quoted "
            "phrases right now.[/] They recover on their own — Google and Qwant "
            "each came back within a few hours during development. Try again "
            "later, or `--engines <name>` if you know one that works."
        )
        raise typer.Exit(1)
    console.print(f"  [green]using {', '.join(healthy)}[/]\n")

    settings = settings.with_overrides(
        searxng_engines=[",".join(healthy)], searxng_queries_per_second=qps
    )
    searcher = SerpSearcher(settings=settings, use_cache=cached, max_results=12)
    if not searcher.supports_operators():
        console.print(
            "[red]no search provider supports operators[/] — start SearXNG with "
            "`make searxng`."
        )
        raise typer.Exit(1)

    results: list[Result] = []
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(), TextColumn("{task.completed}/{task.total}"), TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("resolving", total=len(todo))
        for company in todo:
            progress.update(task, description=f"resolving {company['name'][:28]}")
            results.append(resolve(repos, searcher, company, check_mx=not no_mx))
            progress.advance(task)

    if plain:
        # Tab-separated and untruncated. A rich table wraps long domains into
        # `tridentmotorspor…`, which is unreadable for exactly the rows that
        # most need reading.
        for item in results:
            print(
                "\t".join([
                    item.name,
                    item.chosen.domain if item.chosen else "-",
                    item.chosen.why() if item.chosen else "; ".join(item.warnings),
                    ",".join(a.domain for a in item.alternatives),
                ])
            )
    else:
        table = Table(title=f"domains for {len(results)} companies")
        table.add_column("company", overflow="fold")
        table.add_column("domain", overflow="fold")
        table.add_column("why", overflow="fold")
        table.add_column("alternatives", overflow="fold")
        for item in results:
            style = "green"
            if not item.chosen:
                style = "red"
            elif item.warnings:
                style = "yellow"
            table.add_row(
                item.name,
                f"[{style}]{item.chosen.domain if item.chosen else '—'}[/]",
                item.chosen.why() if item.chosen else "; ".join(item.warnings),
                ", ".join(a.domain for a in item.alternatives) or "[dim]none[/]",
            )
        console.print(table)

        for item in results:
            for warning in item.warnings:
                if item.chosen:
                    console.print(f"[yellow]check[/] {item.name}: {warning}")

    found = [r for r in results if r.chosen]
    console.print(
        f"\n[green]{len(found)}[/] of {len(results)} resolved · "
        f"{sum(1 for r in found if r.warnings)} need a look"
    )

    if not apply:
        console.print(
            "\n[yellow]Nothing written.[/] Read the table, then re-run with "
            "[bold]--apply[/]. Anything wrong is one edit in config/companies.csv."
        )
        raise typer.Exit(0)

    # Write through the CSV, which is the source of truth; the next run syncs
    # it into the database. Writing the database directly would be undone by
    # the first `load_and_sync`.
    chosen = {r.name: r.chosen.domain for r in found}
    models = load_companies()
    written = 0
    for model in models:
        if model.name in chosen and (overwrite or not (model.domain or "").strip()):
            model.domain = chosen[model.name]
            written += 1
    path = save_companies(models)
    console.print(f"[green]wrote {written} domain(s)[/] -> {path}")
    console.print("Run [bold]pitwall validate[/] to re-sync them into the database.")


if __name__ == "__main__":
    app()
