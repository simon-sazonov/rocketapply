"""The standing prohibitions of §0.3 and §9, asserted against the whole tree.

These are not unit tests of a function. They are the guard rails that keep a
later change — a helpful refactor, a "just for debugging" branch — from quietly
crossing a line the owner drew. §3.1 explains at length why SMTP probing is not
being built; this file is what makes that decision survive the explanation.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Everything that ships. `tests/` is excluded deliberately — this very file
#: has to name the forbidden things in order to forbid them.
SOURCE_DIRS = ("src", "app", "scripts")


def source_files() -> list[Path]:
    out: list[Path] = []
    for directory in SOURCE_DIRS:
        out.extend(sorted((ROOT / directory).rglob("*.py")))
    return out


def test_there_are_source_files_to_check():
    """A guard rail that silently checks nothing is worse than none at all."""
    assert len(source_files()) > 20


# ───────────────────── §8: the suite makes no requests ───────────────────


def test_the_network_guard_actually_fires():
    """The autouse guard in conftest is the enforcement behind "no test makes a
    real network call". A guard that has quietly stopped working looks exactly
    like a suite that never needed it."""
    import socket

    from tests.conftest import NetworkUsedInTest

    sock = socket.socket()
    try:
        with pytest.raises(NetworkUsedInTest):
            sock.connect(("api.hunter.io", 443))
    finally:
        sock.close()


def test_an_unstubbed_hunter_call_cannot_reach_the_internet():
    """The adapters take injectable transports so this holds; if one stopped,
    the test that forgot to stub it would fail here rather than spend a credit
    on somebody's real account."""
    from pitwall.config import Settings
    from pitwall.enrich.hunter import HunterClient, HunterError
    from tests.conftest import NetworkUsedInTest

    client = HunterClient(Settings(hunter_api_key="x" * 20, _env_file=None))
    with pytest.raises((HunterError, NetworkUsedInTest)):
        client.account()


# ───────────────────────────── no SMTP, ever ─────────────────────────────

#: §3.1. Any one of the four reasons there is disqualifying; together they make
#: SMTP probing an engineering mistake as well as a policy breach.
SMTP_IMPORTS = {"smtplib", "aiosmtplib", "smtpd", "aiosmtpd", "email.smtp"}


@pytest.mark.parametrize("path", source_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_module_imports_an_smtp_library(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {
        name for name in imported
        if name in SMTP_IMPORTS or name.split(".")[0] in SMTP_IMPORTS
    }
    assert not forbidden, (
        f"{path.relative_to(ROOT)} imports {forbidden}. §3.1 of "
        "PITWALL_V3_EMAIL_ENRICHMENT.md: independent SMTP verification is not "
        "built, for four independent engineering reasons."
    )


#: `RCPT TO` and `MAIL FROM` are the two commands a verifier sends, and port 25
#: is the only way to send them. Any of the three appearing in a code line is
#: the thing §0.3 prohibits.
SMTP_PATTERNS = (
    re.compile(r"\bRCPT\s+TO\b", re.I),
    re.compile(r"\bMAIL\s+FROM\b", re.I),
    re.compile(r"\bSMTP\s*\(", re.I),
    re.compile(r"\bEHLO\b|\bHELO\b", re.I),
    re.compile(r"port\s*=\s*25\b"),
    re.compile(r"\b(?:connect|socket)\([^)]*\b25\b"),
)


def code_lines(path: Path) -> list[tuple[int, str]]:
    """Every line that is not inside a docstring or a comment.

    A prohibition has to be *explained* somewhere, and the explanations live in
    docstrings — `checks.py` names `RCPT TO` three times while setting out why
    it does not send one. Scanning raw text would make the explanation itself
    the violation.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    doc_ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            doc_ranges.append((node.lineno, node.end_lineno or node.lineno))
    out: list[tuple[int, str]] = []
    for i, line in enumerate(source.splitlines(), start=1):
        if any(lo <= i <= hi for lo, hi in doc_ranges):
            continue
        code = line.split("#", 1)[0]
        if code.strip():
            out.append((i, code))
    return out


@pytest.mark.parametrize("path", source_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_module_speaks_smtp(path):
    offenders = [
        f"{path.relative_to(ROOT)}:{n}: {line.strip()}"
        for n, line in code_lines(path)
        for pattern in SMTP_PATTERNS
        if pattern.search(line)
    ]
    assert not offenders, (
        "SMTP probing is prohibited (§0.3, and §3.1 for why):\n" + "\n".join(offenders)
    )


# ──────────────────────────── no phone numbers ───────────────────────────


def test_the_schema_has_no_phone_column():
    """§0.3 on phone numbers: do not request, store, or display them."""
    from pitwall.db import DDL

    lowered = DDL.lower()
    for forbidden in ("phone", "telephone", "mobile_number", "msisdn"):
        assert forbidden not in lowered, f"the schema mentions {forbidden!r}"


def test_the_hunter_adapter_never_asks_for_a_phone_number():
    """Hunter returns `phone_number` in its payloads. Storing one would be a
    breach even though the API offers it for free."""
    hunter = ROOT / "src" / "pitwall" / "enrich" / "hunter.py"
    if not hunter.exists():
        pytest.skip("the Hunter adapter has not been written yet")
    offenders = [
        f"{n}: {line.strip()}"
        for n, line in code_lines(hunter)
        if re.search(r"phone", line, re.I)
    ]
    assert not offenders, "the Hunter adapter touches a phone number:\n" + "\n".join(offenders)


# ─────────────────────── no domain-wide crawling ─────────────────────────


def test_enrichment_does_no_recursive_crawling():
    """§0.3 draws the line at "reading a page the search index already pointed
    at" versus "walking a domain harvesting mailto: links". A link-following
    loop in this package would be the second."""
    enrich = ROOT / "src" / "pitwall" / "enrich"
    if not enrich.exists():
        pytest.skip("the enrich package has not been written yet")
    for path in sorted(enrich.rglob("*.py")):
        text = " ".join(line for _, line in code_lines(path))
        for forbidden in ("BeautifulSoup", "scrapy", "urljoin(", "follow_links", "crawl("):
            assert forbidden not in text, (
                f"{path.name} uses {forbidden!r} — §0.3 prohibits crawling a domain "
                "for addresses."
            )
