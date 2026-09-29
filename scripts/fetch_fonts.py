#!/usr/bin/env python
"""Vendor the three CV typefaces into `assets/fonts/`.

    python scripts/fetch_fonts.py          # fetch what is missing
    python scripts/fetch_fonts.py --force  # re-fetch everything

WHY THIS IS NOT A ONE-LINER. Google Fonts' CSS API serves a format ReportLab
rejects outright ("Not a recognized TrueType font"), and the upstream repository
ships DM Sans and Arimo as VARIABLE fonts, which ReportLab renders at a single
default instance — so asking for Bold would silently give you Regular. Both
problems are solved the same way: pull the variable font from the source
repository and instantiate the static weights locally with fontTools.

Machina Ultrabold, the face in the name and title of the original Canva design,
is commercial and not licensed for generated exports. `profile/layout.yaml`
records Archivo Black as the substitute, chosen once at the template level. All
three families here are SIL OFL, and their licences are vendored alongside them.
"""

from __future__ import annotations

import argparse
import io
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "fonts"
RAW = "https://raw.githubusercontent.com/google/fonts/main/"

#: output name -> (upstream path, axis pins). Empty pins means already static.
#: DM Sans varies on optical size as well as weight; 14 is its text instance.
FONTS: dict[str, tuple[str, dict[str, float]]] = {
    "DMSans-400.ttf":       ("ofl/dmsans/DMSans%5Bopsz,wght%5D.ttf",        {"wght": 400, "opsz": 14}),
    "DMSans-500.ttf":       ("ofl/dmsans/DMSans%5Bopsz,wght%5D.ttf",        {"wght": 500, "opsz": 14}),
    "DMSans-700.ttf":       ("ofl/dmsans/DMSans%5Bopsz,wght%5D.ttf",        {"wght": 700, "opsz": 14}),
    "DMSans-400i.ttf":      ("ofl/dmsans/DMSans-Italic%5Bopsz,wght%5D.ttf", {"wght": 400, "opsz": 14}),
    "DMSans-700i.ttf":      ("ofl/dmsans/DMSans-Italic%5Bopsz,wght%5D.ttf", {"wght": 700, "opsz": 14}),
    "Arimo-400.ttf":        ("ofl/arimo/Arimo%5Bwght%5D.ttf",               {"wght": 400}),
    "Arimo-700.ttf":        ("ofl/arimo/Arimo%5Bwght%5D.ttf",               {"wght": 700}),
    "ArchivoBlack-400.ttf": ("ofl/archivoblack/ArchivoBlack-Regular.ttf",   {}),
}

LICENCES = {
    "OFL-DMSans.txt": "ofl/dmsans/OFL.txt",
    "OFL-Arimo.txt": "ofl/arimo/OFL.txt",
    "OFL-ArchivoBlack.txt": "ofl/archivoblack/OFL.txt",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-fetch fonts already present")
    args = parser.parse_args()

    try:
        import httpx
        from fontTools.ttLib import TTFont
        from fontTools.varLib import instancer
    except ImportError as exc:
        print(f"needs httpx and fonttools: pip install httpx fonttools  ({exc})", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    cache: dict[str, bytes] = {}

    def fetch(path: str) -> bytes:
        if path not in cache:
            resp = httpx.get(RAW + path, timeout=60, follow_redirects=True)
            resp.raise_for_status()
            cache[path] = resp.content
        return cache[path]

    written = skipped = 0
    for name, (path, pins) in FONTS.items():
        target = OUT / name
        if target.exists() and not args.force:
            skipped += 1
            continue
        try:
            source = fetch(path)
            if pins:
                font = instancer.instantiateVariableFont(
                    TTFont(io.BytesIO(source)), pins, inplace=False, updateFontNames=True
                )
                font.save(target)
            else:
                target.write_bytes(source)
        except Exception as exc:  # noqa: BLE001 — one bad font must not stop the rest
            print(f"  FAILED {name}: {exc}", file=sys.stderr)
            continue
        print(f"  {name:<24} {target.stat().st_size:>7} bytes")
        written += 1

    for name, path in LICENCES.items():
        if not (OUT / name).exists() or args.force:
            try:
                (OUT / name).write_bytes(fetch(path))
            except Exception as exc:  # noqa: BLE001
                print(f"  FAILED {name}: {exc}", file=sys.stderr)

    print(f"\n  {written} written, {skipped} already present -> {OUT.relative_to(ROOT)}")

    # Prove they load, rather than trusting that they downloaded.
    try:
        from reportlab.pdfbase.ttfonts import TTFont as RLFont
        bad = []
        for name in FONTS:
            try:
                RLFont("probe", str(OUT / name))
            except Exception as exc:  # noqa: BLE001
                bad.append(f"{name}: {exc}")
        if bad:
            print("\n  ReportLab rejected:", file=sys.stderr)
            for line in bad:
                print(f"    {line}", file=sys.stderr)
            return 1
        print("  all eight load in ReportLab")
    except ImportError:
        print("  (reportlab not installed — skipped the load check)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
