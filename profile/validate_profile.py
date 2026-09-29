#!/usr/bin/env python3
"""validate_profile.py — frame-compliance and data-integrity checks for profile/.

    python3 profile/validate_profile.py            # report
    python3 profile/validate_profile.py --strict   # TODO_SIMON markers become errors

The CV frame has no slack: one character too many wraps a bullet onto a new line
and pushes the page to two. This is the thing that tells you before an employer does.

THE CHECKS NOW LIVE IN `src/pitwall/apply/validate.py`, so this script, the CLI
(`pitwall apply validate-profile`) and CI all run one implementation. Behaviour is
unchanged. For the stricter question — what the bullets measure when a real font
renders them — run `pitwall apply calibrate`.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

try:
    import yaml  # noqa: F401
except ImportError:
    sys.exit("pyyaml required:  pip install pyyaml")

from pitwall.apply.validate import format_report, validate  # noqa: E402

strict = "--strict" in sys.argv
report = validate(pathlib.Path(__file__).parent)
print(format_report(report, strict=strict))
sys.exit(1 if report.failed(strict=strict) else 0)
