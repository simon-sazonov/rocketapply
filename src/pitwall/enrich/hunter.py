"""The Hunter.io adapter — §5 of PITWALL_V3_EMAIL_ENRICHMENT.md.

Manual escalation only. Nothing in this module is ever reached by the search
pipeline, by a bulk action, or by the free chain; the owner clicks on one
person, or on one company, and spends one credit deliberately.

## What a call actually costs

§5.2 says to verify this against Hunter's current documentation rather than
guess it, because the recommendation threshold depends on the answer. Verified
against help.hunter.io on **1 September 2026**:

===================  =========================================================
Account              free
Email Finder         1 credit per call, **charged only if an address is found**
Domain Search        1 credit per **1 to 10 addresses returned** for that domain
Email Verifier       0.5 credits per call
===================  =========================================================

Two consequences, and the second is the reason to read the docs at all:

1. `credits_spent` is a REAL in `enrichment_log`, not an INTEGER. Half a credit
   is a real price and an integer ledger would round it away every time.
2. **Domain Search is billed per ten results, so the `limit` parameter is the
   price.** This module pins it to :data:`DOMAIN_SEARCH_LIMIT` = 10, which
   makes the call cost exactly one credit and makes the preview in §5.2
   truthful. Left unpinned, a company with 40 published addresses would quietly
   cost four credits against a button that promised one. With the cap in place
   §5.2's "recommend the domain call at >= 3 unresolved people" holds exactly.

## What is deliberately absent

Hunter returns phone numbers. §0.3 forbids requesting, storing or displaying
them, so :func:`_person_fields` allow-lists the keys that may be read and
everything else is dropped before the payload is stored. There is a test that
this module never mentions them at all.

The API key never reaches a log line, an exception message, or `raw_json`
(§0.4). Every error path goes through `redact()`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..db import Repos
from .base import (
    CreditsExhausted,
    EmailCandidate,
    EnrichmentContext,
    EnrichmentError,
    StepOutcome,
    redact,
)
from .cache import EnrichmentCache
from .checks import check_address, is_role_address, normalize
from .patterns import split_name

log = logging.getLogger(__name__)

PROVIDER = "hunter"

#: Pinned so one Domain Search costs exactly one credit. See the module
#: docstring — this constant IS the price of the call.
DOMAIN_SEARCH_LIMIT = 10

#: What each endpoint costs, as verified above. Used for the preview the owner
#: is shown *before* clicking, so the number on the button is the real one.
CREDIT_COST: dict[str, float] = {
    "account": 0.0,
    "email-finder": 1.0,
    "domain-search": 1.0,
    "email-verifier": 0.5,
}


class HunterError(EnrichmentError):
    """Hunter refused. Always carries a redacted message."""


class HunterAuthError(HunterError):
    """The key is missing, wrong, or revoked. Not retryable."""


# ─────────────────────────────── the client ──────────────────────────────


@dataclass
class HunterAccount:
    """What `/v2/account` says. Free to call, and §5.1 wants it on screen."""

    plan_name: str = "unknown"
    credits_used: int = 0
    credits_available: int = 0
    reset_date: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return max(0, self.credits_available - self.credits_used)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    def describe(self) -> str:
        """`Hunter · 34 of 50 credits left · resets 1 Oct` — §5.1, verbatim."""
        text = f"Hunter · {self.remaining} of {self.credits_available} credits left"
        if self.reset_date:
            text += f" · resets {_pretty_date(self.reset_date)}"
        return text


def _pretty_date(value: str) -> str:
    from datetime import datetime

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(value)[:10], fmt).strftime("%-d %b")
        except ValueError:
            continue
    return str(value)


def parse_account(payload: dict[str, Any]) -> HunterAccount:
    """Read the account response, tolerating both shapes Hunter has shipped.

    Older responses carry `data.calls.{used,available}`; newer ones carry
    `data.requests.searches.{used,available}` and a separate verifications
    budget. Both are read, and the *searches* budget is the one shown, because
    finding an address is what this feature spends credits on.
    """
    data = payload.get("data") or {}
    used = available = 0

    requests = data.get("requests")
    if isinstance(requests, dict):
        searches = requests.get("searches")
        if isinstance(searches, dict):
            used = int(searches.get("used") or 0)
            available = int(searches.get("available") or 0)

    calls = data.get("calls")
    if not available and isinstance(calls, dict):
        # `calls` is sometimes the flat counter and sometimes nested the same
        # way `requests` is. Read whichever this account returned.
        nested = calls.get("searches")
        if isinstance(nested, dict):
            used = int(nested.get("used") or 0)
            available = int(nested.get("available") or 0)
        else:
            used = int(calls.get("used") or 0)
            available = int(calls.get("available") or 0)

    return HunterAccount(
        plan_name=str(data.get("plan_name") or "unknown"),
        credits_used=used,
        credits_available=available,
        reset_date=str(data.get("reset_date")) if data.get("reset_date") else None,
        raw=data,
    )


class HunterClient:
    """A thin, cached, honest client. One method per endpoint that costs."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        cache: EnrichmentCache | None = None,
        transport: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.hunter_base_url.rstrip("/")
        self.cache = cache if cache is not None else EnrichmentCache(settings=self.settings)
        #: Injected in every test. The suite makes no network call (§8).
        self.transport = transport
        self.calls = 0
        self.cache_hits = 0

    @property
    def key(self) -> str:
        return self.settings.hunter_api_key

    # ── the one place a request is made ──

    def _get(
        self, endpoint: str, params: dict[str, Any], *, use_cache: bool = True
    ) -> dict[str, Any]:
        if not self.key:
            raise HunterAuthError(
                "no HUNTER_API_KEY is set. The free engine works without one; "
                "a key from hunter.io adds the paid lookup."
            )

        if use_cache:
            cached = self.cache.get(PROVIDER, endpoint, params)
            if cached is not None:
                self.cache_hits += 1
                log.info("hunter %s served from cache — no credit spent", endpoint)
                return dict(cached)

        url = f"{self.base_url}/{endpoint}"
        try:
            if self.transport is not None:
                response = self.transport(url, {**params, "api_key": self.key})
            else:
                response = httpx.get(
                    url,
                    params={**params, "api_key": self.key},
                    timeout=self.settings.hunter_timeout_seconds,
                )
        except httpx.HTTPError as exc:
            raise HunterError(f"hunter unreachable: {self._safe(exc)}") from exc

        payload = self._read(response, endpoint)
        if use_cache:
            self.cache.put(PROVIDER, endpoint, params, payload)
        self.calls += 1
        return payload

    def _read(self, response: Any, endpoint: str) -> dict[str, Any]:
        status = int(getattr(response, "status_code", 0))
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001 - an HTML error page is not JSON
            payload = {}
        detail = self._safe(_error_text(payload) or getattr(response, "text", ""))

        if status == 401:
            raise HunterAuthError(f"hunter rejected the API key (401): {detail}")
        if status in (402, 403):
            # 403 is documented as a rate limit and 402 as payment required.
            # Both mean "not now" to a free-plan account, and both must disable
            # the button rather than crash the page (§8).
            raise CreditsExhausted(f"hunter refused the call ({status}): {detail}")
        if status == 429:
            # §5.1 note: Hunter's docs give 429 for "you have reached your usage
            # limit", NOT the 402 the brief expected. Out of credits and "too
            # fast" share a status code, so the body is what tells them apart —
            # and when it cannot, the safe reading is "out", because disabling a
            # button wrongly costs a minute and spending a credit wrongly is
            # 2% of the month.
            raise CreditsExhausted(f"hunter usage limit reached (429): {detail}")
        if status >= 400:
            raise HunterError(f"hunter HTTP {status}: {detail}")
        if not isinstance(payload, dict):
            raise HunterError("hunter returned something that is not a JSON object")
        return payload

    def _safe(self, value: Any) -> str:
        return redact(value, self.key)[:300]

    # ── endpoints ──

    def account(self, *, use_cache: bool = False) -> HunterAccount:
        """Free. Never cached to disk — the whole point is that it is live."""
        return parse_account(self._get("account", {}, use_cache=False))

    def email_finder(self, domain: str, first_name: str, last_name: str) -> dict[str, Any]:
        """1 credit, charged only if an address is found."""
        return self._get(
            "email-finder",
            {"domain": domain, "first_name": first_name, "last_name": last_name},
        )

    def domain_search(self, domain: str, *, limit: int = DOMAIN_SEARCH_LIMIT) -> dict[str, Any]:
        """1 credit — because `limit` is capped at 10. See the module docstring."""
        return self._get(
            "domain-search",
            {"domain": domain, "limit": min(int(limit), DOMAIN_SEARCH_LIMIT)},
        )


def _error_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return str(first.get("details") or first.get("id") or "")
    return ""


# ──────────────────────── reading Hunter's payloads ──────────────────────

#: The only keys read from a Hunter person record. An allow-list, not a
#: deny-list: Hunter returns `phone_number`, and §0.3 forbids requesting,
#: storing or displaying one. A deny-list would let a new field through.
_PERSON_KEYS = (
    "value", "first_name", "last_name", "position", "confidence",
    "type", "verification",
)


def _person_fields(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    return {k: record.get(k) for k in _PERSON_KEYS if record.get(k) is not None}


def _sources_url(record: Any) -> str | None:
    """The first page Hunter says it saw the address on, if it says."""
    if not isinstance(record, dict):
        return None
    sources = record.get("sources")
    if isinstance(sources, list) and sources and isinstance(sources[0], dict):
        return str(sources[0].get("uri") or "") or None
    return None


def _person_name(record: dict[str, Any]) -> str | None:
    first = str(record.get("first_name") or "").strip()
    last = str(record.get("last_name") or "").strip()
    return f"{first} {last}".strip() or None


# ────────────────────────── availability and credits ─────────────────────


def hunter_available(
    settings: Settings | None = None, repos: Repos | None = None
) -> tuple[bool, str]:
    """Can a paid call be made right now, and if not, why?

    §5.1: "When credits reach zero, disable the escalation buttons with the
    reset date shown, rather than letting a click fail."
    """
    settings = settings or get_settings()
    if not settings.hunter_api_key:
        return False, (
            "no HUNTER_API_KEY is set. Everything else here works without one — "
            "a free key from hunter.io adds 50 paid lookups a month."
        )
    if repos is not None:
        state = repos.quota.credit_state(PROVIDER)
        if state["credits"] is not None and state["credits"] <= 0:
            when = state["reset_at"]
            return False, (
                "no Hunter credits left"
                + (f" — they reset on {_pretty_date(when)}" if when else "")
            )
    return True, "ready"


def credit_state(
    repos: Repos, settings: Settings | None = None, *, client: HunterClient | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """What the sidebar shows. Live from Hunter when asked, cached otherwise.

    §5.1 wants the real remaining credits visible at all times, and polling
    `/v2/account` is free — but a Streamlit rerun happens on every keystroke,
    so the stored value stands in between polls.
    """
    settings = settings or get_settings()
    stored = repos.quota.credit_state(PROVIDER)
    out = {
        "configured": bool(settings.hunter_api_key),
        "credits": stored["credits"],
        "reset_at": stored["reset_at"],
        "allowance": settings.hunter_free_monthly_credits,
        "plan": None,
        "live": False,
        "error": None,
    }
    if not out["configured"] or not refresh:
        return out

    try:
        account = (client or HunterClient(settings)).account()
    except EnrichmentError as exc:
        # A failed poll is a display problem, never a page-breaking one.
        out["error"] = str(exc)
        return out

    with repos.writing():
        repos.quota.set_credits(PROVIDER, account.remaining, reset_at=account.reset_date)
    out.update(
        credits=account.remaining,
        reset_at=account.reset_date,
        allowance=account.credits_available or settings.hunter_free_monthly_credits,
        plan=account.plan_name,
        live=True,
    )
    return out


def _charge(ctx: EnrichmentContext, amount: float) -> None:
    """Record a spend against `quota`. §6: no parallel counter."""
    if amount <= 0:
        return
    with ctx.repos.writing():
        ctx.repos.quota.consume(f"{PROVIDER}:calls", 1)
        ctx.repos.quota.spend_credits(PROVIDER, amount)


# ─────────────────────────── Email Finder (1 credit) ─────────────────────


class HunterFinderStep:
    """*"Find this person (1 credit)"* — §5.2's per-person escalation."""

    name = "hunter.finder"
    spends_credits = True

    def __init__(self, *, client: HunterClient | None = None) -> None:
        self.client = client

    def run(self, ctx: EnrichmentContext) -> StepOutcome:
        ok, why = hunter_available(ctx.settings, ctx.repos)
        if not ok:
            return StepOutcome(step=self.name, outcome="skipped", detail=why)
        if not ctx.domain:
            return StepOutcome(
                step=self.name, outcome="skipped",
                detail="no domain resolved — a credit spent on the wrong domain "
                       "is a credit gone",
            )
        parts = split_name(ctx.full_name)
        if parts is None:
            return StepOutcome(
                step=self.name, outcome="skipped",
                detail=f"{ctx.full_name!r} is not a usable first-and-last name",
            )

        client = self.client or HunterClient(ctx.settings)
        cached_before = client.cache_hits
        try:
            payload = client.email_finder(ctx.domain, parts.first, parts.last)
        except CreditsExhausted as exc:
            ctx.note(str(exc))
            return StepOutcome(step=self.name, outcome="skipped", detail=str(exc))
        except EnrichmentError as exc:
            return StepOutcome(step=self.name, outcome="failed", detail=str(exc))

        from_cache = client.cache_hits > cached_before
        data = payload.get("data") or {}
        address = normalize(str(data.get("email") or ""))

        if not address:
            # "If no email can be found, no credit is charged" — Hunter's own
            # documentation. Recording a spend here would make the ledger lie.
            return StepOutcome(
                step=self.name, outcome="empty", credits_spent=0.0,
                detail=f"Hunter has nobody by that name at {ctx.domain} "
                       "(no credit charged)",
            )

        cost = 0.0 if from_cache else CREDIT_COST["email-finder"]
        if cost:
            _charge(ctx, cost)

        checks = check_address(address, check_mx=False)
        confidence = data.get("score")
        verification = (data.get("verification") or {}).get("status")

        # §4: Hunter returning it as deliverable earns `verified`. A merely
        # "found" address is Hunter's own inference and is treated as one.
        deliverable = str(verification or "").lower() in ("valid", "deliverable")
        status = "verified" if deliverable else "probable"
        if checks.is_role:
            # A role address is not this person's address, whatever it cost.
            status = "guess"
            ctx.note(
                f"Hunter returned {address}, which is a shared mailbox rather "
                "than this person's own address."
            )

        _feed_samples(ctx, [data], source="hunter_finder")

        return StepOutcome(
            step=self.name,
            outcome="ok",
            credits_spent=cost,
            detail=(
                f"Hunter found an address at {ctx.domain}"
                + (" (from cache, no credit spent)" if from_cache else " for 1 credit")
            ),
            found=[
                EmailCandidate(
                    email=address,
                    status=status,
                    source="hunter_finder",
                    evidence={
                        "provider": "hunter",
                        "provider_status": verification or "found",
                        "hunter_sources": _sources_url(data),
                        "derived_from_domain": ctx.domain,
                    },
                    # The ONE place a confidence number is legitimate: it is
                    # Hunter's own score, labelled as Hunter's (§6, §4).
                    confidence=float(confidence) if confidence is not None else None,
                    mx_valid=None,
                    is_role_address=checks.is_role,
                    is_disposable=checks.is_disposable,
                    raw={"data": _person_fields(data)},
                )
            ],
        )


# ────────────────────────── Domain Search (1 credit) ─────────────────────


class HunterDomainStep:
    """*"Learn Alpine's pattern (1 credit) — unlocks 7 people"* — §5.2.

    The better spend whenever several people at one company are unresolved: one
    call returns the company's format plus sample addresses, and §5.2 requires
    those to be fed back into `domain_address_samples` so a paid call
    permanently improves the free engine.
    """

    name = "hunter.domain"
    spends_credits = True

    def __init__(self, *, client: HunterClient | None = None) -> None:
        self.client = client

    def run(self, ctx: EnrichmentContext) -> StepOutcome:
        ok, why = hunter_available(ctx.settings, ctx.repos)
        if not ok:
            return StepOutcome(step=self.name, outcome="skipped", detail=why)
        if not ctx.domain:
            return StepOutcome(
                step=self.name, outcome="skipped", detail="no domain resolved"
            )

        client = self.client or HunterClient(ctx.settings)
        cached_before = client.cache_hits
        try:
            payload = client.domain_search(ctx.domain)
        except CreditsExhausted as exc:
            ctx.note(str(exc))
            return StepOutcome(step=self.name, outcome="skipped", detail=str(exc))
        except EnrichmentError as exc:
            return StepOutcome(step=self.name, outcome="failed", detail=str(exc))

        from_cache = client.cache_hits > cached_before
        data = payload.get("data") or {}
        records = [r for r in (data.get("emails") or []) if isinstance(r, dict)]
        cost = 0.0 if from_cache else CREDIT_COST["domain-search"]
        if cost:
            _charge(ctx, cost)

        added = _feed_samples(ctx, records, source="hunter_domain")
        pattern = data.get("pattern")
        if pattern and ctx.company_domain_id is not None:
            # Hunter's own pattern notation ("{first}.{last}") mapped onto the
            # local vocabulary, so one engine reads the corpus, not two.
            local = hunter_pattern_to_local(str(pattern))
            if local:
                with ctx.repos.writing():
                    ctx.repos.domains.set_pattern(
                        ctx.company_domain_id, local, len(records), None
                    )
                ctx.pattern = local
                ctx.pattern_sample_size = len(records)

        return StepOutcome(
            step=self.name,
            outcome="ok" if (records or pattern) else "empty",
            credits_spent=cost,
            detail=(
                f"{len(records)} address(es) and pattern {pattern or 'unknown'} "
                f"for {ctx.domain}, {added} new to the corpus"
                + (" (from cache, no credit spent)" if from_cache else "")
            ),
            samples_added=added,
        )


#: Hunter writes formats as `{first}.{last}`. The local vocabulary is the one
#: `patterns.py` infers, and both have to be the same word for the corpus to be
#: readable by one engine rather than two.
HUNTER_PATTERNS: dict[str, str] = {
    "{first}.{last}": "first.last",
    "{first}{last}": "firstlast",
    "{first}_{last}": "first_last",
    "{first}-{last}": "first-last",
    "{f}{last}": "flast",
    "{f}.{last}": "f.last",
    "{f}_{last}": "f_last",
    "{first}{l}": "firstl",
    "{first}.{l}": "first.l",
    "{last}.{first}": "last.first",
    "{last}{first}": "lastfirst",
    "{last}_{first}": "last_first",
    "{last}{f}": "lastf",
    "{first}": "first",
    "{last}": "last",
    "{f}{l}": "fl",
    "{f}.{l}": "f.l",
}


def hunter_pattern_to_local(pattern: str) -> str | None:
    return HUNTER_PATTERNS.get(str(pattern or "").strip().lower())


def _feed_samples(
    ctx: EnrichmentContext, records: list[Any], *, source: str
) -> int:
    """§5.2: "a paid call permanently improves the free engine".

    Every address Hunter returns goes into `domain_address_samples` with the
    name attached, which is what makes the *next* person at that company free.
    Role addresses are skipped for the same reason they are skipped in a free
    harvest: one `info@` in the corpus corrupts the format inference.
    """
    if ctx.company_domain_id is None:
        return 0
    added = 0
    with ctx.repos.writing():
        for record in records:
            if not isinstance(record, dict):
                continue
            email = normalize(str(record.get("value") or record.get("email") or ""))
            if not email or is_role_address(email):
                continue
            if ctx.repos.domains.add_sample(
                ctx.company_domain_id, email,
                person_name=_person_name(record),
                source_url=_sources_url(record),
                source=source,
            ):
                added += 1
        ctx.samples = ctx.repos.domains.samples(ctx.company_domain_id)
    return added


# ───────────────────────── the spend recommendation ──────────────────────


@dataclass
class SpendAdvice:
    """What §5.2's escalation UI should offer, and which to recommend."""

    domain: str | None
    unresolved: int
    recommend: str            # "domain" | "finder" | "none"
    reason: str
    cost: float = 1.0

    @property
    def offers_domain_search(self) -> bool:
        return self.unresolved >= 2 and bool(self.domain)


def advise(
    repos: Repos,
    company_id: int | None,
    domain: str | None,
    settings: Settings | None = None,
) -> SpendAdvice:
    """One Domain Search, or one Email Finder each?

    The arithmetic is only sound because `DOMAIN_SEARCH_LIMIT` pins the domain
    call to one credit — see the module docstring. At or above the configured
    threshold the domain call is the better spend by a whole credit per extra
    person, and it also improves the free engine permanently.
    """
    settings = settings or get_settings()
    unresolved = unresolved_at_company(repos, company_id) if company_id else 0
    threshold = settings.hunter_domain_search_threshold

    if not domain:
        return SpendAdvice(None, unresolved, "none",
                           "no domain resolved, so a credit would be guessing")
    if unresolved >= threshold:
        return SpendAdvice(
            domain, unresolved, "domain",
            f"one call learns the format for all {unresolved} unresolved people "
            f"here, instead of {unresolved} calls at one credit each",
        )
    return SpendAdvice(
        domain, unresolved, "finder",
        f"only {unresolved} unresolved here, so looking this person up directly "
        "is the cheaper call",
    )


def unresolved_at_company(repos: Repos, company_id: int) -> int:
    """People at this company with no confirmed address — §7.2 item 6.

    "Unresolved" is deliberately anything below `verified`: a `probable` is
    still a hypothesis, and learning the company's real format is exactly what
    would settle it.
    """
    row = repos.conn.execute(
        """
        SELECT COUNT(DISTINCT c.id) AS n
          FROM candidates c
          LEFT JOIN crm m ON m.candidate_id = c.id
         WHERE COALESCE(m.status, 'new') != 'ignored'
           AND (
                c.current_company = (SELECT name FROM companies WHERE id = :cid)
             OR c.id IN (SELECT candidate_id FROM scores WHERE company_id = :cid)
           )
           AND NOT EXISTS (
                SELECT 1 FROM contact_emails e
                 WHERE e.candidate_id = c.id AND e.status = 'verified'
           )
        """,
        {"cid": company_id},
    ).fetchone()
    return int(row["n"]) if row else 0
