"""The free checks of §3.2 — syntax, MX, disposable, role addresses.

Every DNS lookup in this file goes through an injected resolver. The `no_dns`
fixture raises if anything tries to reach a real nameserver, so "the suite makes
no network call" is enforced rather than hoped for.
"""

from __future__ import annotations

import pytest

from pitwall.enrich.checks import (
    AddressChecks,
    MXResult,
    check_address,
    domain_of,
    find_addresses,
    identify_mail_host,
    is_disposable,
    is_free_provider,
    is_role_address,
    is_valid_syntax,
    local_part,
    mx_check,
    normalize,
    reset_mx_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_mx_cache()
    yield
    reset_mx_cache()


# ──────────────────────────────── syntax ─────────────────────────────────


@pytest.mark.parametrize(
    "email",
    [
        "jane.doe@alpine.com",
        "j.doe@alpine.co.uk",
        "jane_doe@sub.alpine-racing.com",
        "jane+f1@alpine.com",
        "j@alpine.com",
        "jane.o'neill@alpine.com".replace("'", "-"),
    ],
)
def test_valid_addresses_pass(email):
    assert is_valid_syntax(email) is True


@pytest.mark.parametrize(
    "email",
    [
        "", "   ", "jane.doe", "@alpine.com", "jane@", "jane@alpine",
        "jane doe@alpine.com", "jane@@alpine.com", "jane@alpine..com",
        "jane@-alpine.com", "jane@alpine.c",
    ],
)
def test_invalid_addresses_fail(email):
    assert is_valid_syntax(email) is False


def test_an_image_reference_is_not_an_address():
    """SERP snippets are full of `logo@2x.png`. Storing one is a bug."""
    assert is_valid_syntax("logo@2x.png") is False
    assert is_valid_syntax("sprite@3x.svg") is False
    assert is_valid_syntax("brochure@alpine.pdf") is False


def test_an_over_long_local_part_fails():
    assert is_valid_syntax("a" * 65 + "@alpine.com") is False


def test_normalize_strips_the_shapes_a_web_page_produces():
    assert normalize("  <Jane.Doe@Alpine.com>  ") == "jane.doe@alpine.com"
    assert normalize("mailto:Jane@Alpine.com") == "jane@alpine.com"
    assert normalize("jane@alpine.com.") == "jane@alpine.com"
    assert normalize("jane@alpine.com,") == "jane@alpine.com"


def test_local_part_and_domain():
    assert local_part("Jane.Doe@Alpine.com") == "jane.doe"
    assert domain_of("Jane.Doe@Alpine.com") == "alpine.com"
    assert domain_of("nonsense") == ""


# ───────────────────────── harvesting from prose ─────────────────────────


def test_find_addresses_pulls_them_out_of_a_snippet():
    text = "Contact Jane.Doe@alpine.com or call. Press: press@alpine.com."
    assert find_addresses(text) == ["jane.doe@alpine.com", "press@alpine.com"]


def test_find_addresses_restricts_to_one_domain():
    """§2.2 harvests a company's corpus; other people's addresses poison it."""
    text = "jane@alpine.com and an advert for bob@dealership.co.uk"
    assert find_addresses(text, domain="alpine.com") == ["jane@alpine.com"]


def test_find_addresses_dedupes_case_insensitively_and_keeps_order():
    text = "a@x.com then A@X.com again, then c@x.com"
    assert find_addresses(text, domain="x.com") == ["a@x.com", "c@x.com"]


def test_find_addresses_ignores_asset_references():
    assert find_addresses("logo@2x.png sits next to jane@alpine.com") == [
        "jane@alpine.com"
    ]


def test_find_addresses_on_empty_input():
    assert find_addresses("") == []
    assert find_addresses(None) == []


# ─────────────────────────── role addresses ──────────────────────────────


@pytest.mark.parametrize(
    "email",
    ["info@alpine.com", "press@alpine.com", "hr@alpine.com", "careers@alpine.com",
     "no-reply@alpine.com", "press.office@alpine.com", "jobs-uk@alpine.com",
     "hr_team@alpine.com", "sponsorship@alpine.com"],
)
def test_role_addresses_are_flagged(email):
    assert is_role_address(email) is True


@pytest.mark.parametrize(
    "email",
    ["jane.doe@alpine.com", "j.doe@alpine.com", "jdoe@alpine.com",
     "information.systems.lead@alpine.com"],
)
def test_a_persons_address_is_not_a_role_address(email):
    assert is_role_address(email) is False


def test_prefix_matching_does_not_swallow_a_real_name():
    """`press` is a role; `preston` is a surname."""
    assert is_role_address("preston@alpine.com") is False
    assert is_role_address("itzel@alpine.com") is False


# ────────────────────── disposable and free providers ────────────────────


def test_disposable_providers_are_recognised():
    assert is_disposable("someone@mailinator.com") is True
    assert is_disposable("yopmail.com") is True
    assert is_disposable("jane@alpine.com") is False


def test_free_providers_are_recognised_but_not_treated_as_invalid():
    assert is_free_provider("jane.doe@gmail.com") is True
    assert is_free_provider("jane.doe@alpine.com") is False
    # A personal mailbox is a real mailbox; only the pattern step cares.
    assert is_valid_syntax("jane.doe@gmail.com") is True


# ────────────────────────────── MX lookup ────────────────────────────────


def test_mx_check_reports_the_hosts(mx_ok):
    result = mx_check("alpine.com", resolver=mx_ok)
    assert result.valid is True
    assert result.hosts[0] == "aspmx.l.google.com"
    assert result.provider == "Google Workspace"
    assert result.verification_reliability == "reliable"


def test_mx_check_identifies_microsoft_as_unreliable_to_verify():
    """§3.1(3): M365 answers 250 for invalid addresses. That is context the
    owner should see next to a Hunter result, not a hidden caveat."""
    result = mx_check(
        "alpine.com", resolver=lambda d: ["alpine-com.mail.protection.outlook.com"]
    )
    assert result.provider == "Microsoft 365"
    assert result.verification_reliability == "unreliable"


def test_mx_check_with_no_records_is_invalid():
    result = mx_check("alpine.com", resolver=lambda d: [])
    assert result.valid is False
    assert result.error == "no MX records"


def test_a_dns_failure_is_recorded_not_raised():
    def boom(domain):
        raise TimeoutError("dns timed out")

    result = mx_check("alpine.com", resolver=boom)
    assert result.valid is False
    assert "TimeoutError" in (result.error or "")


def test_missing_dnspython_degrades_to_unknown_rather_than_failing():
    """The MX check improves a finding; it must never gate the free engine."""
    from pitwall.enrich.checks import MXUnavailable

    def unavailable(domain):
        raise MXUnavailable("dnspython is not installed")

    result = mx_check("alpine.com", resolver=unavailable)
    assert result.valid is False
    assert "dnspython" in (result.error or "")


def test_a_dns_timeout_is_not_mistaken_for_a_missing_library():
    """`EnvironmentError` is an alias of `OSError`, and `TimeoutError` is one
    of its subclasses — so the "library missing" branch used to swallow every
    resolver timeout and report a tooling failure instead of a DNS one."""
    def timed_out(domain):
        raise TimeoutError("The DNS operation timed out")

    result = mx_check("alpine.com", resolver=timed_out)
    assert "TimeoutError" in (result.error or "")
    assert "dnspython" not in (result.error or "")


def test_mx_check_on_an_empty_domain():
    assert mx_check("", resolver=lambda d: []).valid is False


def test_mx_results_are_cached_within_the_process(monkeypatch):
    calls = []

    def counting(domain):
        calls.append(domain)
        return ["aspmx.l.google.com"]

    # `resolver=` deliberately bypasses the cache so tests stay isolated, so
    # exercise the cached path through the module-level resolver instead.
    monkeypatch.setattr("pitwall.enrich.checks._resolve_mx", lambda d, t: counting(d))
    assert mx_check("alpine.com").valid is True
    assert mx_check("alpine.com").valid is True
    assert calls == ["alpine.com"]


def test_identify_mail_host_returns_none_for_something_unrecognised():
    assert identify_mail_host(["mx.some-tiny-host.example"]) == (None, None)


def test_mx_result_label_reads_as_a_sentence():
    assert "Google Workspace" in MXResult("x.com", True, ["aspmx.l.google.com"],
                                          "Google Workspace").label
    assert MXResult("x.com", False, error="no MX records").label == "no MX records"


# ───────────────────────── the combined check ────────────────────────────


def test_check_address_gathers_everything(mx_ok):
    checks = check_address("Jane.Doe@Alpine.com", resolver=mx_ok)
    assert checks.email == "jane.doe@alpine.com"
    assert checks.syntax_ok is True
    assert checks.is_role is False
    assert checks.mx_valid is True
    assert checks.usable is True
    assert checks.as_evidence()["mx_host"] == "Google Workspace"


def test_a_role_address_is_not_usable(mx_ok):
    checks = check_address("press@alpine.com", resolver=mx_ok)
    assert checks.is_role is True
    assert checks.usable is False
    assert "shared mailbox, not a person" in checks.reasons()


def test_a_disposable_address_is_not_usable(mx_ok):
    assert check_address("x@mailinator.com", resolver=mx_ok).usable is False


def test_an_unresolvable_mx_does_not_make_a_finding_unusable():
    """DNS being down is not evidence that a person's address is wrong."""
    checks = check_address(
        "jane.doe@alpine.com", resolver=lambda d: (_ for _ in ()).throw(TimeoutError())
    )
    assert checks.mx_valid is False
    assert checks.usable is True          # still worth showing, with the caveat
    assert any("MX:" in r for r in checks.reasons())


def test_check_address_can_skip_dns_entirely(no_dns):
    checks = check_address("jane.doe@alpine.com", check_mx=False, resolver=no_dns)
    assert checks.mx is None
    assert checks.mx_valid is None
    assert checks.usable is True


def test_check_address_never_raises_on_junk(no_dns):
    checks = check_address("not an address", check_mx=False, resolver=no_dns)
    assert isinstance(checks, AddressChecks)
    assert checks.syntax_ok is False
    assert checks.usable is False
