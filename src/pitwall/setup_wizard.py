"""First-run setup — §3.2 of PITWALL_V2_IMPLEMENTATION.md.

The logic behind `pitwall init`, kept out of the CLI module so it can be tested
without driving a terminal. Six steps, in the order the brief lists them:

1. the Python version, and the Anaconda trap by name
2. `config/` with sample CSVs — commented headers and a few real-looking rows
3. `.env.example` -> `.env`
4. which LLM path: a free Groq key, or local Ollama
5. offer to start the SearXNG container
6. the existing `inputs.py` validation, printed

Nothing here overwrites a file that already exists unless it is asked to. A
half-finished setup that gets re-run should be finished, not reset.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import config_dir, project_root  # noqa: F401
from .profiles import active as active_profile

MIN_PYTHON = (3, 11)

#: Two companies and three roles, close enough to real that the columns explain
#: themselves. Deliberately not the owner's list — a friend cloning this should
#: not inherit 49 motorsport teams they have never heard of.
SAMPLE_COMPANIES = """\
# config/companies.csv — one row per organisation you want to search.
#
# name               required, and the canonical spelling. Everything keys on it.
# aliases            pipe-separated. How the company is written in the wild:
#                    season names, sponsor names, the short form people type.
#                    The single biggest lever on RECALL.
# negative_keywords  pipe-separated. Other organisations that share the name.
#                    The single biggest lever on PRECISION — "McLaren Health
#                    Care" is a hospital network, not a racing team.
# linkedin_slug      optional, unused by search; handy for your own reference
# domain             optional; used to guess the ATS board in `discover_ats.py`
# series             optional, free text
# country            optional; a candidate located here scores slightly higher
# tier               1-3. 1 is searched first and most.
# active             true/false. false keeps the row without searching it.
name,aliases,negative_keywords,linkedin_slug,domain,series,country,tier,active
Example Racing,Example F1|Example Motorsport,Example Health|Example Bank,,example.com,F1,UK,1,true
Second Example,Second Ex|SE Racing,Second Example Insurance,,secondexample.com,WEC,Italy,2,true
"""

SAMPLE_ROLES = """\
# config/roles.csv — one row per role you want to find people in.
#
# title      required, and the canonical spelling
# aliases    pipe-separated. What the same job is called elsewhere.
# family     one of the families in your profile (config/profiles/<name>.yaml).
#            Run `pitwall validate` and it will list the legal values.
# seniority  pipe-separated, from the profile's ladder. Empty means any.
# keywords   pipe-separated. Words that, in a snippet, suggest this is really
#            the job — each one is worth a few points in the heuristic layer.
# priority   1-3. 1 is searched first and most.
# active     true/false
title,aliases,family,seniority,keywords,priority,active
Data Scientist,Data Science|ML Engineer,data_science,graduate|junior|mid,simulation|modelling|Monte Carlo,1,true
Data Engineer,Data Systems Engineer,data_engineering,junior|mid|senior,telemetry|pipeline|logging,2,true
Performance Engineer,Performance Analyst,performance,mid|senior,aerodynamics|vehicle performance,2,true
"""


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""
    action: str = ""


@dataclass
class InitResult:
    steps: list[Step] = field(default_factory=list)
    created: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    def add(self, name: str, ok: bool, detail: str = "", action: str = "") -> Step:
        step = Step(name, ok, detail, action)
        self.steps.append(step)
        return step


# ─────────────────────────────── step 1 ──────────────────────────────────


def check_python() -> Step:
    """The interpreter, and the Anaconda trap by name.

    The system `python3` on macOS is very often Anaconda's, whose
    pydantic-settings predates the `NoDecode` import in config.py. The failure
    that produces says "cannot import name 'NoDecode'", which names the symbol
    and not the cause — so this names the cause.
    """
    version = ".".join(str(p) for p in sys.version_info[:3])
    executable = sys.executable
    if sys.version_info < MIN_PYTHON:
        return Step(
            "python", False, f"{version} at {executable}",
            f"PitWall needs Python >= {'.'.join(map(str, MIN_PYTHON))}. "
            "Create the project venv with `make venv`, then re-run this as "
            "`.venv/bin/pitwall init`.",
        )
    anaconda = "anaconda" in executable.lower() or "conda" in executable.lower()
    in_venv = ".venv" in executable or sys.prefix != sys.base_prefix
    if anaconda and not in_venv:
        return Step(
            "python", False, f"{version} at {executable}",
            "This is the Anaconda interpreter. Its pydantic-settings is older than "
            "the `NoDecode` import in config.py, so `import pitwall` fails with a "
            "message that names the symbol and not the cause. Use `.venv/bin/python`, "
            "or run `make venv` to create it.",
        )
    return Step("python", True, f"{version} at {executable}")


# ─────────────────────────────── step 2 ──────────────────────────────────


def count_rows(path: Path) -> int:
    """Data rows in a CSV, ignoring the comment block and the header."""
    if not path.exists():
        return 0
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return max(0, len(lines) - 1)


def write_sample_inputs(force: bool = False) -> tuple[list[Path], list[Path]]:
    """`config/companies.csv` and `config/roles.csv`, with commented headers.

    Never overwrites without `--force`: these are the two files the whole tool
    is configured by, and a re-run of setup must not cost someone an afternoon
    of alias tuning.
    """
    created: list[Path] = []
    skipped: list[Path] = []
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in (("companies.csv", SAMPLE_COMPANIES), ("roles.csv", SAMPLE_ROLES)):
        path = directory / name
        if path.exists() and not force:
            skipped.append(path)
            continue
        path.write_text(body, encoding="utf-8")
        created.append(path)
    return created, skipped


# ─────────────────────────────── step 3 ──────────────────────────────────


def write_env(force: bool = False) -> tuple[Path | None, Path | None]:
    """`.env.example` -> `.env`. Returns (created, skipped)."""
    root = project_root()
    example, target = root / ".env.example", root / ".env"
    if not example.exists():
        return None, None
    if target.exists() and not force:
        return None, target
    shutil.copyfile(example, target)
    return target, None


# ─────────────────────────────── step 5 ──────────────────────────────────


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=15, check=False
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def start_searxng(root: Path | None = None) -> tuple[bool, str]:
    """`make searxng`, so the container recipe lives in exactly one place."""
    root = root or project_root()
    try:
        proc = subprocess.run(
            ["make", "searxng"], cwd=str(root), capture_output=True,
            text=True, timeout=600, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output.strip()[-600:]


# ─────────────────────────────── step 6 ──────────────────────────────────


def validate_inputs():
    """The existing `inputs.py` validation, unchanged — §3.3 only surfaces it.

    Both files, merged into one report, and without touching the database:
    `load_and_sync` would write, and validating should not.
    """
    from .inputs import load_companies, load_roles

    _, companies_report = load_companies()
    _, roles_report = load_roles()
    return companies_report.merge(roles_report)


def vocabulary_hint() -> str:
    """The legal `family` and `seniority` values, from the active profile."""
    profile = active_profile()
    return (
        f"profile `{profile.name}`\n"
        f"  families:   {', '.join(profile.role_families)}\n"
        f"  seniority:  {', '.join(profile.seniorities)}"
    )
