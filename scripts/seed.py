#!/usr/bin/env python
"""Write `config/companies.csv` and `config/roles.csv` — §17 of PITWALL_PLAN.md.

Two sources:

* ``--source local`` (default when the files exist) converts the
  ``motorsport_teams.csv`` / ``motorsport_positions.csv`` research tables in the
  repository root into the two input files PitWall reads. This is the richer
  data: 49 teams × 17 positions, with the per-team×position priority matrix
  folded into the company tier and role priority.
* ``--source plan`` writes the starter tables printed in §17 of the plan
  (F1 2026 grid + feeder + endurance + suppliers).

**Verify entrant names against the current season before your first run** —
motorsport naming changes annually, and the `aliases` column is where that
volatility lives.
"""

from __future__ import annotations

import contextlib
import csv
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import typer  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from pitwall.config import config_dir  # noqa: E402
from pitwall.inputs import load_companies, load_roles  # noqa: E402
from pitwall.profiles import active as active_profile  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__)
console = Console()


def _announce_profile() -> None:
    """Which vocabulary the roles being written have to satisfy (§3.1)."""
    profile = active_profile()
    console.print(
        f"profile [cyan]{profile.name}[/] "
        f"({len(profile.role_families)} role families, "
        f"{len(profile.seniorities)} seniority levels)"
        + ("" if profile.path else "  [dim](built-in defaults)[/]")
    )

TEAMS_CSV = ROOT / "motorsport_teams.csv"
POSITIONS_CSV = ROOT / "motorsport_positions.csv"

# ────────────────────── local-source mapping tables ──────────────────────

#: target_position -> role family (the enum in models.ROLE_FAMILIES).
POSITION_FAMILY = {
    "Data Engineer": "data_engineering",
    "Telemetry Engineer": "data_engineering",
    "Data Analyst": "data_science",
    "Performance Analyst": "data_science",
    "Machine Learning Engineer": "data_science",
    "Race Strategy Engineer": "strategy",
    "Optimisation Engineer": "strategy",
    "Operations Research": "strategy",
    "Performance Engineer": "performance",
    "Simulation Engineer": "simulation",
    "Simulation & Modelling Engineer": "simulation",
    "Vehicle Performance Engineer": "vehicle_dynamics",
    "Software Engineer": "software",
    "Systems Engineer": "software",
    "Race Engineer": "operations",
    "Assistant Race Engineer": "operations",
    "Trackside Engineer": "operations",
}

#: Levels worth targeting per position — the entry routes get the wide net.
POSITION_SENIORITY = {
    "Data Engineer": "intern|graduate|junior|mid",
    "Data Analyst": "intern|graduate|junior|mid",
    "Performance Analyst": "intern|graduate|junior|mid",
    "Assistant Race Engineer": "graduate|junior|mid",
    "Trackside Engineer": "graduate|junior|mid",
    "Telemetry Engineer": "graduate|junior|mid",
    "Performance Engineer": "graduate|junior|mid",
    "Race Strategy Engineer": "graduate|junior|mid",
    "Simulation Engineer": "graduate|junior|mid",
    "Simulation & Modelling Engineer": "graduate|junior|mid",
    "Software Engineer": "graduate|junior|mid",
    "Systems Engineer": "graduate|junior|mid",
    "Vehicle Performance Engineer": "graduate|junior|mid",
    "Machine Learning Engineer": "graduate|junior|mid|senior",
    "Optimisation Engineer": "graduate|junior|mid|senior",
    "Operations Research": "graduate|junior|mid|senior",
    "Race Engineer": "mid|senior|lead",
}

#: Snippet keywords that boost the heuristic score for this role (§9.1).
POSITION_KEYWORDS = {
    "Data Engineer": "telemetry|logging|data pipeline|post-session|CAN bus",
    "Telemetry Engineer": "telemetry|sensors|logger|data acquisition|live link",
    "Data Analyst": "dashboard|SQL|telemetry|stint|competitor analysis",
    "Performance Analyst": "driver performance|benchmarking|sector|lap time",
    "Machine Learning Engineer": "machine learning|PyTorch|model|inference|applied scientist",
    "Race Strategy Engineer": "Monte Carlo|undercut|pit window|tyre degradation|safety car",
    "Optimisation Engineer": "optimisation|energy management|lap time simulation|solver",
    "Operations Research": "operations research|stochastic|decision science|scheduling",
    "Performance Engineer": "lap time|setup|telemetry|correlation",
    "Simulation Engineer": "driver in loop|simulator|DIL|correlation",
    "Simulation & Modelling Engineer": "vehicle model|tyre model|model validation|lap sim",
    "Vehicle Performance Engineer": "vehicle dynamics|tyre model|suspension|handling",
    "Software Engineer": "C++|Python|real-time|tools|internal tooling",
    "Systems Engineer": "control systems|electronics|embedded|ECU",
    "Race Engineer": "setup|driver comms|race engineering|strategy call",
    "Assistant Race Engineer": "assistant race engineer|number two|run plan",
    "Trackside Engineer": "trackside|support engineer|customer team|travel",
}

#: data_science_fit -> company tier (1 = dream target, drives query budget).
FIT_TO_TIER = {"Very High": 1, "High": 1, "Medium": 2, "Low": 3}

#: Manufacturer -> the road-car/consumer arm you must NOT match.
MANUFACTURER_NEGATIVES = {
    "Porsche": "dealership|Porsche Cars sales|road car sales",
    "BMW": "dealership|BMW sales|BMW Financial",
    "Mercedes-AMG": "dealership|Mercedes-Benz Cars|van sales",
    "Ferrari": "dealership|Ferrari North America sales|showroom",
    "Toyota": "dealership|Toyota Motor Sales|Toyota Financial",
    "Lexus": "dealership|Lexus sales",
    "Audi": "dealership|Audi sales|Audi Retail",
    "Genesis": "dealership|Genesis Motor sales|Hyundai retail",
    "Alpine": "Alpine Electronics|Alpine audio|alpine skiing|Alpine Bank",
    "Cadillac": "Cadillac dealership|GM sales|General Motors retail",
    "Corvette": "Chevrolet dealership|GM sales",
    "Ford": "Ford dealership|Ford Credit|Ford Motor Credit",
    "Peugeot": "dealership|Peugeot retail",
    "Lamborghini": "dealership|Lamborghini showroom",
    "Aston Martin": "Aston Martin Lagonda road cars|dealership|showroom",
    "McLaren": "McLaren Health Care|McLaren Automotive|hospital|Macomb",
}

#: Team-name collisions that are not covered by the manufacturer map.
TEAM_NEGATIVES = {
    "Hitech Grand Prix": "Hitech Systems|Hi-Tech Solutions|Hitech recruitment",
    "Hitech TGR": "Hitech Systems|Hi-Tech Solutions",
    "Manthey": "Manthey heating|Manthey construction",
    "Iron Lynx": "Lynx software|Lynx cargo",
    "Rodin Motorsport": "Rodin sculpture|Rodin museum",
    "Trident Motorsport": "Trident submarine|Trident seafoods|Trident systems",
    "Campos Racing": "Campos coffee",
    "MP Motorsport": "MP materials|MP Biomedicals",
    "Invicta Racing": "Invicta watches retail",
    "Akkodis ASP Team": "Akkodis staffing|Akkodis recruitment consultant",
    "Winward Racing": "Winward capital|Windward maritime",
    "Team WRT": "WRT radio|WRT construction",
    "BMW M Team WRT": "dealership|BMW sales",
    "AF Corse": "Corse coffee",
    "Emil Frey Racing": "Emil Frey dealership|Emil Frey retail",
    "Comtoyou Racing": "Comtoyou trading",
    "Herberth Motorsport": "Herberth construction",
    "Toksport WRT": "Toksport retail",
    "Grasser Racing Team": "Grasser construction",
    "Attempto Racing": "Attempto publishing|Attempto Verlag",
    "Haupt Racing Team": "Haupt pharmacy",
    "Getspeed": "Getspeed courier|speed test",
    "Optimum Motorsport": "Optimum nutrition|Optimum health",
    "United Autosports": "United Airlines|United Rentals",
}

#: Raw `series` text -> the compact code used in queries and filters.
SERIES_TOKENS = [
    ("WEC", "WEC"), ("Hypercar", "WEC"), ("LMGT3", "WEC"), ("Le Mans", "WEC"),
    ("IMSA", "IMSA"), ("ELMS", "ELMS"), ("Asian Le Mans", "ALMS"),
    ("GTWC", "GT"), ("GT World", "GT"), ("GT3", "GT"), ("DTM", "DTM"),
    ("Nurburgring", "NLS"), ("NLS", "NLS"), ("24H", "GT"),
    ("FIA F2", "F2"), ("FIA F3", "F3"), ("F1 Academy", "F1A"),
    ("Formula 2", "F2"), ("Formula 3", "F3"), ("Eurocup", "F3"),
    ("Formula Regional", "FRegional"), ("Euroformula", "F3"),
    ("Super Formula", "SF"), ("IndyCar", "IndyCar"), ("Formula E", "FE"),
]


def _split_semi(value: str) -> list[str]:
    return [p.strip() for p in (value or "").replace("|", ";").split(";") if p.strip()]


def _compact_series(raw: str) -> str:
    seen: list[str] = []
    for needle, code in SERIES_TOKENS:
        if needle.lower() in (raw or "").lower() and code not in seen:
            seen.append(code)
    return "/".join(seen) or "Motorsport"


def _team_negatives(team: str, manufacturer: str, also_known: list[str]) -> str:
    parts: list[str] = []
    for key, negs in MANUFACTURER_NEGATIVES.items():
        if key.lower() in (manufacturer or "").lower() or key.lower() in team.lower():
            parts += negs.split("|")
    if team in TEAM_NEGATIVES:
        parts += TEAM_NEGATIVES[team].split("|")
    for alias in also_known:
        if alias in TEAM_NEGATIVES:
            parts += TEAM_NEGATIVES[alias].split("|")
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return "|".join(out)


# ─────────────────────────── local conversion ────────────────────────────


def build_from_local() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if not TEAMS_CSV.exists() or not POSITIONS_CSV.exists():
        raise FileNotFoundError(
            f"expected {TEAMS_CSV.name} and {POSITIONS_CSV.name} in {ROOT}"
        )
    teams = list(csv.DictReader(TEAMS_CSV.open(encoding="utf-8-sig")))
    positions = list(csv.DictReader(POSITIONS_CSV.open(encoding="utf-8-sig")))

    companies: list[dict[str, str]] = []
    for t in teams:
        name = (t.get("team") or "").strip()
        if not name:
            continue
        # Aliases are alternate ORGANISATION names only. A bare manufacturer
        # brand ("Ferrari", "Porsche") is a surname and a road-car division as
        # often as it is the race team, so it goes to negative_keywords instead.
        aliases = _split_semi(t.get("also_known_as", ""))
        manufacturer = (t.get("manufacturer") or "").strip()
        companies.append(
            {
                "name": name,
                "aliases": "|".join(dict.fromkeys(aliases)),
                "negative_keywords": _team_negatives(name, manufacturer, aliases),
                "linkedin_slug": "",
                "domain": "",
                "series": _compact_series(t.get("series", "")),
                "country": (t.get("country") or "").split("(")[0].strip(),
                "tier": str(FIT_TO_TIER.get((t.get("data_science_fit") or "").strip(), 2)),
                "active": "true",
            }
        )

    # role priority comes from the per-team×position matrix in the positions file
    agg: dict[str, dict[str, object]] = defaultdict(
        lambda: {"prio": [], "titles": Counter()}
    )
    for row in positions:
        pos = (row.get("target_position") or "").strip()
        if not pos:
            continue
        raw = (row.get("priority") or "").strip()
        with contextlib.suppress(ValueError, IndexError):
            agg[pos]["prio"].append(int(raw[0]))  # type: ignore[union-attr]
        for alias in _split_semi(row.get("typical_title_at_this_team", "")):
            agg[pos]["titles"][alias] += 1  # type: ignore[index]

    roles: list[dict[str, str]] = []
    for pos, data in agg.items():
        prios: list[int] = data["prio"]  # type: ignore[assignment]
        mean = statistics.mean(prios) if prios else 3.0
        priority = 1 if mean <= 2.2 else (2 if mean <= 3.1 else 3)
        aliases = [
            a for a, _ in data["titles"].most_common()  # type: ignore[union-attr]
            if a.lower() != pos.lower()
        ][:5]
        roles.append(
            {
                "title": pos,
                "aliases": "|".join(aliases),
                "family": POSITION_FAMILY.get(pos, "data_science"),
                "seniority": POSITION_SENIORITY.get(pos, "graduate|junior|mid"),
                "keywords": POSITION_KEYWORDS.get(pos, ""),
                "priority": str(priority),
                "active": "true",
            }
        )
    roles.sort(key=lambda r: (int(r["priority"]), r["title"]))
    return companies, roles


# ──────────────────────── plan §17 starter tables ────────────────────────

PLAN_COMPANIES: list[tuple[str, ...]] = [
    # name, aliases, negative_keywords, series, country, tier
    ("McLaren Racing", "McLaren F1|McLaren Mastercard|McLaren Applied|MTC|McLaren Technology Centre", "Health Care|Automotive|hospital|Macomb", "F1", "UK", "1"),
    ("Mercedes-Benz Grand Prix", "Mercedes-AMG PETRONAS|Mercedes F1|Brackley|HPP|High Performance Powertrains", "Mercedes-Benz Cars|dealership|van", "F1", "UK", "1"),
    ("Red Bull Racing", "Oracle Red Bull Racing|Red Bull Technology|Red Bull Powertrains|RBR", "energy drink|Red Bull Media", "F1", "UK", "1"),
    ("Ferrari", "Scuderia Ferrari|Scuderia Ferrari HP|Ferrari S.p.A|Maranello", "dealership|Ferrari North America sales", "F1", "Italy", "1"),
    ("Williams Racing", "Atlassian Williams Racing|Williams Grand Prix Engineering|Williams Advanced Engineering", "Williams Companies|Williams Sonoma", "F1", "UK", "1"),
    ("Racing Bulls", "Visa Cash App RB|VCARB|AlphaTauri|Toro Rosso", "", "F1", "Italy", "2"),
    ("Aston Martin F1", "AMR GP|Aston Martin Aramco|AMR Technology Campus", "Aston Martin Lagonda road cars|dealership", "F1", "UK", "1"),
    ("Haas F1 Team", "MoneyGram Haas|TGR Haas|Haas Formula LLC", "Haas Automation|CNC", "F1", "USA/UK", "2"),
    ("Sauber Motorsport", "Audi F1|Audi Revolut F1 Team|Hinwil|Kick Sauber", "", "F1", "Switzerland", "1"),
    ("Alpine Racing", "BWT Alpine F1|Enstone|Alpine F1 Team", "Alpine audio|Alpine electronics|ski", "F1", "UK/France", "2"),
    ("Cadillac F1", "TWG Cadillac|Cadillac Formula 1 Team|Andretti Global", "Cadillac dealership|GM sales", "F1", "USA/UK", "1"),
    ("PREMA Racing", "Prema Powerteam", "", "F2/F3", "Italy", "2"),
    ("ART Grand Prix", "ART GP", "", "F2/F3", "France", "2"),
    ("Hitech Grand Prix", "Hitech GP", "Hitech Systems|Hi-Tech", "F2/F3", "UK", "2"),
    ("Rodin Motorsport", "Rodin Carlin|Carlin", "", "F2/F3", "UK", "2"),
    ("Campos Racing", "", "", "F2/F3", "Spain", "3"),
    ("MP Motorsport", "", "", "F2/F3", "Netherlands", "3"),
    ("Invicta Racing", "Virtuosi Racing", "", "F2/F3", "UK", "3"),
    ("Trident Motorsport", "", "", "F2/F3", "Italy", "3"),
    ("Van Amersfoort Racing", "VAR", "", "F3", "Netherlands", "3"),
    ("Toyota Gazoo Racing Europe", "TGR-E|TMG|Toyota Gazoo Racing WEC", "dealership|Toyota Motor Sales", "WEC", "Germany", "1"),
    ("Porsche Motorsport", "Porsche Penske Motorsport|Weissach", "dealership|Porsche Cars sales", "WEC/GT", "Germany", "1"),
    ("AF Corse", "Ferrari AF Corse", "", "WEC/GT", "Italy", "2"),
    ("Hertz Team JOTA", "JOTA Sport|Cadillac Hertz Team JOTA", "Hertz rental", "WEC", "UK", "2"),
    ("Peugeot Sport", "Peugeot TotalEnergies", "dealership", "WEC", "France", "2"),
    ("BMW M Motorsport", "BMW M Team WRT|WRT|Team WRT", "dealership|BMW sales", "WEC/GT", "Germany/Belgium", "2"),
    ("Alpine Endurance Team", "Signatech Alpine", "Alpine audio", "WEC", "France", "3"),
    ("Manthey", "Manthey EMA|Manthey Racing", "", "GT", "Germany", "3"),
    ("Iron Lynx", "Iron Dames", "", "GT", "Italy", "3"),
    ("Akkodis ASP Team", "ASP Team", "", "GT", "France", "3"),
    ("Pirelli Motorsport", "Pirelli F1", "tyre retail|dealership", "Supplier", "Italy", "2"),
    ("Cosworth", "Cosworth Electronics", "", "Supplier", "UK", "2"),
    ("AVL Racing", "AVL List", "", "Supplier", "Austria", "3"),
    ("Catapult Sports", "Catapult Motorsport", "", "Analytics", "Australia/UK", "3"),
    ("Sportradar", "Sportradar Motorsport", "", "Analytics", "Switzerland", "3"),
]

PLAN_ROLES: list[tuple[str, ...]] = [
    # title, aliases, family, seniority, keywords, priority
    ("Data Scientist", "Data Science|Senior Data Scientist|Graduate Data Scientist", "data_science", "intern|graduate|junior|mid", "machine learning|predictive|modelling", "1"),
    ("Machine Learning Engineer", "ML Engineer|MLOps Engineer|AI Engineer", "data_science", "graduate|junior|mid", "PyTorch|model deployment|inference", "1"),
    ("Data Engineer", "Data Platform Engineer|Analytics Engineer", "data_engineering", "graduate|junior|mid", "pipeline|Spark|Databricks|Kafka", "1"),
    ("Data Analyst", "Performance Data Analyst|Motorsport Data Analyst", "data_science", "intern|graduate|junior", "dashboard|SQL|telemetry", "1"),
    ("Race Strategy Engineer", "Strategy Engineer|Race Strategist|Strategy Analyst", "strategy", "graduate|junior|mid", "Monte Carlo|undercut|pit window|tyre degradation", "1"),
    ("Performance Engineer", "Track Performance Engineer|Race Support Engineer", "performance", "graduate|junior|mid", "telemetry|lap time|setup", "1"),
    ("Simulation Engineer", "Vehicle Simulation Engineer|Driver-in-Loop Engineer|DIL Engineer", "simulation", "graduate|junior|mid", "driver in loop|lap sim|model validation", "2"),
    ("Vehicle Dynamics Engineer", "VD Engineer|Vehicle Performance Engineer", "vehicle_dynamics", "graduate|junior|mid", "tyre model|suspension|handling", "2"),
    ("Aerodynamics Engineer", "Aero Engineer|CFD Engineer", "performance", "graduate|junior", "CFD|wind tunnel|OpenFOAM", "2"),
    ("Software Engineer", "Software Developer|Backend Engineer|Tools Engineer", "software", "graduate|junior|mid", "C++|Python|real-time", "2"),
    ("Head of Data Science", "Data Science Lead|Head of Analytics|Principal Data Scientist", "data_science", "lead|head", "", "1"),
    ("Head of Race Strategy", "Chief Strategist|Strategy Lead|Head of Strategy", "strategy", "lead|head", "", "1"),
    ("Motorsport Analyst", "Racing Analyst|Performance Analyst", "data_science", "junior|mid", "timing|sector|stint", "2"),
    ("Research Engineer", "Applied Scientist|R&D Engineer", "data_science", "graduate|junior|mid", "optimisation|research", "3"),
]

COMPANY_HEADER = [
    "name", "aliases", "negative_keywords", "linkedin_slug", "domain",
    "series", "country", "tier", "active",
]
ROLE_HEADER = ["title", "aliases", "family", "seniority", "keywords", "priority", "active"]


def build_from_plan() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    companies = [
        {
            "name": n, "aliases": a, "negative_keywords": neg, "linkedin_slug": "",
            "domain": "", "series": s, "country": c, "tier": t, "active": "true",
        }
        for n, a, neg, s, c, t in PLAN_COMPANIES
    ]
    roles = [
        {
            "title": t, "aliases": a, "family": f, "seniority": s,
            "keywords": k, "priority": p, "active": "true",
        }
        for t, a, f, s, k, p in PLAN_ROLES
    ]
    return companies, roles


def _write(rows: list[dict[str, str]], header: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


@app.command()
def main(
    source: str = typer.Option(
        "auto", help="auto | local (motorsport_*.csv in the repo root) | plan (§17 starter data)"
    ),
    force: bool = typer.Option(False, "--force", help="overwrite existing config CSVs"),
    merge: bool = typer.Option(
        False, "--merge", help="keep rows already in config/*.csv and add only new ones"
    ),
) -> None:
    """Write config/companies.csv and config/roles.csv."""
    _announce_profile()
    if source == "auto":
        source = "local" if TEAMS_CSV.exists() and POSITIONS_CSV.exists() else "plan"

    companies, roles = build_from_local() if source == "local" else build_from_plan()

    cpath, rpath = config_dir() / "companies.csv", config_dir() / "roles.csv"
    if merge:
        companies = _merge_rows(cpath, companies, "name")
        roles = _merge_rows(rpath, roles, "title")
    elif (cpath.exists() or rpath.exists()) and not force:
        console.print(
            f"[yellow]{cpath.name} / {rpath.name} already exist — pass --force to overwrite "
            "or --merge to add only new rows.[/]"
        )
        raise typer.Exit(1)

    _write(companies, COMPANY_HEADER, cpath)
    _write(roles, ROLE_HEADER, rpath)

    loaded_c, creport = load_companies(cpath)
    loaded_r, rreport = load_roles(rpath)

    table = Table(title=f"seeded from source={source}")
    table.add_column("file")
    table.add_column("rows", justify="right")
    table.add_column("valid", justify="right")
    table.add_column("errors", justify="right", style="red")
    table.add_column("warnings", justify="right", style="yellow")
    table.add_row("companies.csv", str(len(companies)), str(len(loaded_c)),
                  str(len(creport.errors)), str(len(creport.warnings)))
    table.add_row("roles.csv", str(len(roles)), str(len(loaded_r)),
                  str(len(rreport.errors)), str(len(rreport.warnings)))
    console.print(table)
    for issue in creport.errors + rreport.errors:
        console.print(f"[red]row {issue.row}: {issue.message}[/]")
    console.print(f"[green]wrote[/] {cpath}\n[green]wrote[/] {rpath}")


def _merge_rows(path: Path, new_rows: list[dict[str, str]], key: str) -> list[dict[str, str]]:
    if not path.exists():
        return new_rows
    existing = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    seen = {(r.get(key) or "").strip().casefold() for r in existing}
    merged = existing + [r for r in new_rows if r[key].strip().casefold() not in seen]
    return merged


if __name__ == "__main__":
    app()
