"""Syntax, MX, disposable and role-address checks — §3.2 of the v3 brief.

**There is no SMTP code in this file, or anywhere else in this tree, and that is
a finding rather than a squeamishness.** §3.1 sets out four independent reasons,
any one of which is disqualifying:

1. Residential ISPs block outbound port 25, so from the owner's MacBook the
   connection very likely never opens. Docker does not change that.
2. Production verifiers rotate IPs; mail servers answer repeated `RCPT TO` from
   one address with deliberately ambiguous codes (252, 421). A single laptop IP
   is exactly the pattern they throttle.
3. The big hosts are unreliable or hostile. Microsoft 365 returns 250 for
   invalid addresses, Yahoo/AOL return 250 or 252 regardless, iCloud throttles
   within a few hundred. Most corporate mail sits behind Google or Microsoft.
4. Catch-all domains 250 everything, so the answer is "unknown" no matter how
   clean the probe.

What is here instead is cheap, reliable and mostly local. The MX lookup is the
one that earns its place: DNS runs on port 53 and is never blocked, so it works
everywhere, and the MX hostnames tell you *who hosts the mail* — which is
context worth showing, and a signal for how much to trust a Hunter verification
on that domain.

The corroboration search in `corroborate.py` is the real verifier. This module
only ever rules things *out*.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# ──────────────────────────────── syntax ─────────────────────────────────

#: Deliberately not RFC 5321 in full. The full grammar admits quoted local
#: parts and bracketed address literals that no company mailbox uses, and every
#: string this package handles was either derived from a person's name or read
#: off a web page. A tight pattern rejects the junk a SERP snippet produces —
#: `name@2x.png`, `foo@…`, a trailing full stop — which is the actual job.
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}$"
)

#: For pulling addresses out of prose — SERP titles and snippets (§2.2).
#: Anchored on a word boundary and stripped of trailing punctuation by
#: `find_addresses`, because "write to jane@alpine.com." is the normal shape.
EMAIL_IN_TEXT_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
)

#: File extensions that make an "address" an image or asset reference. SERP
#: snippets are full of `logo@2x.png` and `sprite@3x.svg`.
_ASSET_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js",
    ".woff", ".woff2", ".ttf", ".pdf", ".zip", ".mp4", ".json", ".xml",
)


def is_valid_syntax(email: str) -> bool:
    """RFC-shaped validation — "the string is a possible address"."""
    email = (email or "").strip()
    if not email or len(email) > 254:
        return False
    if email.lower().endswith(_ASSET_SUFFIXES):
        return False
    local = email.rsplit("@", 1)[0]
    if len(local) > 64:
        return False
    return bool(EMAIL_RE.match(email))


def normalize(email: str) -> str:
    """The stored form: trimmed, lowercased, angle brackets and mailto: gone."""
    text = (email or "").strip()
    if text.lower().startswith("mailto:"):
        text = text[7:]
    return text.strip("<>").strip().rstrip(".,;:").lower()


def local_part(email: str) -> str:
    return normalize(email).rsplit("@", 1)[0]


def domain_of(email: str) -> str:
    parts = normalize(email).rsplit("@", 1)
    return parts[1] if len(parts) == 2 else ""


def find_addresses(text: str, *, domain: str | None = None) -> list[str]:
    """Every address in a blob of prose, deduped, in the order they appear.

    `domain` restricts the harvest to one company, which is what §2.2 actually
    wants: a SERP page for `site:alpine.com "@alpine.com"` is full of other
    people's addresses from adverts and footers, and storing those against
    Alpine's domain would poison the pattern corpus.
    """
    out: list[str] = []
    seen: set[str] = set()
    wanted = (domain or "").strip().lower().lstrip("@")
    for match in EMAIL_IN_TEXT_RE.finditer(text or ""):
        email = normalize(match.group(0))
        if not is_valid_syntax(email) or email in seen:
            continue
        if wanted and domain_of(email) != wanted:
            continue
        seen.add(email)
        out.append(email)
    return out


# ──────────────────────────── role addresses ─────────────────────────────

#: Shared mailboxes. §3.2: "it is a shared mailbox, not a person" — and §8
#: requires these to be *flagged*, never returned as somebody's address. A press
#: office address filed under a named engineer is worse than no address, because
#: it looks like a result.
ROLE_LOCAL_PARTS: frozenset[str] = frozenset(
    {
        "info", "information", "contact", "contacts", "enquiries", "enquiry",
        "inquiries", "inquiry", "hello", "hi", "mail", "email", "general",
        "office", "reception", "admin", "administration", "team", "all",
        "press", "media", "pr", "comms", "communications", "newsroom",
        "marketing", "newsletter", "news", "social",
        "hr", "jobs", "job", "careers", "career", "recruitment", "recruiting",
        "recruit", "apply", "applications", "talent", "people", "cv",
        "sales", "support", "help", "helpdesk", "service", "customerservice",
        "care", "billing", "accounts", "accounting", "finance", "invoices",
        "invoice", "payments", "purchasing", "procurement", "orders",
        "legal", "privacy", "dpo", "gdpr", "compliance", "security", "abuse",
        "postmaster", "webmaster", "hostmaster", "noc", "root", "sysadmin", "it",
        "noreply", "no-reply", "donotreply", "do-not-reply", "bounce", "mailer",
        "notifications", "alerts", "automated", "system",
        "partners", "partnerships", "sponsorship", "sponsorships", "commercial",
        "business", "bd", "investors", "ir", "events", "bookings", "shop",
        "store", "merchandise", "tickets", "hospitality", "membership",
        "webshop", "feedback", "suggestions", "training", "education",
    }
)

#: Words that make a local part a role even with something appended:
#: `press.office@`, `jobs-uk@`, `hr_team@`.
#:
#: A deliberately *smaller* set than `ROLE_LOCAL_PARTS`, and the difference
#: matters. Prefix matching is aggressive, and a false positive here is
#: expensive: flagging `marketing.director@alpine.com` as a shared mailbox
#: throws away a real person's address. So this list holds only words that are
#: never part of a job title — `information.systems.lead@` is a person,
#: `information@` is a front desk, and only the exact-match set catches the
#: second without also catching the first.
ROLE_PREFIXES: frozenset[str] = frozenset(
    {
        "info", "press", "hr", "jobs", "careers", "recruitment", "recruiting",
        "contact", "enquiries", "enquiry", "inquiries", "inquiry",
        "noreply", "no-reply", "donotreply", "do-not-reply", "mailer",
        "postmaster", "webmaster", "hostmaster", "abuse", "admin",
        "helpdesk", "support", "sales", "billing", "accounts", "invoices",
        "privacy", "dpo", "gdpr", "notifications", "alerts", "bounce",
    }
)

_ROLE_PREFIX_SEPARATORS = (".", "-", "_", "+")


def is_role_address(email: str) -> bool:
    """Is this a shared mailbox rather than a person?"""
    local = local_part(email)
    if not local:
        return False
    if local in ROLE_LOCAL_PARTS:
        return True
    for sep in _ROLE_PREFIX_SEPARATORS:
        head = local.split(sep, 1)[0]
        if head and head in ROLE_PREFIXES:
            return True
    return False


# ───────────────────── disposable and free providers ─────────────────────

#: Throwaway inbox providers. A static list, per §3.2 — a live lookup would be
#: another network dependency for a check that changes about twice a year.
DISPOSABLE_DOMAINS: frozenset[str] = frozenset(
    {
        "mailinator.com", "guerrillamail.com", "guerrillamail.net",
        "sharklasers.com", "grr.la", "10minutemail.com", "10minutemail.net",
        "tempmail.com", "temp-mail.org", "tempmailo.com", "throwawaymail.com",
        "yopmail.com", "yopmail.fr", "trashmail.com", "trashmail.de",
        "getnada.com", "nada.email", "dispostable.com", "maildrop.cc",
        "fakeinbox.com", "mailnesia.com", "mintemail.com", "spamgourmet.com",
        "mytemp.email", "emailondeck.com", "moakt.com", "tempr.email",
        "discard.email", "mailcatch.com", "inboxkitten.com", "burnermail.io",
        "spam4.me", "harakirimail.com", "anonaddy.me", "mozmail.com",
    }
)

#: Personal mailboxes. Not invalid — plenty of real people use one for work —
#: but a `first.last@gmail.com` derived from a company pattern is nonsense, so
#: the pattern step refuses to apply a company format to a free provider.
FREE_PROVIDER_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.fr",
        "yahoo.it", "yahoo.de", "ymail.com", "rocketmail.com",
        "hotmail.com", "hotmail.co.uk", "hotmail.fr", "hotmail.it",
        "outlook.com", "outlook.fr", "outlook.it", "live.com", "live.co.uk",
        "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "aol.co.uk",
        "gmx.com", "gmx.de", "gmx.net", "web.de", "mail.com", "mail.ru",
        "protonmail.com", "protonmail.ch", "proton.me", "pm.me",
        "zoho.com", "yandex.com", "yandex.ru", "fastmail.com", "hey.com",
        "tutanota.com", "tuta.io", "libero.it", "virgilio.it", "alice.it",
        "orange.fr", "wanadoo.fr", "free.fr", "laposte.net", "sfr.fr",
        "btinternet.com", "sky.com", "virginmedia.com", "talktalk.net",
        "bluewin.ch", "t-online.de", "seznam.cz", "wp.pl", "o2.pl",
    }
)


def is_disposable(email_or_domain: str) -> bool:
    domain = _as_domain(email_or_domain)
    return domain in DISPOSABLE_DOMAINS


def is_free_provider(email_or_domain: str) -> bool:
    domain = _as_domain(email_or_domain)
    return domain in FREE_PROVIDER_DOMAINS


def _as_domain(value: str) -> str:
    value = (value or "").strip().lower().lstrip("@")
    return value.rsplit("@", 1)[-1] if "@" in value else value


# ─────────────────────────────── MX lookup ───────────────────────────────

#: MX hostname fragment -> the mail host it identifies, and how much a Hunter
#: verification on that host is worth. §3.2 wants the host displayed; §3.1's
#: reasoning explains why the reliability note matters. Order is significant —
#: the first fragment found wins, so the specific entries come before generic.
MAIL_HOSTS: tuple[tuple[str, str, str], ...] = (
    ("aspmx.l.google.com", "Google Workspace", "reliable"),
    ("googlemail.com", "Google Workspace", "reliable"),
    ("google.com", "Google Workspace", "reliable"),
    ("outlook.com", "Microsoft 365", "unreliable"),
    ("protection.outlook.com", "Microsoft 365", "unreliable"),
    ("mail.protection.outlook.com", "Microsoft 365", "unreliable"),
    ("pphosted.com", "Proofpoint", "unreliable"),
    ("ppe-hosted.com", "Proofpoint", "unreliable"),
    ("mimecast.com", "Mimecast", "unreliable"),
    ("messagelabs.com", "Symantec/MessageLabs", "unreliable"),
    ("barracudanetworks.com", "Barracuda", "unreliable"),
    ("iphmx.com", "Cisco IronPort", "unreliable"),
    ("zoho.com", "Zoho", "reliable"),
    ("zoho.eu", "Zoho", "reliable"),
    ("yandex.net", "Yandex", "reliable"),
    ("mailgun.org", "Mailgun", "reliable"),
    ("fastmail.com", "Fastmail", "reliable"),
    ("ovh.net", "OVH", "reliable"),
    ("ionos.co.uk", "IONOS", "reliable"),
    ("ionos.com", "IONOS", "reliable"),
    ("one.com", "one.com", "reliable"),
    ("secureserver.net", "GoDaddy", "reliable"),
    ("registrar-servers.com", "Namecheap", "reliable"),
    ("hostinger.com", "Hostinger", "reliable"),
    ("emailsrvr.com", "Rackspace", "reliable"),
    ("icloud.com", "iCloud", "unreliable"),
)

#: How long an MX answer is trusted, in seconds. DNS is cheap, but a Streamlit
#: page enriching eight people re-reads the same domain eight times.
MX_CACHE_TTL = 900.0

_mx_cache: dict[str, tuple[float, MXResult]] = {}
_mx_lock = threading.Lock()


@dataclass(frozen=True)
class MXResult:
    """What DNS said about a domain, and what that means for the owner."""

    domain: str
    valid: bool
    hosts: list[str] = field(default_factory=list)
    #: "Google Workspace", "Microsoft 365", … or None when unrecognised.
    provider: str | None = None
    #: Whether a *third-party* verification of an address here is worth much.
    #: Not a claim about the address — a note about the host. §3.1(3).
    verification_reliability: str | None = None
    error: str | None = None

    @property
    def conclusive(self) -> bool:
        """Did the lookup actually answer, either way?

        The difference matters more than it looks. "This domain publishes no MX
        records" is a verdict, and a strong hint that the resolved domain is
        wrong. "The resolver timed out" is not a verdict about anything, and
        treating the two the same would throw away a good finding because the
        wifi dropped.
        """
        return self.valid or self.error == "no MX records"

    @property
    def label(self) -> str:
        if not self.valid:
            return self.error or "the domain does not accept mail"
        if self.provider:
            return f"mail hosted by {self.provider}"
        return f"mail accepted ({self.hosts[0]})" if self.hosts else "mail accepted"

    def as_evidence(self) -> dict[str, Any]:
        return {
            "mx_host": self.provider or (self.hosts[0] if self.hosts else None),
            "mx_valid": self.valid,
        }


def identify_mail_host(hosts: list[str]) -> tuple[str | None, str | None]:
    """`['aspmx.l.google.com']` -> `("Google Workspace", "reliable")`."""
    joined = " ".join(h.lower() for h in hosts)
    for fragment, name, reliability in MAIL_HOSTS:
        if fragment in joined:
            return name, reliability
    return None, None


def reset_mx_cache() -> None:
    """Test helper — forget every memoised lookup."""
    with _mx_lock:
        _mx_cache.clear()


class MXUnavailable(RuntimeError):
    """The MX check itself could not run — not a verdict about the domain.

    Its own exception type rather than the obvious `EnvironmentError`, because
    `EnvironmentError` is an alias of `OSError` and `TimeoutError` is a subclass
    of it. Using it here silently caught every DNS timeout as "dnspython is
    missing", and reported a resolver failure as a tooling failure.
    """


def _resolve_mx(domain: str, timeout: float) -> list[str]:
    """The one place this package touches DNS.

    `dnspython` is an optional dependency on purpose. Without it the check
    degrades to "unknown" rather than failing: an MX answer improves a finding,
    it never gates one, and a missing wheel must not stop the free engine.
    """
    try:
        import dns.resolver  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise MXUnavailable(
            "dnspython is not installed, so the MX check is unavailable "
            "(`pip install dnspython`). Everything else still works."
        ) from exc

    resolver = dns.resolver.Resolver()
    resolver.timeout = timeout
    resolver.lifetime = timeout
    answers = resolver.resolve(domain, "MX")
    ranked = sorted(answers, key=lambda r: int(getattr(r, "preference", 0)))
    return [str(r.exchange).rstrip(".").lower() for r in ranked]


def mx_check(
    domain: str,
    *,
    timeout: float = 5.0,
    resolver: Any | None = None,
    use_cache: bool = True,
) -> MXResult:
    """Does this domain accept mail at all, and who hosts it?

    `resolver` is injected by every test — the suite makes no network call of
    any kind, DNS included (§8).
    """
    domain = _as_domain(domain)
    if not domain:
        return MXResult(domain="", valid=False, error="no domain")

    if use_cache and resolver is None:
        with _mx_lock:
            cached = _mx_cache.get(domain)
        if cached and time.monotonic() - cached[0] < MX_CACHE_TTL:
            return cached[1]

    lookup = resolver or (lambda d: _resolve_mx(d, timeout))
    try:
        hosts = [str(h).rstrip(".").lower() for h in (lookup(domain) or [])]
    except MXUnavailable as exc:
        # The checker is missing, not the records. Never cached: installing
        # dnspython should start working without restarting the app.
        return MXResult(domain=domain, valid=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - NXDOMAIN, timeout, SERVFAIL, …
        result = MXResult(
            domain=domain, valid=False,
            error=f"{type(exc).__name__}: {str(exc)[:120]}" if str(exc) else type(exc).__name__,
        )
        _remember(domain, result, use_cache and resolver is None)
        return result

    if not hosts:
        result = MXResult(domain=domain, valid=False, error="no MX records")
    else:
        provider, reliability = identify_mail_host(hosts)
        result = MXResult(
            domain=domain, valid=True, hosts=hosts,
            provider=provider, verification_reliability=reliability,
        )
    _remember(domain, result, use_cache and resolver is None)
    return result


def _remember(domain: str, result: MXResult, enabled: bool) -> None:
    if enabled:
        with _mx_lock:
            _mx_cache[domain] = (time.monotonic(), result)


# ──────────────────────────── the whole check ────────────────────────────


@dataclass(frozen=True)
class AddressChecks:
    """Every free check on one address, in one object."""

    email: str
    syntax_ok: bool
    is_role: bool
    is_disposable: bool
    is_free_provider: bool
    mx: MXResult | None = None

    @property
    def mx_valid(self) -> bool | None:
        return None if self.mx is None else self.mx.valid

    @property
    def usable(self) -> bool:
        """Is this worth showing the owner as somebody's address at all?

        A role address is not a person and a disposable one is not a mailbox.
        An unresolvable MX is *not* disqualifying here: DNS can be down, and
        deleting a finding because a lookup timed out would be worse than
        showing it with "mail host unknown" against it.
        """
        return self.syntax_ok and not self.is_role and not self.is_disposable

    def as_evidence(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.mx is not None:
            out.update(self.mx.as_evidence())
            if self.mx.verification_reliability:
                out["mx_verification_reliability"] = self.mx.verification_reliability
        if self.is_role:
            out["role_address"] = True
        if self.is_free_provider:
            out["free_provider"] = True
        return out

    def reasons(self) -> list[str]:
        """Plain-English notes for the UI, worst news first."""
        out: list[str] = []
        if not self.syntax_ok:
            out.append("that is not a valid address")
        if self.is_role:
            out.append("shared mailbox, not a person")
        if self.is_disposable:
            out.append("throwaway address provider")
        if self.is_free_provider:
            out.append("personal mailbox, not a company one")
        if self.mx is not None and not self.mx.valid:
            out.append(f"MX: {self.mx.error or 'the domain does not accept mail'}")
        elif self.mx is not None and self.mx.provider:
            out.append(f"mail hosted by {self.mx.provider}")
        return out


def check_address(
    email: str,
    *,
    check_mx: bool = True,
    resolver: Any | None = None,
    timeout: float = 5.0,
) -> AddressChecks:
    """Run every free check. Never raises — a check that fails is "unknown"."""
    email = normalize(email)
    syntax_ok = is_valid_syntax(email)
    mx: MXResult | None = None
    if check_mx and syntax_ok:
        mx = mx_check(domain_of(email), resolver=resolver, timeout=timeout)
    return AddressChecks(
        email=email,
        syntax_ok=syntax_ok,
        is_role=is_role_address(email) if syntax_ok else False,
        is_disposable=is_disposable(email),
        is_free_provider=is_free_provider(email),
        mx=mx,
    )
