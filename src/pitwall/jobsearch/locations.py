"""Where a job is — §58 (city → country) and §59 (what "remote" means).

A posting says "London", "München", "Cambridge, MA", "Hamburg, München,
Düsseldorf, remote" or "Remote – US only", and the filters need a set of
countries and an honest remote flag. Everything here is a lookup table plus a
tokenizer; nothing guesses. A location nobody can place comes back with no
countries, and the filters treat that as unknown — which passes.

Country codes are ISO 3166 alpha-2 throughout, with the UK as `GB`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# ───────────────────────────── countries ─────────────────────────────────

#: code -> (display name, other names a posting may use)
COUNTRIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "AT": ("Austria", ("österreich", "osterreich", "autriche")),
    "BE": ("Belgium", ("belgique", "belgië", "belgie", "belgien")),
    "BG": ("Bulgaria", ()),
    "CH": ("Switzerland", ("schweiz", "suisse", "svizzera", "swiss")),
    "CY": ("Cyprus", ()),
    "CZ": ("Czechia", ("czech republic", "česko", "cesko")),
    "DE": ("Germany", ("deutschland", "allemagne", "germania", "de")),
    "DK": ("Denmark", ("danmark", "dänemark")),
    "EE": ("Estonia", ("eesti",)),
    "ES": ("Spain", ("españa", "espana", "spanien", "espagne")),
    "FI": ("Finland", ("suomi", "finnland")),
    "FR": ("France", ("frankreich", "francia")),
    "GB": ("United Kingdom", ("uk", "u.k.", "great britain", "britain", "england",
                              "scotland", "wales", "northern ireland", "gb")),
    "GR": ("Greece", ("hellas",)),
    "HR": ("Croatia", ("hrvatska",)),
    "HU": ("Hungary", ("magyarország",)),
    "IE": ("Ireland", ("éire", "eire", "republic of ireland")),
    "IS": ("Iceland", ()),
    "IT": ("Italy", ("italia", "italien", "italie")),
    "LT": ("Lithuania", ("lietuva",)),
    "LU": ("Luxembourg", ("luxemburg",)),
    "LV": ("Latvia", ("latvija",)),
    "MT": ("Malta", ()),
    "NL": ("Netherlands", ("the netherlands", "nederland", "holland", "niederlande",
                           "pays-bas")),
    "NO": ("Norway", ("norge", "norwegen")),
    "PL": ("Poland", ("polska", "polen")),
    "PT": ("Portugal", ()),
    "RO": ("Romania", ("românia",)),
    "SE": ("Sweden", ("sverige", "schweden", "suède")),
    "SI": ("Slovenia", ("slovenija",)),
    "SK": ("Slovakia", ("slovensko",)),
    "UA": ("Ukraine", ("україна",)),
    "US": ("United States", ("usa", "u.s.", "u.s.a.", "united states of america",
                             "america", "us")),
    "CA": ("Canada", ()),
    "AU": ("Australia", ()),
    "IL": ("Israel", ()),
    "IN": ("India", ()),
    "JP": ("Japan", ()),
    "HK": ("Hong Kong", ("hongkong", "hong kong sar")),
    "CN": ("China", ("mainland china", "prc")),
    "SG": ("Singapore", ()),
    "AE": ("United Arab Emirates", ("uae", "dubai")),
}

EU = frozenset({
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR",
    "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK",
})
#: "Europe" as job postings use it: the EU plus its neighbours in the market.
EUROPE = EU | {"GB", "CH", "NO", "IS", "UA"}

_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
_US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho", "illinois",
    "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland",
    "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana",
    "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah",
    "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia",
}

#: Sub-national regions that name their country unambiguously.
_REGIONS: dict[str, str] = {
    "bavaria": "DE", "bayern": "DE", "baden-württemberg": "DE", "baden-wurttemberg": "DE",
    "hesse": "DE", "hessen": "DE", "north rhine-westphalia": "DE",
    "nordrhein-westfalen": "DE", "nrw": "DE", "lower saxony": "DE",
    "niedersachsen": "DE", "saxony": "DE", "sachsen": "DE",
    "île-de-france": "FR", "ile-de-france": "FR", "auvergne-rhône-alpes": "FR",
    "nord": "FR", "rhône": "FR", "bouches-du-rhône": "FR", "essonne": "FR",
    "landes": "FR", "hauts-de-seine": "FR",
    "greater london": "GB", "oxfordshire": "GB", "northamptonshire": "GB",
    "surrey": "GB", "cambridgeshire": "GB", "buckinghamshire": "GB",
    "lombardy": "IT", "lombardia": "IT", "emilia-romagna": "IT", "piedmont": "IT",
    "catalonia": "ES", "cataluña": "ES", "catalunya": "ES", "community of madrid": "ES",
    "north holland": "NL", "noord-holland": "NL", "south holland": "NL",
    "zuid-holland": "NL", "north brabant": "NL", "noord-brabant": "NL",
    "lisbon district": "PT", "porto district": "PT",
    "ontario": "CA", "british columbia": "CA", "quebec": "CA",
}

#: city (lower case, several spellings) -> country. Deliberately a table:
#: the whole point of §58 is that "London" should never need a guess.
CITIES: dict[str, str] = {
    # Germany
    "berlin": "DE", "munich": "DE", "münchen": "DE", "munchen": "DE", "muenchen": "DE",
    "stuttgart": "DE", "cologne": "DE", "köln": "DE", "koln": "DE", "koeln": "DE",
    "hamburg": "DE", "frankfurt": "DE", "frankfurt am main": "DE", "düsseldorf": "DE",
    "dusseldorf": "DE", "duesseldorf": "DE", "leipzig": "DE", "dresden": "DE",
    "heidelberg": "DE", "karlsruhe": "DE", "darmstadt": "DE", "aachen": "DE",
    "nuremberg": "DE", "nürnberg": "DE", "nurnberg": "DE", "hannover": "DE",
    "hanover": "DE", "bonn": "DE", "dortmund": "DE", "essen": "DE", "mannheim": "DE",
    "ingolstadt": "DE", "wolfsburg": "DE", "regensburg": "DE", "freiburg": "DE",
    "potsdam": "DE", "bremen": "DE", "tübingen": "DE", "tubingen": "DE",
    "garching": "DE", "sindelfingen": "DE", "weissach": "DE", "celle": "DE",
    "gudensberg": "DE", "walldorf": "DE", "erlangen": "DE", "ulm": "DE",
    # Netherlands
    "amsterdam": "NL", "rotterdam": "NL", "the hague": "NL", "den haag": "NL",
    "utrecht": "NL", "eindhoven": "NL", "delft": "NL", "leiden": "NL",
    "groningen": "NL", "veldhoven": "NL", "amstelveen": "NL", "haarlem": "NL",
    # France
    "paris": "FR", "lyon": "FR", "toulouse": "FR", "marseille": "FR", "lille": "FR",
    "grenoble": "FR", "nice": "FR", "sophia antipolis": "FR", "bordeaux": "FR",
    "nantes": "FR", "rennes": "FR", "montpellier": "FR", "strasbourg": "FR",
    "saclay": "FR", "palaiseau": "FR", "dijon": "FR", "magny-cours": "FR",
    "hossegor": "FR", "lisses": "FR", "la défense": "FR", "la defense": "FR",
    "boulogne-billancourt": "FR", "issy-les-moulineaux": "FR", "viry-châtillon": "FR",
    # United Kingdom
    "london": "GB", "manchester": "GB", "edinburgh": "GB", "glasgow": "GB",
    "cambridge": "GB", "oxford": "GB", "bristol": "GB", "birmingham": "GB",
    "leeds": "GB", "reading": "GB", "milton keynes": "GB", "brackley": "GB",
    "enstone": "GB", "silverstone": "GB", "woking": "GB", "grove": "GB",
    "banbury": "GB", "maidenhead": "GB", "guildford": "GB", "belfast": "GB",
    "cardiff": "GB", "newcastle": "GB", "sheffield": "GB", "nottingham": "GB",
    "southampton": "GB", "bath": "GB", "coventry": "GB", "warwick": "GB",
    "slough": "GB", "hemel hempstead": "GB", "bicester": "GB", "towcester": "GB",
    "northampton": "GB", "didcot": "GB", "harwell": "GB",
    # Ireland
    "dublin": "IE", "cork": "IE", "galway": "IE", "limerick": "IE",
    # Switzerland
    "zurich": "CH", "zürich": "CH", "zuerich": "CH", "geneva": "CH", "genève": "CH",
    "geneve": "CH", "basel": "CH", "lausanne": "CH", "bern": "CH", "zug": "CH",
    "lugano": "CH", "hinwil": "CH",
    # Nordics
    "stockholm": "SE", "gothenburg": "SE", "göteborg": "SE", "goteborg": "SE",
    "malmö": "SE", "malmo": "SE", "lund": "SE", "uppsala": "SE",
    "helsinki": "FI", "espoo": "FI", "tampere": "FI", "oulu": "FI",
    "copenhagen": "DK", "københavn": "DK", "kobenhavn": "DK", "aarhus": "DK",
    "oslo": "NO", "bergen": "NO", "trondheim": "NO", "reykjavik": "IS",
    # Southern Europe
    "milan": "IT", "milano": "IT", "rome": "IT", "roma": "IT", "turin": "IT",
    "torino": "IT", "bologna": "IT", "florence": "IT", "firenze": "IT",
    "maranello": "IT", "modena": "IT", "naples": "IT", "napoli": "IT",
    "padua": "IT", "padova": "IT", "pisa": "IT", "genoa": "IT", "genova": "IT",
    "trento": "IT", "faenza": "IT", "monza": "IT", "imola": "IT",
    "madrid": "ES", "barcelona": "ES", "valencia": "ES", "seville": "ES",
    "sevilla": "ES", "bilbao": "ES", "malaga": "ES", "málaga": "ES",
    "lisbon": "PT", "lisboa": "PT", "porto": "PT", "oporto": "PT", "braga": "PT",
    "coimbra": "PT", "aveiro": "PT", "faro": "PT",
    "athens": "GR", "valletta": "MT",
    # Central / Eastern Europe
    "vienna": "AT", "wien": "AT", "graz": "AT", "linz": "AT", "salzburg": "AT",
    "innsbruck": "AT",
    "brussels": "BE", "bruxelles": "BE", "brussel": "BE", "antwerp": "BE",
    "antwerpen": "BE", "ghent": "BE", "gent": "BE", "leuven": "BE", "liège": "BE",
    "luxembourg city": "LU",
    "warsaw": "PL", "warszawa": "PL", "krakow": "PL", "kraków": "PL",
    "wroclaw": "PL", "wrocław": "PL", "gdansk": "PL", "gdańsk": "PL", "poznan": "PL",
    "prague": "CZ", "praha": "CZ", "brno": "CZ", "budapest": "HU",
    "bucharest": "RO", "cluj-napoca": "RO", "sofia": "BG", "zagreb": "HR",
    "ljubljana": "SI", "bratislava": "SK", "tallinn": "EE", "riga": "LV",
    "vilnius": "LT", "kyiv": "UA", "kiev": "UA", "lviv": "UA",
    # United States
    "san francisco": "US", "new york": "US", "new york city": "US", "nyc": "US",
    "seattle": "US", "boston": "US", "austin": "US", "los angeles": "US",
    "chicago": "US", "mountain view": "US", "palo alto": "US", "menlo park": "US",
    "sunnyvale": "US", "san jose": "US", "redmond": "US", "bellevue": "US",
    "san diego": "US", "washington dc": "US", "washington, d.c.": "US",
    "denver": "US", "atlanta": "US", "pittsburgh": "US", "philadelphia": "US",
    "detroit": "US", "miami": "US", "dallas": "US", "houston": "US",
    "salt lake city": "US", "raleigh": "US", "charlotte": "US", "indianapolis": "US",
    "cupertino": "US", "santa clara": "US", "redwood city": "US", "san mateo": "US",
    "oakland": "US", "berkeley": "US", "portland": "US", "minneapolis": "US",
    "ann arbor": "US", "princeton": "US", "new haven": "US", "somerville": "US",
    "mooresville": "US", "concord": "US",
    # Elsewhere
    "toronto": "CA", "montreal": "CA", "montréal": "CA", "vancouver": "CA",
    "waterloo": "CA", "tel aviv": "IL", "singapore": "SG", "sydney": "AU",
    "melbourne": "AU", "bangalore": "IN", "bengaluru": "IN", "tokyo": "JP",
}

#: Native and ASCII spellings -> the one name shown and compared, so a job in
#: "München" meets a preferred city written "Munich", and two sources spelling
#: the same city differently still dedupe.
_CITY_CANON: dict[str, str] = {
    "münchen": "munich", "munchen": "munich", "muenchen": "munich",
    "köln": "cologne", "koln": "cologne", "koeln": "cologne",
    "düsseldorf": "dusseldorf", "duesseldorf": "dusseldorf",
    "frankfurt am main": "frankfurt", "nürnberg": "nuremberg", "nurnberg": "nuremberg",
    "hanover": "hannover", "tübingen": "tubingen",
    "den haag": "the hague", "zürich": "zurich", "zuerich": "zurich",
    "genève": "geneva", "geneve": "geneva", "göteborg": "gothenburg",
    "goteborg": "gothenburg", "malmö": "malmo", "københavn": "copenhagen",
    "kobenhavn": "copenhagen", "milano": "milan", "roma": "rome", "torino": "turin",
    "firenze": "florence", "napoli": "naples", "padova": "padua", "genova": "genoa",
    "sevilla": "seville", "málaga": "malaga", "lisboa": "lisbon", "oporto": "porto",
    "wien": "vienna", "bruxelles": "brussels", "brussel": "brussels",
    "antwerpen": "antwerp", "gent": "ghent", "warszawa": "warsaw", "kraków": "krakow",
    "wrocław": "wroclaw", "gdańsk": "gdansk", "praha": "prague", "kiev": "kyiv",
    "new york city": "new york", "nyc": "new york", "montréal": "montreal",
    "bengaluru": "bangalore", "la défense": "la defense", "washington, d.c.": "washington dc",
}


def canonical_city(city: str) -> str:
    folded = _fold(city)
    return _CITY_CANON.get(folded, folded).title()


#: Cities that exist on both sides of the Atlantic. The table's answer is the
#: default; a US state anywhere in the same string moves them to the US.
_TRANSATLANTIC = {"cambridge", "birmingham", "portland", "concord", "reading", "bath",
                  "manchester", "grove"}

# ───────────────────────────── work mode ─────────────────────────────────

_REMOTE_RE = re.compile(
    r"\b(?:remote|fully[ -]remote|remote[ -]first|work from home|wfh|home[ -]?office|"
    r"telecommut\w*|t[ée]l[ée]travail|anywhere)\b",
    re.I,
)
_HYBRID_RE = re.compile(r"\b(?:hybrid|hybride|flexible working|partly remote)\b", re.I)
_ONSITE_RE = re.compile(r"\b(?:on[- ]?site|in[- ]office|office[- ]based|vor ort|sur site)\b",
                        re.I)

#: How postings name the region a remote job is open to.
_REMOTE_REGIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:worldwide|anywhere|global(?:ly)?|any location)\b", re.I), "WORLD"),
    (re.compile(r"\bemea\b", re.I), "EMEA"),
    (re.compile(r"\b(?:eu|european union)\b", re.I), "EU"),
    (re.compile(r"\beurope(?:an)?\b|\bcet\b|\bcest\b", re.I), "EUROPE"),
    (re.compile(r"\b(?:north america|americas)\b", re.I), "NA"),
)


# ──────────────────────────────── API ────────────────────────────────────


def _fold(text: str) -> str:
    return " ".join(text.strip().lower().split())


_ALIAS_TO_CODE: dict[str, str] = {}
for _code, (_name, _aliases) in COUNTRIES.items():
    _ALIAS_TO_CODE[_name.lower()] = _code
    for _alias in _aliases:
        _ALIAS_TO_CODE[_alias] = _code


def country_code(value: str | None) -> str | None:
    """`"Germany"`, `"deutschland"`, `"DE"`, `"uk"` -> ISO alpha-2, else None."""
    if not value:
        return None
    raw = value.strip()
    if len(raw) == 2 and raw.upper() in COUNTRIES:
        return "GB" if raw.upper() == "UK" else raw.upper()
    if raw.upper() == "UK":
        return "GB"
    return _ALIAS_TO_CODE.get(_fold(raw))


def country_name(code: str | None) -> str:
    if not code:
        return ""
    entry = COUNTRIES.get(code.upper())
    return entry[0] if entry else code


def city_country(city: str | None) -> str | None:
    return CITIES.get(_fold(city)) if city else None


#: Remote-region codes that are not countries. "EU" and "NA" are two letters
#: long, and code that took "two letters" to mean "a country" read every
#: "Remote (EU)" job as remote-within-a-country-called-EU: closed to everyone.
REGION_CODES = frozenset({"EU", "EUROPE", "EMEA", "NA", "WORLD"})


def is_country_code(code: str | None) -> bool:
    return bool(code) and len(str(code)) == 2 and str(code).upper() not in REGION_CODES


def region_contains(region: str | None, country: str | None) -> bool:
    """Is `country` inside a remote region like EU / EUROPE / EMEA / WORLD / NA?"""
    if not region or not country:
        return False
    region = region.upper()
    if region == "WORLD":
        return True
    if region == "EU":
        return country in EU
    if region in ("EUROPE", "EMEA"):
        return country in EUROPE or region == "EMEA" and country in {"AE", "IL"}
    if region == "NA":
        return country in {"US", "CA"}
    return region == country


@dataclass(frozen=True)
class Location:
    countries: tuple[str, ...] = ()
    cities: tuple[str, ...] = ()
    remote_status: str = "unknown"          # remote | hybrid | onsite | unknown
    remote_region: str | None = None        # ISO2, EU, EUROPE, EMEA, NA, WORLD
    #: Every region a remote job names, as alternatives: "Remote — US or
    #: Canada" is (US, CA), and someone in either can take it. Empty when the
    #: posting names none, which is "unknown", never "anywhere".
    remote_regions: tuple[str, ...] = ()

    @property
    def country(self) -> str | None:
        return self.countries[0] if self.countries else None

    @property
    def city(self) -> str | None:
        return self.cities[0] if self.cities else None


_SPLIT_RE = re.compile(r"\s*(?:[,;/|·•\n()\[\]]|\s[-–—]\s|\bor\b|\band\b|&)\s*", re.I)


def _strip_accents_safe(token: str) -> str:
    """Keep accents (the table has both forms) but drop stray punctuation."""
    token = unicodedata.normalize("NFC", token)
    token = token.strip(" .:-–—\t\"'")
    # "US only", "UK-based", "EU residents": the qualifier is not the place.
    return re.sub(r"(?i)[\s-]+(?:only|based|residents?|citizens?)$", "", token)


@dataclass
class _Found:
    countries: list[str] = field(default_factory=list)
    cities: list[tuple[str, str]] = field(default_factory=list)   # (city, country)
    us_state: bool = False

    def country(self, code: str | None) -> None:
        if code and code not in self.countries:
            self.countries.append(code)


def parse_location(
    text: str | None,
    *,
    remote_flag: bool | None = None,
    workplace: str | None = None,
    country_hint: str | None = None,
    title: str | None = None,
) -> Location:
    """Read one posting's location.

    `remote_flag` and `workplace` are what the source itself states (Arbeitnow's
    `remote`, Lever/Ashby's `workplaceType`) and outrank the text. `title` is
    read only for its work mode — "Data Scientist (Remote, EU)" is common.
    """
    found = _Found()
    raw = text or ""
    for part in _SPLIT_RE.split(raw):
        token = _strip_accents_safe(part)
        if not token:
            continue
        folded = _fold(token)
        if len(token) == 2 and token.isupper() and token in _US_STATES and token not in {
            "DE", "IN", "ME", "OR", "CA"
        }:
            found.us_state = True
            found.country("US")
            continue
        if token in {"CA", "DE", "IN", "ME", "OR"} and len(token) == 2 and token.isupper():
            # Two-letter codes that are also US states: CA and DE read as the
            # state only next to a US city, otherwise as the country.
            if any(c == "US" for _, c in found.cities):
                found.us_state = True
                found.country("US")
            else:
                found.country(country_code(token))
            continue
        if folded in _US_STATE_NAMES:
            found.us_state = True
            found.country("US")
            continue
        code = country_code(token) if len(folded) > 2 or token.isupper() else None
        if code:
            found.country(code)
            continue
        if folded in _REGIONS:
            found.country(_REGIONS[folded])
            continue
        city = city_country(folded)
        if city is None:
            # "Paris office", "Berlin HQ", "Munich, Germany (Hybrid)"
            head = folded.split(" ")[0]
            city = CITIES.get(head)
            if city:
                folded = head
        if city:
            found.cities.append((folded, city))

    cities: list[str] = []
    for city, code in found.cities:
        if found.us_state and city in _TRANSATLANTIC:
            code = "US"
        found.country(code)
        name = canonical_city(city)
        if name not in cities:
            cities.append(name)
    hint = country_code(country_hint) if country_hint else None
    if hint and hint not in found.countries:
        found.countries.insert(0, hint)

    # ── work mode: the source's own statement first, then the words ──
    mode_text = f"{raw} {title or ''}"
    status = "unknown"
    wp = (workplace or "").strip().lower().replace("_", "-")
    if wp in {"remote"}:
        status = "remote"
    elif wp in {"hybrid"}:
        status = "hybrid"
    elif wp in {"onsite", "on-site", "office", "in-office"}:
        status = "onsite"
    elif _HYBRID_RE.search(mode_text):
        status = "hybrid"
    elif remote_flag is True or _REMOTE_RE.search(mode_text):
        status = "remote"
    elif remote_flag is False and found.countries or _ONSITE_RE.search(mode_text):
        status = "onsite"

    regions: list[str] = []
    if status == "remote":
        for pattern, name in _REMOTE_REGIONS:
            if pattern.search(mode_text) and name not in regions:
                regions.append(name)
        if "WORLD" in regions:
            regions = ["WORLD"]
        else:
            # Countries named beside the work mode are where it is open:
            # "Remote - UK", "Remote (US, Canada)", "Remote EU or UK". One the
            # named region already contains adds nothing.
            for code in found.countries:
                if code not in regions and not any(region_contains(r, code) for r in regions):
                    regions.append(code)
    return Location(
        countries=tuple(found.countries),
        cities=tuple(cities),
        remote_status=status,
        remote_region=regions[0] if regions else None,
        remote_regions=tuple(regions),
    )
