"""The truth layer — what may be said, and what may not.

Half of these are the generic rules from the spec. The other half are Simon's
own, which no off-the-shelf implementation would find and which are the reason
this profile can be trusted: work authorisation is a topic block rather than a
phrasing rule, three specific attributions belong to teammates, and the
reinforcement-learning claim about the trading work is a conflation that invites
a question with no answer.
"""

from __future__ import annotations

import pytest

from pitwall.apply.loader import load_profile
from pitwall.apply.truth import build_truth, derive_block_terms


@pytest.fixture(scope="module")
def profile():
    return load_profile()


@pytest.fixture(scope="module")
def truth(profile):
    return build_truth(profile)


def codes(violations) -> set[str]:
    return {v.code for v in violations}


# ── the run-specific clause: the gap the model just noticed ──

def test_missing_requirements_become_forbidden(profile):
    """The most dangerous hallucination is the one that closes the gap the
    matcher just found.

    SAP HANA rather than Snowflake, because Snowflake is already blocked — it is
    on the gap map at `confidence: none`. Only a term the profile has never heard
    of isolates the run-specific clause.
    """
    clean = build_truth(profile)
    assert not clean.check("The stack is built on SAP HANA.")

    gated = build_truth(profile, missing_requirements=["SAP HANA"])
    hits = gated.check("I have used SAP HANA in production.")
    assert "FORBIDDEN_CLAIM" in codes(hits)
    assert "the posting asks for it" in hits[0].detail


def test_zero_confidence_skills_are_always_forbidden(profile, truth):
    none_skills = [s.name for s in profile.skills if s.confidence == "none"]
    assert none_skills, "the gap map has emptied — check skills.yaml"
    for name in none_skills[:6]:
        assert "FORBIDDEN_CLAIM" in codes(truth.check(f"I have hands-on {name} experience."))


# ── Simon's rules ──

@pytest.mark.parametrize("sentence", [
    "I am authorised to work in Portugal.",
    "I hold a valid work permit for the EU.",
    "I would not require sponsorship for this role.",
    "My residence permit is current.",
    "I am an EU citizen.",
    "I have the right to work in the United Kingdom.",
])
def test_work_authorisation_is_blocked_however_it_is_phrased(truth, sentence):
    """A topic block, not a phrase block — and it holds even when the posting
    raises the subject first. Simon answers that one himself."""
    assert "FORBIDDEN_TOPIC" in codes(truth.check(sentence)), sentence


def test_teammates_work_is_blocked(truth):
    assert "NOT_MY_WORK" in codes(truth.check("My GraphFrames analysis found communities."))
    assert "NOT_MY_WORK" in codes(truth.check("I built the EMA regime filter."))
    assert "NOT_MY_WORK" in codes(truth.check("I ran the BTC walk-forward audit."))


def test_his_own_work_on_the_same_repositories_is_not_blocked(truth):
    """The distinction that matters: he really did contribute to Breakout
    Strategy V2, so neither "EMA" nor "Breakout Strategy" alone may be blocked —
    only the phrase naming the part that is not his."""
    assert not truth.check(
        "I built the RSI momentum-confirmation filter and the Monte Carlo risk module."
    )
    assert not truth.check(
        "Built a medallion ETL pipeline over 74 years of F1 data across 14 tables."
    )


@pytest.mark.parametrize("sentence", [
    "I applied reinforcement learning in the Ophis Research trading work.",
    "The Monte Carlo risk module used a learned policy.",
    "I used RL in breakout-strategy-v2.",
])
def test_reinforcement_learning_in_the_trading_work_is_blocked(truth, sentence):
    """The conflation Simon caught himself: Monte Carlo methods DO appear inside
    RL, but the risk module samples price paths for a FIXED rule-based strategy.
    Nothing learns. "RL in a trading system" invites "walk me through the reward
    function", and there is no answer."""
    assert "FORBIDDEN_PAIRING" in codes(truth.check(sentence)), sentence


def test_reinforcement_learning_elsewhere_is_not_blocked(truth):
    """The other half, and the reason this is a pairing rule rather than a phrase
    block: the sepsis project is real RL evidence and one of the strongest things
    in the profile. Blocking the term outright would delete it."""
    assert truth.check(
        "Benchmarked Q-Learning, SARSA and PPO on a 716-state ICU sepsis MDP."
    ) == []
    assert truth.check("Reward shaping lifted SARSA survival in the sepsis project.") == []


def test_cooccurrence_derivation_splits_technique_from_context(truth):
    by_claim = {claim: (t, c) for t, c, claim in truth.cooccurrence_blocks}
    techniques, contexts = by_claim[
        "reinforcement learning in the Ophis Research / trading work"
    ]
    assert techniques == ["reinforcement learning"]
    assert contexts == ["Ophis Research", "trading work"]


def test_evidence_private_skills_are_claimable_but_not_citable(profile, truth):
    """Real work, in a repository that is not published. The skill may be
    claimed; naming a project for it would be an invention."""
    assert truth.no_project_citation == {"R", "TypeScript", "C#", "Forecasting"}
    for name in truth.no_project_citation:
        assert name in truth.allowed_skills_letter


def test_block_term_derivation_drops_terms_that_describe_his_own_work():
    own = "i built the ema regime confirmation on breakout strategy v2"
    derived = derive_block_terms("the EMA regime filter in Breakout Strategy V2", own)
    assert "EMA" not in derived
    assert "Breakout Strategy" not in derived


# ── generic gates ──

def test_derived_numbers_are_rejected(truth):
    """15,832 -> 63 is in the profile. "over 99%" is a NEW number, and deriving
    one is where exaggeration creeps in."""
    assert "UNSUPPORTED_NUMBER" in codes(truth.check("I reduced query time by over 99%."))


def test_profile_numbers_are_accepted(truth):
    assert not truth.unsupported_numbers(
        "Built a swing-point classifier on 4 years of ETH 15-minute bars"
    )


@pytest.mark.parametrize("verb", ["led", "managed", "architected", "spearheaded"])
def test_seniority_verbs_are_gated(truth, verb):
    assert "UNSUPPORTED_SENIORITY_VERB" in codes(truth.check(f"I {verb} the data team."))


def test_preferred_verbs_pass(truth):
    for verb in ("built", "designed", "benchmarked", "validated", "measured"):
        assert not truth.check(f"I {verb} the evaluation harness.")


def test_supported_sentences_pass_every_gate(truth):
    for sentence in [
        "Built a Monte Carlo pre-trade engine scoring VaR, CVaR and Kelly.",
        "Replayed every simulated path under the live exit rules.",
        "Modelled F1 safety-car deployment as a hierarchical hazard.",
    ]:
        assert truth.check(sentence) == [], sentence


def test_allowed_facts_cover_every_bullet_and_variant(profile, truth):
    ids = {f.id for f in truth.allowed_facts}
    for bullet in profile.experience_bullets:
        assert bullet.id in ids
        for variant in bullet.variants:
            assert variant.id in ids
    for project in profile.projects:
        for bullet in project.bullets:
            assert bullet.id in ids


# ─────────────────────── the adversarial corpus (§14.2) ───────────────────
#
# Hand-written sentences, half true and half subtly false. Run this on every
# prompt or gate change: an edit that drops recall here is a regression no
# matter how much nicer the prose got.
#
# The generic half comes from the spec. The rest is specific to this profile,
# and those are the ones that matter — they encode decisions Simon made about
# what is and is not his, which no generic rule would ever recover.

BLOCKED = [
    # numbers that were never measured
    ("I reduced query time by over 99%.", "UNSUPPORTED_NUMBER"),
    ("The classifier reached 0.99 AUC on held-out data.", "UNSUPPORTED_NUMBER"),
    ("I trained on 4.2 million labelled examples.", "UNSUPPORTED_NUMBER"),
    # tools that are not in the profile
    ("I have production experience deploying models on AWS.", "FORBIDDEN_CLAIM"),
    ("I ran the training jobs on Kubernetes.", "FORBIDDEN_CLAIM"),
    ("I built the orchestration in Airflow.", "FORBIDDEN_CLAIM"),
    ("I used Databricks for the feature store.", "FORBIDDEN_CLAIM"),
    # seniority, in every inflection
    ("I led a team of data scientists.", "UNSUPPORTED_SENIORITY_VERB"),
    ("I own the deployment pipeline end to end.", "UNSUPPORTED_SENIORITY_VERB"),
    ("I am managing two junior analysts.", "UNSUPPORTED_SENIORITY_VERB"),
    ("I architected the whole platform.", "UNSUPPORTED_SENIORITY_VERB"),
    ("I drove the migration to a new stack.", "UNSUPPORTED_SENIORITY_VERB"),
    # the topic that is off-limits however it is phrased
    ("I am authorised to work anywhere in the EU.", "FORBIDDEN_TOPIC"),
    ("No visa sponsorship would be required.", "FORBIDDEN_TOPIC"),
    ("I hold Portuguese residency.", "FORBIDDEN_TOPIC"),
    # work that belongs to teammates
    ("My GraphFrames analysis surfaced the driver network.", "NOT_MY_WORK"),
    ("I wrote the Spark Structured Streaming ingestion.", "NOT_MY_WORK"),
    ("I built the EMA regime filter for the strategy.", "NOT_MY_WORK"),
    ("I produced the BTC walk-forward audit.", "NOT_MY_WORK"),
    # the conflation Simon caught himself making
    ("I applied reinforcement learning in the trading work.", "FORBIDDEN_PAIRING"),
    ("The Monte Carlo risk module learned a policy over time.", "FORBIDDEN_PAIRING"),
]

ALLOWED = [
    # every one of these is traceable to a bullet, a variant or a metric
    "Built a Monte Carlo pre-trade engine scoring VaR, CVaR and Kelly.",
    "Replayed every simulated path under the live exit rules.",
    "Built a swing-point classifier on 4 years of ETH 15-minute bars.",
    "Modelled F1 safety-car deployment as a hierarchical hazard on 2018-2026 race data.",
    "Held 0.94 recall on a 60-case labelled set by tuning thresholds.",
    "Built a medallion ETL pipeline over 74 years of F1 data across 14 tables.",
    "Benchmarked Q-Learning, SARSA, Double Q and PPO on an ICU sepsis MDP.",
    "I built the RSI momentum-confirmation filter and the Monte Carlo risk module.",
    "I authored the ETL and MLlib notebooks on the F1 project.",
    "I compared the genetic algorithm against simulated annealing at an equal 63,000-evaluation budget.",
    # ordinary uses of gated-verb stems, which must not trip anything
    "I built my own evaluation harness before trusting the numbers.",
    "The finding is directly relevant to how the model is used.",
    "I was already ahead of the schedule I set myself.",
    # framing and opinion, which assert nothing checkable
    "Equal budgets are the only way to find out which algorithm is better.",
    "I want to work where the models are actually used for decisions.",
]


@pytest.mark.parametrize("sentence,code", BLOCKED)
def test_adversarial_sentences_are_caught(truth, sentence, code):
    assert code in codes(truth.check(sentence)), sentence


@pytest.mark.parametrize("sentence", ALLOWED)
def test_true_sentences_pass_every_gate(truth, sentence):
    assert truth.check(sentence) == [], sentence


# ── the attraction sentence ──────────────────────────────────────────────────


@pytest.fixture(scope="module")
def gapped(profile):
    """A truth layer with two real gaps, unverified so they survive as written.

    `verify=False` because `verify_gaps` drops a term the profile can prove and
    `looks_like_a_product` drops an activity phrase — neither of which is what
    this test is about.
    """
    return build_truth(
        profile, missing_requirements=["RL environments", "Real estate"], verify=False,
    )


def test_the_sentence_the_prompt_demands_does_not_block_the_letter(gapped):
    """The writer prompt REQUIRES a closing that says what draws him to the work,
    which cannot be written without naming the role's subject. This gate used to
    block the letter for naming it — two of three DO_NOT_SEND letters on
    28 Sep 2026 were exactly that, and both were shippable prose."""
    from pitwall.apply.truth import blocks_sending, severity

    letter = (
        "Dear Hiring Manager,\n\nI built a seven-stage pipeline.\n\n"
        "What excites me most about this role is the blend of research and "
        "engineering: designing new RL environments and reward signals.\n\n"
        "Simon Sazonov"
    )
    hits = gapped.forbidden_hits(letter)
    assert [v.code for v in hits] == ["DOMAIN_IN_INTEREST"]
    assert severity(hits[0].code) == "style"
    assert blocks_sending(hits) == []
    # Still reported, and the note has to read as a judgement call rather than
    # as a verdict.
    assert "read the sentence and decide" in hits[0].detail


@pytest.mark.parametrize("letter,why", [
    (
        "Dear Hiring Manager,\n\nI have built RL environments for three years.\n\n"
        "Simon Sazonov",
        "a plain claim, nowhere near the frame",
    ),
    (
        "Dear Hiring Manager,\n\nI built RL environments at work.\n\n"
        "What draws me to this role is designing new RL environments.\n\n"
        "Simon Sazonov",
        "named once inside the frame and once outside it",
    ),
    (
        "Dear Hiring Manager,\n\nI have built RL environments and am drawn to "
        "more of it.\n\nSimon Sazonov",
        "the frame worn as a coat, mid-sentence",
    ),
])
def test_a_claim_still_blocks_however_it_is_dressed(gapped, letter, why):
    from pitwall.apply.truth import blocks_sending

    hits = gapped.forbidden_hits(letter)
    assert [v.code for v in hits] == ["FORBIDDEN_CLAIM"], why
    assert blocks_sending(hits), why


def test_the_interest_sentence_is_never_operated_on():
    """Surgery here would strip the role's own subject out of the one sentence
    whose job is to name it."""
    from pitwall.apply.truth import NO_SURGERY

    assert "DOMAIN_IN_INTEREST" in NO_SURGERY
