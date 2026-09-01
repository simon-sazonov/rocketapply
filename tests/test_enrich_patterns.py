"""Format inference — §2.3, and the table-test §8 asks for by name.

The assertion that matters most in this file is not that the right format is
inferred. It is that **n=1 is never reported as confident**: §2.3 says "a
pattern from one address is a guess, from three is close to fact. Never present
n=1 and n=5 identically", and §4 turns that into the difference between an
amber `probable` chip and a grey `guess` one.
"""

from __future__ import annotations

import pytest

from pitwall.enrich.patterns import (
    FORMATS,
    GENERIC_PERMUTATIONS,
    LOW_INFORMATION_FORMATS,
    PatternStep,
    address_for,
    apply_format,
    format_of,
    formats_explaining,
    generic_addresses,
    infer_pattern,
    shape_of,
    split_name,
)

# ─────────────────────────────── the name ────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "first", "last"),
    [
        ("Jane Doe", "jane", "doe"),
        ("jane doe", "jane", "doe"),
        ("Dr. Jane Doe", "jane", "doe"),
        ("Jane Doe PhD", "jane", "doe"),
        ("Jean-Pierre Dupont", "jeanpierre", "dupont"),
        ("José Peña", "jose", "pena"),
        ("Jane Ann Doe", "jane", "doe"),
        ("Jane Doe | Aerodynamicist at Alpine", "jane", "doe"),
        ("Jane Doe (she/her)", "jane", "doe"),
        ("Jane Doe - Head of Aero", "jane", "doe"),
        ("Max van der Berg", "max", "berg"),
        ("Seán O'Sullivan", "sean", "osullivan"),
    ],
)
def test_split_name(raw, first, last):
    parts = split_name(raw)
    assert parts is not None
    assert (parts.first, parts.last) == (first, last)


def test_particles_are_kept_as_a_second_reading():
    parts = split_name("Max van der Berg")
    assert parts.last_with_particles == "vanderberg"
    assert set(parts.surnames()) == {"berg", "vanderberg"}


@pytest.mark.parametrize("raw", ["", None, "Madonna", "   ", "Dr.", "|||"])
def test_an_unusable_name_returns_none(raw):
    assert split_name(raw) is None


# ─────────────── the table test §8 asks for, one row per format ──────────

#: One known name -> address pair per format. Every one of these is a real
#: shape a company uses.
TABLE = [
    ("first.last", "Jane Doe", "jane.doe"),
    ("flast", "Jane Doe", "jdoe"),
    ("first", "Jane Doe", "jane"),
    ("firstlast", "Jane Doe", "janedoe"),
    ("first_last", "Jane Doe", "jane_doe"),
    ("f.last", "Jane Doe", "j.doe"),
    ("firstl", "Jane Doe", "janed"),
    ("last.first", "Jane Doe", "doe.jane"),
    ("lastf", "Jane Doe", "doej"),
    ("first-last", "Jane Doe", "jane-doe"),
    ("lastfirst", "Jane Doe", "doejane"),
    ("last_first", "Jane Doe", "doe_jane"),
    ("last", "Jane Doe", "doe"),
    ("f_last", "Jane Doe", "j_doe"),
    ("first.l", "Jane Doe", "jane.d"),
    ("fl", "Jane Doe", "jd"),
    ("f.l", "Jane Doe", "j.d"),
]


@pytest.mark.parametrize(("pattern", "name", "local"), TABLE, ids=[t[0] for t in TABLE])
def test_each_format_builds_the_address_it_claims(pattern, name, local):
    assert apply_format(pattern, split_name(name), "alpine.com") == f"{local}@alpine.com"


@pytest.mark.parametrize(("pattern", "name", "local"), TABLE, ids=[t[0] for t in TABLE])
def test_each_format_is_recognised_from_its_own_output(pattern, name, local):
    assert pattern in formats_explaining(local, split_name(name))


def test_every_named_format_has_a_builder():
    for pattern in FORMATS:
        assert apply_format(pattern, split_name("Jane Ann Doe"), "alpine.com")


def test_a_disambiguating_digit_does_not_break_recognition():
    """Companies append a digit for a name collision: `j.doe2@`."""
    assert "f.last" in formats_explaining("j.doe2", split_name("Jane Doe"))


def test_a_particle_surname_is_recognised_either_way():
    parts = split_name("Max van der Berg")
    assert "first.last" in formats_explaining("max.berg", parts)
    assert "first.last" in formats_explaining("max.vanderberg", parts)


# ───────────────────── inference, and its sample size ────────────────────


def test_two_agreeing_samples_infer_the_format():
    guess = infer_pattern([
        {"email": "jane.doe@alpine.com", "person_name": "Jane Doe"},
        {"email": "marc.dubois@alpine.com", "person_name": "Marc Dubois"},
    ])
    assert guess.pattern == "first.last"
    assert guess.sample_size == 2
    assert guess.agreement == 1.0


def test_one_sample_is_reported_as_one_sample():
    """§2.3: never present n=1 and n=5 identically."""
    guess = infer_pattern([{"email": "jane.doe@alpine.com", "person_name": "Jane Doe"}])
    assert guess.pattern == "first.last"
    assert guess.sample_size == 1
    assert guess.meets(2) is False
    assert "single known address" in guess.describe()
    assert "guess" in guess.describe()


def test_five_samples_read_differently_from_one():
    one = infer_pattern([{"email": "jane.doe@a.com", "person_name": "Jane Doe"}])
    many = infer_pattern([
        {"email": "jane.doe@a.com", "person_name": "Jane Doe"},
        {"email": "marc.dubois@a.com", "person_name": "Marc Dubois"},
        {"email": "anna.klein@a.com", "person_name": "Anna Klein"},
    ])
    assert one.describe() != many.describe()
    assert one.as_evidence()["sample_size"] != many.as_evidence()["sample_size"]


def test_evidence_never_contains_a_bare_confidence_number():
    """§4: a percentage invites the owner to trust a hypothesis."""
    evidence = infer_pattern(
        [{"email": "jane.doe@a.com", "person_name": "Jane Doe"}]
    ).as_evidence()
    assert "confidence" not in evidence
    assert evidence["sample_size"] == 1


def test_a_conflicting_sample_lowers_the_agreement():
    guess = infer_pattern([
        {"email": "jane.doe@a.com", "person_name": "Jane Doe"},
        {"email": "marc.dubois@a.com", "person_name": "Marc Dubois"},
        {"email": "aklein@a.com", "person_name": "Anna Klein"},
    ])
    assert guess.pattern == "first.last"
    assert guess.sample_size == 2
    assert guess.conflicts == 1
    assert guess.agreement == pytest.approx(2 / 3)


def test_role_addresses_never_enter_the_corpus():
    """`info@` would "match" the `first` format for someone called Info."""
    guess = infer_pattern([
        {"email": "info@alpine.com", "person_name": None},
        {"email": "press@alpine.com", "person_name": None},
        {"email": "jane.doe@alpine.com", "person_name": "Jane Doe"},
    ])
    assert guess.pattern == "first.last"
    assert guess.sample_size == 1


def test_nameless_samples_never_raise_the_sample_size():
    """A shape cannot distinguish first.last from last.first, so two shapes are
    not two samples — counting them would promote a guess to `probable` on
    evidence that never existed."""
    guess = infer_pattern([
        {"email": "anna.klein@a.com", "person_name": None},
        {"email": "marc.dubois@a.com", "person_name": None},
    ])
    assert guess.sample_size == 0
    assert guess.shape_support == 2
    assert guess.meets(2) is False
    assert "nothing at this company confirms it" in guess.describe()


def test_a_nameless_sample_can_be_rescued_by_someone_already_in_the_database():
    """Free, and not circular: the candidate list was collected independently
    of any address."""
    guess = infer_pattern(
        [
            {"email": "jdoe@alpine.com", "person_name": None},
            {"email": "mdubois@alpine.com", "person_name": None},
        ],
        known_people=[{"full_name": "Jane Doe"}, {"full_name": "Marc Dubois"}],
    )
    assert guess.pattern == "flast"
    assert guess.sample_size == 2


def test_an_ambiguous_rescue_is_refused():
    """`jdoe@` could be Jane Doe or John Doe, so it identifies neither."""
    guess = infer_pattern(
        [{"email": "jdoe@alpine.com", "person_name": None}],
        known_people=[{"full_name": "Jane Doe"}, {"full_name": "John Doe"}],
    )
    assert guess.sample_size == 0


def test_no_samples_at_all():
    guess = infer_pattern([])
    assert guess.pattern is None
    assert guess.sample_size == 0
    assert guess.describe() == "no address format could be worked out"


def test_ties_break_towards_the_commoner_format():
    """`jd@` is explained by several formats; the tie-break never invents
    confidence, it only picks which equally-supported answer to show."""
    guess = infer_pattern([{"email": "jane@alpine.com", "person_name": "Jane Doe"}])
    assert guess.pattern == "first"
    assert guess.sample_size == 1


# ────────────────────────────── the shapes ───────────────────────────────


@pytest.mark.parametrize(
    ("local", "shape"),
    [("jane.doe", "w.w"), ("j.doe", "i.w"), ("jdoe", "w"), ("jane_doe", "w_w"),
     ("jane-doe", "w-w"), ("j.d", "i.i"), ("jane.doe2", "w.w"), ("", "")],
)
def test_shape_of(local, shape):
    assert shape_of(local) == shape


# ───────────────────────────── applying it ───────────────────────────────


def test_address_for_applies_the_pattern():
    assert address_for("Jane Doe", "alpine.com", "first.last") == "jane.doe@alpine.com"


def test_a_company_format_is_never_applied_to_a_personal_mailbox():
    """A personal mailbox is chosen by its owner, not generated by IT policy."""
    assert address_for("Jane Doe", "gmail.com", "first.last") is None
    assert generic_addresses("Jane Doe", "gmail.com") == []


def test_address_for_refuses_an_unusable_name():
    assert address_for("Madonna", "alpine.com", "first.last") is None
    assert address_for(None, "alpine.com", "first.last") is None


def test_generic_permutations_are_ordered_by_frequency():
    assert generic_addresses("Jane Doe", "alpine.com") == [
        "jane.doe@alpine.com", "jdoe@alpine.com",
        "janedoe@alpine.com", "j.doe@alpine.com",
    ]


def test_generic_permutations_exclude_the_low_information_formats():
    """`jd@alpine.com` is as likely to reach a stranger as the person meant."""
    assert not set(GENERIC_PERMUTATIONS) & LOW_INFORMATION_FORMATS


def test_format_of_reads_a_persons_own_address():
    assert format_of("j.doe@alpine.com", "Jane Doe") == "f.last"
    assert format_of("nonsense@alpine.com", "Jane Doe") is None


# ─────────────────────────── the pattern step ────────────────────────────


def _samples(repos, ctx, pairs):
    domain_id = repos.domains.upsert(int(ctx.company["id"]), "alpine.com")
    ctx.domain = "alpine.com"
    ctx.domain_source = "config"
    ctx.company_domain_id = domain_id
    for email, person in pairs:
        repos.domains.add_sample(domain_id, email, person_name=person)
    ctx.samples = repos.domains.samples(domain_id)
    return domain_id


def test_two_samples_and_a_valid_mx_earn_probable(enrich_ctx, repos, mx_ok):
    ctx = enrich_ctx()
    _samples(repos, ctx, [
        ("marc.dubois@alpine.com", "Marc Dubois"),
        ("anna.klein@alpine.com", "Anna Klein"),
    ])
    outcome = PatternStep(resolver=mx_ok).run(ctx)

    assert outcome.ok
    found = outcome.found[0]
    assert found.email == "jane.doe@alpine.com"
    assert found.status == "probable"
    assert found.evidence["sample_size"] == 2
    assert found.evidence["mx_host"] == "Google Workspace"
    assert found.confidence is None


def test_one_sample_only_earns_guess(enrich_ctx, repos, mx_ok):
    ctx = enrich_ctx()
    _samples(repos, ctx, [("marc.dubois@alpine.com", "Marc Dubois")])
    found = PatternStep(resolver=mx_ok).run(ctx).found[0]
    assert found.status == "guess"
    assert found.evidence["sample_size"] == 1


def test_no_samples_gives_a_generic_permutation_as_a_guess(enrich_ctx, repos, mx_ok):
    ctx = enrich_ctx()
    _samples(repos, ctx, [])
    found = PatternStep(resolver=mx_ok).run(ctx).found[0]
    assert found.status == "guess"
    assert found.email == "jane.doe@alpine.com"
    assert found.evidence["generic_permutation"] is True
    assert found.evidence["sample_size"] == 0


def test_a_domain_with_no_mx_records_produces_nothing(enrich_ctx, repos):
    """No MX means the domain accepts no mail, so every address derived from it
    is worthless — and the domain is probably the thing that is wrong."""
    ctx = enrich_ctx()
    _samples(repos, ctx, [
        ("marc.dubois@alpine.com", "Marc Dubois"),
        ("anna.klein@alpine.com", "Anna Klein"),
    ])
    outcome = PatternStep(resolver=lambda d: []).run(ctx)
    assert outcome.outcome == "empty"
    assert outcome.found == []
    assert any("no MX records" in n for n in ctx.notes)


def test_an_unreachable_resolver_downgrades_probable_to_guess(enrich_ctx, repos):
    """§4 lists MX validity as part of what earns `probable`. An address that
    could not be checked has not earned it — but a resolver timeout is evidence
    of nothing, so the finding survives as a guess."""
    ctx = enrich_ctx()
    _samples(repos, ctx, [
        ("marc.dubois@alpine.com", "Marc Dubois"),
        ("anna.klein@alpine.com", "Anna Klein"),
    ])

    def timed_out(domain):
        raise TimeoutError("dns timed out")

    found = PatternStep(resolver=timed_out).run(ctx).found[0]
    assert found.status == "guess"
    assert found.evidence["sample_size"] == 2
    assert any("could not be checked" in n for n in ctx.notes)


def test_the_step_stores_the_pattern_on_the_company(enrich_ctx, repos, mx_ok):
    """One person's enrichment leaves the answer for everyone else there."""
    ctx = enrich_ctx()
    domain_id = _samples(repos, ctx, [
        ("marc.dubois@alpine.com", "Marc Dubois"),
        ("anna.klein@alpine.com", "Anna Klein"),
    ])
    PatternStep(resolver=mx_ok).run(ctx)
    row = repos.domains.by_id(domain_id)
    assert (row["pattern"], row["pattern_sample_size"]) == ("first.last", 2)
    assert row["pattern_agreement"] == 1.0


def test_the_step_skips_without_a_domain(enrich_ctx, no_dns):
    ctx = enrich_ctx()
    assert PatternStep(resolver=no_dns).run(ctx).outcome == "skipped"


def test_the_step_skips_an_unusable_name(enrich_ctx, repos, no_dns):
    ctx = enrich_ctx(name="Madonna")
    _samples(repos, ctx, [])
    outcome = PatternStep(resolver=no_dns).run(ctx)
    assert outcome.outcome == "skipped"
    assert "usable first-and-last name" in (outcome.detail or "")


def test_the_step_is_free(enrich_ctx):
    assert PatternStep.spends_credits is False
