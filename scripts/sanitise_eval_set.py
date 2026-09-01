#!/usr/bin/env python
"""Replace the real people in the tracked fixtures with synthetic ones — §3.4.

    .venv/bin/python scripts/sanitise_eval_set.py            # in place, with a backup
    .venv/bin/python scripts/sanitise_eval_set.py --check    # are the tracked files clean?
    .venv/bin/python scripts/sanitise_eval_set.py --serp     # the SERP captures too

`tests/fixtures/eval_set.csv` is tracked, because it is what makes a threshold
change measurable rather than a vibe. It also held 60 real people's names,
headlines and profile URLs, which is the one thing that cannot be in a
repository someone else is going to clone.

So: the tracked fixture gets synthetic identities, and the real labelled set
moves to `data/`, which is git-ignored, where it stays useful to the owner.

**What is replaced, and what is not.** Names, name fragments and profile slugs
are replaced consistently — the same person becomes the same pseudonym
everywhere, so a row that was self-consistent stays self-consistent. Everything
the scoring layer actually reads — company, aliases, negative keywords, role,
family, seniority, keywords, location, evidence count and the hand-applied
label — is untouched. That is what keeps the sanitised set exercising the same
code paths and producing the same sweep.

Pseudonyms are drawn deterministically from a fixed list by hashing the real
name, so re-running this produces the same file rather than a fresh diff.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import typer  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

console = Console()

TRACKED = ROOT / "tests" / "fixtures" / "eval_set.csv"
PRIVATE = ROOT / "data" / "eval_set_real.csv"

FIRST_NAMES = [
    "Alina", "Bruno", "Cato", "Dilara", "Emeka", "Freja", "Goran", "Hana",
    "Idris", "Jonna", "Kavan", "Lior", "Mira", "Nuno", "Oona", "Pavel",
    "Quinn", "Rina", "Sable", "Tomasz", "Ulla", "Vito", "Wren", "Xenia",
    "Yannick", "Zara", "Amara", "Bodhi", "Cassia", "Darius", "Elin", "Fen",
]
LAST_NAMES = [
    "Abbate", "Bergqvist", "Calder", "Devlin", "Eskola", "Ferraro", "Grady",
    "Halvorsen", "Ivanek", "Jansen", "Koval", "Laurent", "Moreno", "Nystrom",
    "Okafor", "Pires", "Quintero", "Rasmussen", "Solberg", "Tavares", "Ubaldi",
    "Varga", "Whitlock", "Ximenes", "Yilmaz", "Zanetti", "Ashworth", "Brandt",
    "Cortez", "Duarte", "Engel", "Fontaine",
]


def is_synthetic(name: str) -> bool:
    """Is this name already drawn from the two lists below?

    What makes `pseudonym` idempotent, and therefore what makes `--check` work:
    a sanitised file is one where sanitising again changes nothing.
    """
    parts = name.replace("-", " ").split()
    if len(parts) < 2:
        return False
    # Case-insensitive: one of these captures spells its subject in lower case
    # ("luigi urbinelli"), and its pseudonym inherits that.
    first = {n.casefold() for n in FIRST_NAMES}
    last = {n.casefold() for n in LAST_NAMES}
    return parts[0].casefold() in first and all(p.casefold() in last for p in parts[1:])


def pseudonym(real: str, forbidden: frozenset[str] = frozenset()) -> str:
    """A stable synthetic name for a real one. Same input, same output.

    Idempotent: a name that is already synthetic is returned unchanged, so the
    script can be re-run and `--check` has a fixed point to test against.

    `forbidden` holds tokens that already appear in the source as REAL names.
    One of the captures has a person surnamed Brandt, and "Brandt" is in the
    pool below — so without this, a different person is handed that surname and
    the real one's name is still sitting in the file, attached to a stranger.
    Salting until the draw avoids the collision keeps the output deterministic.
    """
    real = real.strip()
    if is_synthetic(real):
        return real
    blocked = {f.casefold() for f in forbidden}
    for salt in range(64):
        digest = hashlib.sha256(f"{salt}:{real.casefold()}".encode()).digest()
        first = FIRST_NAMES[digest[0] % len(FIRST_NAMES)]
        last = LAST_NAMES[digest[1] % len(LAST_NAMES)]
        # Multi-barrelled surnames stay multi-barrelled: shape carries signal
        # for the parser, which is one of the paths these fixtures exercise.
        if "-" in real:
            last = f"{last}-{LAST_NAMES[digest[2] % len(LAST_NAMES)]}"
        parts = {p.casefold() for p in f"{first} {last}".replace("-", " ").split()}
        if not parts & blocked:
            return f"{first} {last}"
    return f"{first} {last}"   # pragma: no cover - 64 draws never all collide


#: A LinkedIn profile id: alphanumeric, and it always contains a digit.
#: Without the digit requirement the trailing SURNAME is mistaken for the id and
#: kept — `.../in/ulla-fontaine-ayliff` still names a real person.
_PROFILE_ID = re.compile(r"-([0-9a-z]*[0-9][0-9a-z]*)/?$")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _sub_keeping_case(pattern: str, replacement: str, text: str) -> str:
    """Case-insensitive substitution that puts the matched case back.

    Without this the pass that catches lower-case slug fragments also
    lower-cases the name in the title, which makes the script non-idempotent:
    a second run turns "Hana Ubaldi" into "hana ubaldi".
    """

    def repl(match: re.Match[str]) -> str:
        found = match.group(0)
        if found.islower():
            return replacement.lower()
        if found.isupper():
            return replacement.upper()
        return replacement

    return re.sub(pattern, repl, text, flags=re.I)


def _replace_all(text: str, real: str, fake: str) -> str:
    """Swap a name and its fragments, case-insensitively and slug-wise."""
    if not text:
        return text
    real_parts = [p for p in re.split(r"[^A-Za-z0-9'-]+", real) if len(p) > 2]
    fake_parts = fake.split()

    out = _sub_keeping_case(re.escape(real), fake, text)
    out = _sub_keeping_case(re.escape(slugify(real)), slugify(fake), out)
    for i, part in enumerate(real_parts):
        replacement = fake_parts[min(i, len(fake_parts) - 1)]
        out = _sub_keeping_case(rf"\b{re.escape(part)}\b", replacement, out)
    return out


def sanitise_row(
    row: dict[str, str],
    forbidden: frozenset[str] = frozenset(),
    names: dict[str, str] | None = None,
) -> dict[str, str]:
    """One row, with every identity in the corpus replaced — not just its own.

    A snippet routinely names somebody else: "View Paul Laval's profile on
    LinkedIn". Replacing only the row's own subject leaves those third parties
    in a tracked file, which is the same problem one level down. `names` is the
    whole corpus mapping, applied to every free-text column.
    """
    real = (row.get("full_name") or "").strip()
    out = dict(row)
    mapping = dict(names or {})
    if real and not is_synthetic(real):
        mapping.setdefault(real, pseudonym(real, forbidden))

    if real:
        out["full_name"] = mapping.get(real, real)
    for column in ("serp_title", "serp_snippet"):
        if row.get(column):
            out[column] = _swap_many(row[column], mapping)

    url = row.get("profile_url") or ""
    if url and out.get("full_name"):
        # Derived from the new name, never patched — see _rebuild_profile_urls.
        tail = re.search(_PROFILE_ID, url)
        suffix = f"-{tail.group(1)}" if tail else ""
        host = "uk.linkedin.com" if "uk.linkedin.com" in url else "www.linkedin.com"
        out["profile_url"] = f"https://{host}/in/{slugify(out['full_name'])}{suffix}"
    return out


#: "View Paul Laval's profile on LinkedIn" — the shape LinkedIn snippets use
#: to name a third party.
_SNIPPET_NAME = re.compile(
    r"View\s+([A-Z\u00c0-\u024f][\w\u00c0-\u024f'-]+(?:\s+[A-Z\u00c0-\u024f][\w\u00c0-\u024f'-]+){1,2})"
    r"(?:\u2019s|'s)?\s+(?:profile|full)"
)


def _names_in_snippets(rows: list[dict[str, str]]) -> list[str]:
    """Third parties named inside somebody else's snippet."""
    found: list[str] = []
    for row in rows:
        for match in _SNIPPET_NAME.finditer(row.get("serp_snippet") or ""):
            name = match.group(1).strip()
            if not is_synthetic(name) and name not in found:
                found.append(name)
    return found


def looks_sanitised(rows: list[dict[str, str]]) -> bool:
    """True when sanitising again would change nothing — the fixed-point test."""
    return all(
        not row.get("full_name") or sanitise_row(row)["full_name"] == row["full_name"]
        for row in rows
    )


def main(
    source: Path = typer.Option(TRACKED, help="the eval set to read"),
    out: Path = typer.Option(TRACKED, help="where to write the sanitised set"),
    keep_real: Path = typer.Option(PRIVATE, help="where to keep the real one"),
    check: bool = typer.Option(False, "--check", help="verify and change nothing"),
    serp: bool = typer.Option(True, "--serp/--no-serp",
                              help="also sanitise the captured SERP fixtures"),
) -> None:
    if not source.exists():
        console.print(f"[red]{source} not found[/]")
        raise typer.Exit(1)

    rows = list(csv.DictReader(source.open(encoding="utf-8-sig")))
    if not rows:
        console.print(f"[red]{source} is empty[/]")
        raise typer.Exit(1)
    fields = list(rows[0])

    if check:
        offenders = [
            r["full_name"] for r in rows
            if r.get("full_name") and not is_synthetic(r["full_name"])
        ]
        if not offenders:
            console.print(f"[green]{source.name} carries no real names[/] ({len(rows)} rows)")
            return
        console.print(
            f"[red]{source.name} holds {len(offenders)} real identity/identities[/] — "
            "run this script without --check before sharing the repository."
        )
        raise typer.Exit(1)

    if keep_real and not keep_real.exists():
        keep_real.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, keep_real)
        console.print(f"[green]kept the real labelled set[/] at {keep_real} (git-ignored)")

    # Build the whole corpus mapping first: a snippet can name someone whose
    # own row is elsewhere in the file, or who has no row at all.
    real_names = [
        (r.get("full_name") or "").strip() for r in rows
        if (r.get("full_name") or "").strip() and not is_synthetic(r["full_name"])
    ]
    real_names += _names_in_snippets(rows)
    forbidden = frozenset(
        part for name in real_names for part in name.replace("-", " ").split()
    )
    names = {name: pseudonym(name, forbidden) for name in dict.fromkeys(real_names)}
    sanitised = [sanitise_row(r, forbidden, names) for r in rows]
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sanitised)

    table = Table(title=f"{len(sanitised)} rows sanitised")
    table.add_column("was")
    table.add_column("is now")
    for before, after in list(zip(rows, sanitised, strict=True))[:5]:
        table.add_row(before.get("full_name", ""), after.get("full_name", ""))
    console.print(table)
    console.print(
        f"[green]wrote {out}[/]\n"
        "Labels, companies, roles, keywords and locations are untouched — only "
        "identities changed, so the scoring paths are the same ones."
    )

    if serp:
        for name, changed in sanitise_serp_files(out.parent):
            console.print(f"[green]sanitised[/] {name} ({changed} line(s) rewritten)")




# ───────────────── the SERP captures carry people too ────────────────────
#
# `serp_ddg_mclaren.json` and `serp_serper_ferrari.json` are verbatim captures
# from a real run, which is exactly what makes them good fixtures — and exactly
# why they hold 27 real names, headlines and profile URLs. Same treatment: the
# identities become synthetic, everything the PARSER reads about structure (the
# title shape, the " - " separators, the "| LinkedIn" suffix, the URL host, the
# trailing profile id, the non-profile rows) is preserved, because that
# structure is the entire point of the fixture.

SERP_FIXTURES = ("serp_ddg_mclaren.json", "serp_serper_ferrari.json")

_PERSON_TITLE = re.compile(r"^\s*([^\-|·]{2,60}?)\s+[-–—]\s+")


def _name_from_title(title: str) -> str | None:
    """The leading "Name - " of a LinkedIn SERP title, if there is one.

    Case is not required: one real row in these captures is
    "luigi urbinelli - T.D. and race engineer - AF Corse", and a
    capitalisation rule silently left that person in the fixture.
    """
    match = _PERSON_TITLE.match(title or "")
    if not match:
        return None
    name = match.group(1).strip()
    words = name.split()
    if not 1 < len(words) <= 4:
        return None
    if not all(w[:1].isalpha() or w[:1] in "'\u2019" for w in words):
        return None
    return name


def _walk_strings(node, transform):
    if isinstance(node, str):
        return transform(node)
    if isinstance(node, list):
        return [_walk_strings(x, transform) for x in node]
    if isinstance(node, dict):
        return {k: _walk_strings(v, transform) for k, v in node.items()}
    return node


def sanitise_serp_text(text: str) -> str:
    """Swap every person out of one captured SERP document.

    Operates on the DECODED JSON, so a name written as `\u00fc` in the file is
    matched the same as one written literally, and the nested `raw` copy of a
    title is rewritten identically to the top-level one.

    Names come from titles ONLY — the "Name - …" prefix of a LinkedIn SERP title
    is a strong signal, and guessing them from URL slugs instead turned the
    company "Af Corse" into "Af Laurent" on the first attempt. Corrupting a
    company name would gut the fixture: company matching is most of what these
    files exist to test.
    """
    data = json.loads(text)

    real_names: list[str] = []

    def collect(value: str) -> str:
        name = _name_from_title(value)
        if name and not is_synthetic(name) and name not in real_names:
            real_names.append(name)
        return value

    _walk_strings(data, collect)

    # Every real token in the document is off-limits as a pseudonym, so no
    # stranger inherits a real person's surname.
    forbidden = frozenset(
        part for name in real_names for part in name.replace("-", " ").split()
    )
    names = {name: pseudonym(name, forbidden) for name in real_names}

    def swap(value: str) -> str:
        return _swap_many(value, names)

    # URLs first, from the ORIGINAL titles: the trailing profile id has to be
    # read off the original slug, not off a half-replaced one.
    _rebuild_profile_urls(data)
    return json.dumps(_walk_strings(data, swap), indent=2, ensure_ascii=False) + "\n"


_PROFILE_URL = re.compile(r"^(https?://[a-z]{0,3}\.?linkedin\.com/in/)([^/?#]+)(/?.*)$", re.I)


def _rebuild_profile_urls(node, title: str | None = None) -> None:
    """Regenerate every `linkedin.com/in/<slug>` from its (already replaced) title.

    Derived, not patched. Slug patching kept leaking — a run-together slug
    (`timheinemann`), an accented one (`pascal-kühner`), a vanity tail
    (`racewithcharlotte`) — and each miss puts a real person back in a tracked
    file. Deriving the slug from the title cannot miss, and the host and the
    trailing profile id are preserved so the URL parser is still tested on the
    same shapes.
    """
    if isinstance(node, list):
        for item in node:
            _rebuild_profile_urls(item, title)
        return
    if not isinstance(node, dict):
        return

    here = node.get("title") or node.get("text") or title
    name = _name_from_title(here or "")
    for key in ("url", "href", "link"):
        value = node.get(key)
        if not isinstance(value, str):
            continue
        match = _PROFILE_URL.match(value)
        if not match or not name:
            continue
        prefix, slug, suffix = match.groups()
        # A LinkedIn profile id always contains a digit; requiring one stops
        # the already-replaced surname being mistaken for the id and appended
        # twice ("jonna-ubaldi-ubaldi97").
        tail = re.search(r"-([0-9a-z]*[0-9][0-9a-z]*)$", slug)
        node[key] = prefix + slugify(name) + (f"-{tail.group(1)}" if tail else "") + suffix
    for value in node.values():
        if isinstance(value, list | dict):
            _rebuild_profile_urls(value, here)


#: A sentinel that cannot occur in JSON string data, so a placeholder written
#: by one rule is invisible to the next.
_MARK = "\x00"


def _swap_many(text: str, names: dict[str, str]) -> str:
    """Replace every real name at once, without rules colliding.

    One-at-a-time substitution lets a later rule rewrite an earlier rule's
    OUTPUT: replacing "Tom" -> "Oona" after "Tomasz Eskola" had already been
    written produced "Oonaasz Eskola". So this is two-phase — every match
    becomes an inert placeholder first, and placeholders are expanded only when
    every rule has run.
    """
    if not text or not names:
        return text

    slots: list[str] = []

    def reserve(replacement: str) -> str:
        slots.append(replacement)
        return f"{_MARK}{len(slots) - 1}{_MARK}"

    def sub(pattern: str, replacement: str, value: str) -> str:
        def repl(match: re.Match[str]) -> str:
            found = match.group(0)
            if found.islower():
                return reserve(replacement.lower())
            if found.isupper():
                return reserve(replacement.upper())
            return reserve(replacement)

        return re.sub(pattern, repl, value, flags=re.I)

    out = text
    for real, fake in sorted(names.items(), key=lambda kv: -len(kv[0])):
        out = sub(re.escape(real), fake, out)
        out = sub(re.escape(slugify(real)), slugify(fake), out)
        # Slug shapes LinkedIn actually uses that `slugify` does not produce:
        # accented ("pascal-kühner"), run-together ("timheinemann"), dotted.
        for variant in _slug_variants(real):
            out = sub(re.escape(variant), slugify(fake), out)
        parts = _name_parts(real)
        for part, replacement in zip(parts, _padded(fake.split(), parts), strict=True):
            # >= 3, not > 3: "Max", "Tim" and "Tom" are real first names in
            # these captures, and a four-character floor left all three behind.
            if len(part) >= 3:
                out = sub(re.escape(part), replacement, out)

    return re.sub(
        rf"{_MARK}(\d+){_MARK}", lambda m: slots[int(m.group(1))], out
    )


def _name_parts(name: str) -> list[str]:
    return [p for p in re.split(r"[^A-Za-z0-9'\u00c0-\u024f-]+", name) if len(p) > 2]


def _padded(replacements: list[str], parts: list[str]) -> list[str]:
    return [replacements[min(i, len(replacements) - 1)] for i in range(len(parts))]


def _slug_variants(name: str) -> list[str]:
    low = name.lower()
    return [
        low.replace(" ", "-").replace(".", ""),
        low.replace(" ", "").replace(".", ""),
        low.replace(" ", "."),
    ]


def sanitise_serp_files(directory: Path, check: bool = False) -> list[tuple[str, int]]:
    """Returns [(filename, people_replaced)]."""
    results = []
    for name in SERP_FIXTURES:
        path = directory / name
        if not path.exists():
            continue
        before = path.read_text(encoding="utf-8")
        after = sanitise_serp_text(before)
        changed = sum(
            1 for a, b in zip(before.splitlines(), after.splitlines(), strict=False) if a != b
        )
        if not check and after != before:
            path.write_text(after, encoding="utf-8")
        results.append((name, changed))
    return results


if __name__ == "__main__":
    typer.run(main)
