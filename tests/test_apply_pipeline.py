"""Stages 1 to 6 with stub models — the suite makes no network call (§8).

Every LLM stage takes an injectable client for exactly this reason, so these
tests exercise the real code paths with scripted replies rather than mocking the
functions under test.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pitwall.apply.analyzer import JobAnalysis, Requirement, analyze_job, trim_description
from pitwall.apply.factcheck import check_letter, split_sentences
from pitwall.apply.letter import (
    Letter,
    banned_phrase_hits,
    repair_mechanics,
    write_letter,
    writer_whitelist,
)
from pitwall.apply.llm import TASK_ROUTING, AllProvidersFailed, TaskRouter
from pitwall.apply.loader import load_profile
from pitwall.apply.matcher import build_signal, looks_like_a_product, match, verify_gaps
from pitwall.apply.packager import output_dir, slug
from pitwall.apply.planner import NarrativePlan, plan_letter
from pitwall.apply.truth import build_truth, verb_forms


class StubClient:
    """An OpenAI-compatible client that replays scripted JSON."""

    def __init__(self, *replies: dict, fail: Exception | None = None):
        self.replies = list(replies)
        self.fail = fail
        self.prompts: list[str] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        if self.fail:
            raise self.fail
        self.prompts.append(kwargs["messages"][-1]["content"])
        payload = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        )


@pytest.fixture(scope="module")
def profile():
    return load_profile()


def router(**clients) -> TaskRouter:
    return TaskRouter(clients=clients)


POSTING = (
    "We are looking for a graduate data scientist. You must have strong Python skills "
    "and solid SQL. Experience with PyTorch is a plus. You will build and evaluate "
    "machine learning models on time series data. Kubernetes experience is required."
)

ANALYSIS_REPLY = {
    "role_family": "DATA_SCIENCE", "seniority": "ENTRY_LEVEL",
    "required_skills": [
        {"term": "Python", "evidence_quote": "strong Python skills"},
        {"term": "SQL", "evidence_quote": "solid SQL"},
        {"term": "Kubernetes", "evidence_quote": "Kubernetes experience is required"},
        # Invented: this quote is nowhere in the posting.
        {"term": "Terraform", "evidence_quote": "deep Terraform expertise required"},
    ],
    "preferred_skills": [{"term": "PyTorch", "evidence_quote": "PyTorch is a plus"}],
    "responsibilities": ["build and evaluate machine learning models"],
    "domain": [], "keywords": ["time series"], "language": "English",
    "company_signals": [], "hard_gates": [],
}


# ── stage 1 ──

def test_invented_requirements_are_dropped():
    """A 20B model will occasionally invent a requirement. The quote check
    catches it for free, and a fabricated one is worse than a missed one —
    it would become a forbidden claim."""
    analysis = analyze_job(
        {"title": "Graduate Data Scientist", "description": POSTING},
        router(groq=StubClient(ANALYSIS_REPLY)),
    )
    terms = [r.term for r in analysis.required_skills]
    assert "Python" in terms and "Kubernetes" in terms
    assert "Terraform" not in terms
    assert ("Terraform", "deep Terraform expertise required") in analysis.rejected


def test_language_is_normalised_to_a_two_letter_code():
    analysis = analyze_job({"description": POSTING}, router(groq=StubClient(ANALYSIS_REPLY)))
    assert analysis.language == "en"


def test_quotes_are_matched_against_the_untrimmed_posting():
    """A quote from a section that truncation removed is still a real quote."""
    padded = "filler. " * 2000 + POSTING
    analysis = analyze_job(
        {"description": padded}, router(groq=StubClient(ANALYSIS_REPLY))
    )
    assert analysis.truncated
    assert "Python" in [r.term for r in analysis.required_skills]


def test_trim_keeps_the_requirements_section():
    body = "intro. " * 900 + "\nRequirements\nYou must have Rust and Go.\n" + "tail. " * 900
    trimmed, truncated = trim_description(body, limit=2000)
    assert truncated
    assert "Rust and Go" in trimmed


# ── stage 2 ──

def test_declared_gaps_and_real_evidence_are_told_apart(profile):
    analysis = analyze_job(
        {"description": POSTING}, router(groq=StubClient(ANALYSIS_REPLY))
    )
    result = match(analysis, profile)
    assert {m.requirement for m in result.matched} >= {"Python", "SQL"}
    assert any(g.requirement == "Kubernetes" and g.declared_gap for g in result.missing)


def test_signal_only_carries_canonical_tags(profile):
    analysis = JobAnalysis(
        role_family="DATA_SCIENCE",
        required_skills=[Requirement("Python", "x"), Requirement("PyTorch", "x")],
        keywords=["time series"],
    )
    signal = build_signal(analysis, profile)
    assert "python" in signal.required
    assert "time_series" in signal.keywords
    # PyTorch is not a canonical tag, so it matches no bullet and must not
    # smuggle in the topics of the projects that happen to use it.
    assert "nlp" not in signal.required and "llm" not in signal.required


@pytest.mark.parametrize("term,product", [
    ("AWS", True), ("Kubernetes", True), ("Apache Airflow", True),
    ("model development", False), ("previous internship experience", False),
    ("strong communication skills", False),
])
def test_only_named_technologies_are_worth_forbidding(term, product):
    assert looks_like_a_product(term) is product


def test_a_narrower_inventory_cannot_gag_a_real_strength(profile):
    """`job_matches.missing_skills` reports `simulation` missing because it is
    computed against a narrower inventory. The profile proves otherwise."""
    gaps, contradicted = verify_gaps(profile, ["simulation", "AWS"])
    assert gaps == ["AWS"]
    assert contradicted and contradicted[0][0] == "simulation"


# ── stage 3 ──

def test_gates_are_built_from_both_opinions(profile):
    truth = build_truth(profile, missing_requirements=["Kubernetes", "SAP HANA"])
    assert "Kubernetes" in truth.forbidden_claims
    assert "SAP HANA" in truth.forbidden_claims


@pytest.mark.parametrize("verb,form", [
    ("owned", "owns"), ("managed", "managing"), ("led", "leading"),
    ("architected", "architecting"), ("spearheaded", "spearheads"),
])
def test_gated_verbs_cover_their_inflections(profile, verb, form):
    assert form in verb_forms(verb)
    truth = build_truth(profile)
    assert truth.gated_verb_hits(f"I {form} the evaluation work")


@pytest.mark.parametrize("sentence", [
    "Autonomous driving brings these areas together.",
    "The calibration drives the decision threshold.",
    "This leads to a better calibrated model.",
])
def test_ordinary_inflections_are_not_seniority_claims(profile, sentence):
    """`drove` and `led` are gated, but "autonomous driving" is a field and
    "leads to" is the commonest construction in technical prose. Gating the
    whole family rejected good sentences."""
    assert build_truth(profile).gated_verb_hits(sentence) == [], sentence


def test_founding_verbs_are_gated(profile):
    """Simon's own Red Bull letter says "I co-founded BeeSurfing", while the
    profile records it as a University team project."""
    truth = build_truth(profile)
    assert truth.gated_verb_hits("I co-founded BeeSurfing with three friends.")
    assert truth.gated_verb_hits("I founded the project.")


def test_names_from_the_example_letters_cannot_be_repeated(profile):
    """The writer is shown Simon's weaker letters as things NOT to do, and a
    model shown any text absorbs its nouns. One of them names "Snowit" as an
    employer; Snowit appears nowhere in the profile."""
    truth = build_truth(profile, job={"posting_company": "Anthropic", "title": "Engineer"})
    assert "Snowit" in truth.contaminants
    hits = truth.contamination_hits("At Snowit I designed forecasting tools.")
    assert hits and hits[0].code == "CONTAMINATION"


def test_the_employer_being_applied_to_is_never_a_contaminant(profile):
    """Alpine is a contaminant from an old letter — but not when the letter is
    being written to Alpine."""
    truth = build_truth(profile, job={"posting_company": "Alpine", "title": "Placement"})
    assert truth.contamination_hits("I am applying to Alpine for this placement.") == []


def test_real_profile_content_is_not_flagged_as_contamination(profile):
    """The known-set is built from the whole profile, because a version built
    from org names alone flagged "IMS", "RMSE", "COVID" and "Science"."""
    truth = build_truth(profile, job={"posting_company": "Anthropic", "title": "Engineer"})
    for sentence in (
        "My MSc at NOVA IMS focuses on machine learning.",
        "A gradient-boosted model at 2,533 ms RMSE and 0.878 ROC-AUC.",
        "In my COVID-19 MLOps project I built modular pipelines with Kedro.",
    ):
        assert truth.contamination_hits(sentence) == [], sentence


def test_ordinary_uses_of_those_stems_are_not_blocked(profile):
    truth = build_truth(profile)
    for sentence in ("I built my own harness", "the work is directly relevant",
                     "I was ahead of the deadline"):
        assert truth.gated_verb_hits(sentence) == [], sentence


def test_typographic_thousands_separators_are_folded(profile):
    """A model writes 63 000 with a narrow no-break space; the gate must
    not read that as two unsupported numbers."""
    truth = build_truth(profile)
    assert truth.unsupported_numbers("an equal 63 000-evaluation budget") == []
    assert truth.unsupported_numbers("an equal 71 000-evaluation budget")


# ── stages 4b and 5b ──

PLAN_REPLY = {
    "main_theme": "decisions under uncertainty", "secondary_theme": "measured evaluation",
    "evidence_order": ["ev.monte_carlo_risk", "ev.pitwall"], "company_angle": "",
    "opening_strategy": "problem_first", "avoid": ["AWS"], "target_words": 300,
}


def test_planner_falls_back_when_the_model_names_nothing_usable(profile):
    analysis = JobAnalysis(role_family="QUANTITATIVE")
    result = match(analysis, profile)
    truth = build_truth(profile)
    bad = {**PLAN_REPLY, "evidence_order": ["ev.does_not_exist"]}
    plan, blocks = plan_letter(
        profile, analysis, result, truth, router(groq=StubClient(bad)), {"title": "Quant"}
    )
    assert plan.evidence_order and blocks
    assert all(profile.evidence_block(b) is not None for b in plan.evidence_order)


def test_letter_is_regenerated_when_a_gate_fires(profile):
    truth = build_truth(profile, missing_requirements=["AWS"])
    plan = NarrativePlan(evidence_order=["ev.pitwall"], target_words=300)
    blocks = [profile.evidence_block("ev.pitwall")]
    bad = {"letter": "Dear Hiring Manager,\n\nI have a proven track record "
                     "deploying models on AWS.\n\nSimon Sazonov"}
    good = {"letter": "Dear Hiring Manager,\n\nI built a seven-stage resumable "
                      "pipeline over SQLite.\n\nSimon Sazonov"}
    result = write_letter(
        profile, plan, blocks, truth,
        router(groq=StubClient(bad, good)), {"title": "Engineer"},
    )
    assert result.attempts == 2
    assert result.accepted
    assert "AWS" not in result.text


def test_a_letter_that_never_passes_is_not_accepted(profile):
    truth = build_truth(profile, missing_requirements=["AWS"])
    plan = NarrativePlan(evidence_order=["ev.pitwall"])
    blocks = [profile.evidence_block("ev.pitwall")]
    bad = {"letter": "I led the AWS migration."}
    result = write_letter(
        profile, plan, blocks, truth, router(groq=StubClient(bad)), {"title": "x"}
    )
    assert not result.accepted
    assert len(result.drafts) == 3  # the initial attempt plus two retries


def test_banned_phrases_survive_a_line_break(profile):
    assert banned_phrase_hits("a proven\n  track record", ["proven track record"])
    assert not banned_phrase_hits("a track record of proven work", ["proven track record"])


def test_placeholders_are_caught(profile):
    truth = build_truth(profile)
    plan = NarrativePlan(evidence_order=["ev.pitwall"])
    blocks = [profile.evidence_block("ev.pitwall")]
    reply = {"letter": "I am applying to [Company Name] for the role."}
    result = write_letter(
        profile, plan, blocks, truth, router(groq=StubClient(reply)), {"title": "x"}
    )
    assert not result.accepted
    assert any(v.code == "PLACEHOLDER" for v in result.violations)


def test_the_checker_sees_everything_the_writer_saw(profile):
    """A checker shown only `facts` marks the writer's faithful use of a hook or
    a story UNSUPPORTED. The two whitelists have to be the same one."""
    blocks = [profile.evidence_block("ev.pitwall")]
    whitelist = writer_whitelist(blocks)
    assert blocks[0].one_line_hook.strip() in whitelist
    assert blocks[0].story.strip() in whitelist


# ── stage 6 ──

def test_sentences_with_no_first_person_claim_are_not_audited(profile):
    truth = build_truth(profile)
    letter = Letter(text="The role asks for a strong engineer. It is an interesting problem.")
    client = StubClient({"verdicts": [{"n": 1, "verdict": "UNSUPPORTED",
                              "unsupported_span": "strong engineer"}]})
    result = check_letter(letter, truth, ["a fact"], router(groq=client), written_by="groq")
    assert result.passed
    assert all(v.source == "no-claim" for v in result.verdicts)
    assert client.prompts == []


def test_the_gate_wins_when_the_model_disagrees(profile):
    """The auditor passed it; the deterministic gate did not. The gate is exact
    and the auditor is a judge we already said not to trust."""
    truth = build_truth(profile, missing_requirements=["AWS"])
    letter = Letter(text="I deployed my models on AWS for the team.")
    client = StubClient(
        {"verdicts": [{"n": 1, "verdict": "SUPPORTED", "unsupported_span": None}]}
    )
    result = check_letter(letter, truth, ["a fact"], router(groq=client), written_by="groq")
    assert not result.independent
    assert result.blocks_output
    assert any(v.code == "FORBIDDEN_CLAIM" for v in result.gate_violations)


def test_an_auditor_objection_alone_does_not_block_output(profile):
    """Neither available auditor is reliable enough to hold a veto: Groq wrote
    the text, and local Qwen marked a verbatim fact unsupported on a real run.
    So an objection drives a rewrite and then a NEEDS_REVIEW flag, never a
    silent discard."""
    truth = build_truth(profile)
    letter = Letter(text="I built a seven-stage resumable pipeline over SQLite.")
    client = StubClient(
        {"verdicts": [{"n": 1, "verdict": "UNSUPPORTED",
                       "unsupported_span": "seven-stage"}]}
    )
    result = check_letter(letter, truth, ["a fact"], router(groq=client), written_by="groq")
    assert result.failures
    assert result.needs_review
    assert not result.blocks_output


def test_an_unreadable_verdict_is_not_counted_as_a_pass(profile):
    """Local Qwen answered a real call with {"isAllowed": false, "reason": ...}.
    Reading a missing `verdict` as SUPPORTED would turn the safety net into a
    rubber stamp."""
    from pitwall.apply.factcheck import _audit_batch

    truth = build_truth(profile)  # noqa: F841 — parity with the other cases
    client = StubClient({"isAllowed": False, "reason": "not in the facts"})
    verdicts = _audit_batch(["I deployed to AWS."], ["a fact"], router(groq=client), set())
    assert [v.source for v in verdicts] == ["skipped"]


def test_a_sentence_missing_from_the_batch_reply_is_unchecked_not_passed(profile):
    """The failure a batched audit has and a per-sentence one does not: the
    model answers for three of five and the other two look clean."""
    from pitwall.apply.factcheck import _audit_batch

    client = StubClient({"verdicts": [
        {"n": 1, "verdict": "SUPPORTED", "unsupported_span": None},
        {"n": 3, "verdict": "UNSUPPORTED", "unsupported_span": "AWS"},
    ]})
    verdicts = _audit_batch(
        ["I built one.", "I built two.", "I deployed to AWS."],
        ["a fact"], router(groq=client), set(),
    )
    assert [v.source for v in verdicts] == ["model", "skipped", "model"]
    assert verdicts[2].verdict == "UNSUPPORTED"
    # …and the verdict landed on the sentence it was about, not on position 2.
    assert verdicts[2].sentence == "I deployed to AWS."


def test_an_unreachable_auditor_is_reported_not_treated_as_a_pass(profile):
    truth = build_truth(profile)
    letter = Letter(text="I built a seven-stage resumable pipeline over SQLite.")
    broken = StubClient({}, fail=RuntimeError("connection refused"))
    result = check_letter(letter, truth, ["a fact"], router(groq=broken), written_by="groq")
    assert any("could not be checked" in note for note in result.notes)
    assert not result.blocks_output  # unchecked is reported, never silently blocked


def test_fragments_are_judged_with_their_clause():
    pieces = split_sentences("I tuned in stages. Not merely depth. Then I validated it fully.")
    from pitwall.apply.factcheck import _merge_fragments

    merged = _merge_fragments(pieces)
    assert not any(len(s.split()) < 4 for s in merged)


# ── routing and packaging ──

def test_the_auditor_is_never_the_model_that_wrote_the_letter():
    """A model grading its own output is biased toward passing it.

    Until 28 Sep 2026 that was guaranteed by routing the audit to a different
    PROVIDER — local Ollama — which cost 819 of the 832 seconds a letter took.
    The guarantee is now carried by the model: the same key may answer both, so
    long as `TASK_MODELS` pins a different family for the audit. This test is
    the guarantee; the mechanism is free to change again.
    """
    from pitwall.apply.llm import TASK_MODELS

    writer_provider = TASK_ROUTING["letter_write"][0]
    for provider in TASK_ROUTING["fact_check"]:
        if provider != writer_provider:
            continue  # a different provider is independent on its own
        audit_model = TASK_MODELS.get(("fact_check", provider))
        write_model = TASK_MODELS.get(("letter_write", provider))
        assert audit_model, (
            f"fact_check shares provider {provider!r} with letter_write and pins no "
            f"model of its own — the auditor would be the writer"
        )
        assert audit_model != write_model


def test_the_audit_asks_once_for_the_whole_letter():
    """31 sequential calls at ~26s each was the entire cost of a letter."""
    from pitwall.apply.factcheck import BATCH_SCHEMA

    items = BATCH_SCHEMA["properties"]["verdicts"]
    assert items["type"] == "array"
    # The sentence NUMBER travels with the verdict, so a reordered or short
    # reply cannot shift verdicts onto the wrong sentences.
    assert "n" in items["items"]["properties"]
    assert set(items["items"]["required"]) == {"n", "verdict", "unsupported_span"}


def test_every_provider_failing_raises_rather_than_half_answering():
    broken = StubClient({}, fail=RuntimeError("nope"))
    with pytest.raises(AllProvidersFailed):
        router(groq=broken).complete_json("job_analysis", "sys", "user")


def test_output_directories_are_never_overwritten(tmp_path):
    first = output_dir(tmp_path, "BMW", "Graduate Data Scientist")
    first.mkdir(parents=True)
    (first / "cv.pdf").write_text("x")
    second = output_dir(tmp_path, "BMW", "Graduate Data Scientist")
    assert second != first
    assert second.name.endswith("_v2")


def test_slugs_are_ascii_and_bounded():
    assert slug("Waymo / Alphabet — Zürich!") == "Waymo_Alphabet_Z_Rich"
    assert len(slug("x" * 200)) <= 40


# ── the voice loop ──

def test_exemplars_lead_the_writer_prompt(profile, tmp_path):
    """A whole letter has to come before the rules, because the model imitates an
    example far better than it follows a description of one."""
    from pitwall.apply.letter import _user_prompt

    profile = profile.model_copy(deep=True)
    profile.style.exemplars = [
        {"id": "ex.good", "verdict": "good", "why": "concrete nouns",
         "text": "I built the thing and here is what it cost."},
        {"id": "ex.bad", "verdict": "avoid", "why": "abstractions",
         "text": "I am passionate about leveraging synergies."},
    ]
    truth = build_truth(profile)
    prompt = _user_prompt(
        profile, NarrativePlan(evidence_order=["ev.pitwall"]),
        [profile.evidence_block("ev.pitwall")], truth, {"title": "Engineer"},
    )
    assert prompt.startswith("═══ A LETTER THIS CANDIDATE WROTE")
    assert "I built the thing and here is what it cost." in prompt
    assert "Do NOT write like this" in prompt
    assert prompt.index("I built the thing") < prompt.index("NEVER USE THESE PHRASES")


def test_no_exemplars_still_produces_a_usable_prompt(profile):
    from pitwall.apply.letter import _exemplars, _user_prompt

    assert _exemplars(profile) == "" or profile.style.exemplars
    truth = build_truth(profile)
    prompt = _user_prompt(
        profile, NarrativePlan(evidence_order=["ev.pitwall"]),
        [profile.evidence_block("ev.pitwall")], truth, {"title": "Engineer"},
    )
    assert "FACTS" in prompt and "NEVER USE THESE PHRASES" in prompt


def test_every_writing_sample_reaches_the_prompt(profile):
    """Cutting the samples to three was part of why the voice signal was thin."""
    from pitwall.apply.letter import _style_section

    section = _style_section(profile)
    for sample in profile.style.writing_samples:
        if isinstance(sample, dict) and sample.get("id"):
            assert sample["id"] in section


def test_ratings_round_trip_and_summarise(tmp_path):
    from pitwall.apply.voice_eval import Rating, append, by_version, format_trend, load

    path = tmp_path / "voice_eval.csv"
    for version, natural, burden in (("v1", 2, 60), ("v1", 2, 50), ("v1", 3, 55),
                                     ("v2", 4, 25), ("v2", 4, 30), ("v2", 5, 20)):
        append(Rating(prompt_version=version, job_id=1, naturalness=natural,
                      edit_burden_pct=burden), path)
    rows = load(path)
    assert len(rows) == 6
    summaries = {s.version: s for s in by_version(rows)}
    assert summaries["v1"].edit_burden == 55.0
    assert summaries["v2"].edit_burden == 25.0
    # The spec's bar: three or more letters, edit burden at or under 30%.
    assert not summaries["v1"].ships
    assert summaries["v2"].ships
    assert "ships" in format_trend(rows)


def test_an_empty_log_says_what_to_do(tmp_path):
    from pitwall.apply.voice_eval import format_trend, load

    assert "no ratings yet" in format_trend(load(tmp_path / "nothing.csv"))


def test_an_ordinary_word_in_a_job_title_is_not_a_name(profile):
    """A Waymo letter was really refused for the word "Placement".

    It comes from the Red Bull letter's title, "Race Strategy Student
    Placement", and looks exactly like a proper noun — until you notice the
    Alpine letter also writes "the month the placement starts". A real name is
    essentially never written in lower case, and that one occurrence is the
    difference. The test is in the data rather than in a list to maintain.
    """
    truth = build_truth(profile, job={"posting_company": "Waymo", "title": "Summer Intern"})
    assert "Placement" not in truth.contaminants
    assert truth.contamination_hits("This placement would suit me.") == []
    # The genuine names are still blocked.
    assert {"Snowit", "IndraMind", "Enstone"} <= truth.contaminants


# ── progress, and not re-asking the same question ──

def test_the_check_reports_each_sentence(profile):
    """This stage takes minutes. Without per-sentence reporting the only honest
    thing a progress bar can say is "working", which is indistinguishable from
    stuck — and that is exactly how it was reported."""
    from pitwall.apply.factcheck import check_letter

    truth = build_truth(profile)
    letter = Letter(text="I built a pipeline over SQLite. I labelled sixty candidates myself.")
    seen: list[tuple[int, int]] = []
    client = StubClient({"verdict": "SUPPORTED", "unsupported_span": None})
    check_letter(
        letter, truth, ["a fact"], router(groq=client), written_by="",
        on_sentence=lambda i, total, _text: seen.append((i, total)),
    )
    assert seen, "no sentence was reported"
    assert seen[-1][0] == seen[-1][1], "the last report should be the last sentence"


def test_sentences_a_previous_round_cleared_are_not_reasked(profile):
    """A rewrite usually changes two sentences out of thirteen. Re-asking a
    local 7B model about the other eleven costs ~15 seconds each for an answer
    it has already given."""
    from pitwall.apply.factcheck import check_letter

    truth = build_truth(profile)
    kept = "I built a seven-stage resumable pipeline over SQLite."
    letter = Letter(text=f"{kept} I labelled sixty candidates myself.")
    client = StubClient({"verdict": "SUPPORTED", "unsupported_span": None})
    result = check_letter(
        letter, truth, ["a fact"], router(groq=client), written_by="",
        already_supported={kept},
    )
    assert any(v.source == "unchanged" for v in result.verdicts)
    # The cleared sentence was never sent to the model.
    assert all(kept not in prompt for prompt in client.prompts)


def test_a_cleared_round_hands_its_sentences_forward(profile):
    from pitwall.apply.factcheck import check_letter

    truth = build_truth(profile)
    letter = Letter(text="I built a pipeline over SQLite.")
    client = StubClient({"verdict": "SUPPORTED", "unsupported_span": None})
    result = check_letter(letter, truth, ["a fact"], router(groq=client), written_by="")
    assert result.supported_text == {"I built a pipeline over SQLite."}


# ── degradation, and refusing to degrade ──

def test_prose_never_falls_through_to_a_small_local_model():
    """Learned the hard way. When Groq rate-limited mid-run, `letter_write` fell
    through to local Qwen2.5-7B, which does not imitate a style exemplar — it
    COPIES it, and produced a letter opening with Simon's Alpine thesis sentence
    verbatim, addressed to a different company. Twenty minutes for something
    that could never have passed."""
    from pitwall.apply.llm import CAPABLE_ONLY

    assert {"letter_write", "narrative_plan"} <= CAPABLE_ONLY
    routed = TaskRouter().providers_for("letter_write")
    assert "ollama" not in routed


def test_a_rate_limit_waits_instead_of_degrading(profile, monkeypatch):
    """A per-minute token limit is a PAUSE. The first version classified nothing,
    so a routine limit looked like a dead provider and the run silently moved to
    a model that could not do the job."""
    import pitwall.apply.llm as llm_mod

    class Limited(StubClient):
        def __init__(self, *replies):
            super().__init__(*replies)
            self.calls = 0

        def _create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Rate limit reached. Please try again in 0.01s")
            return super()._create(**kwargs)

    monkeypatch.setattr(llm_mod, "RATE_LIMIT_SLEEP", 0.01)
    waits: list[tuple[str, float, int]] = []
    client = Limited({"ok": True})
    r = TaskRouter(clients={"groq": client})
    r.on_wait = lambda provider, seconds, attempt: waits.append((provider, seconds, attempt))

    assert r.complete_json("letter_write", "sys", "user") == {"ok": True}
    assert client.calls == 2, "it should have retried the same provider"
    assert waits and waits[0][0] == "groq"


def test_a_capable_only_task_says_why_it_cannot_run():
    router = TaskRouter(clients={"ollama": StubClient({})})
    with pytest.raises(AllProvidersFailed) as excinfo:
        router.complete_json("letter_write", "sys", "user")
    assert "capable model" in str(excinfo.value)


def test_a_cached_answer_from_a_weaker_model_is_not_reused(tmp_path):
    """A cached result is only as good as the model that made it, and the key is
    the prompt, which cannot know that. When Groq was rate-limited, the local
    model's near-verbatim copy of the style exemplar was cached under the same
    key — so every later run "succeeded" in two seconds by serving it back, with
    the degradation invisible."""
    import json

    from pitwall.apply.llm import TaskRouter

    cache = tmp_path / "cache"
    cache.mkdir()
    good = StubClient({"letter": "a proper letter"})
    r = TaskRouter(clients={"groq": good}, cache_dir=cache)

    # Poison the cache the way a degraded run would have.
    first = r.complete_json("letter_write", "sys", "user")
    assert first == {"letter": "a proper letter"}
    entry = next(iter(cache.glob("*.json")))
    payload = json.loads(entry.read_text())
    payload["provider"] = "ollama"
    payload["result"] = {"letter": "a copy of the exemplar"}
    entry.write_text(json.dumps(payload))

    again = TaskRouter(clients={"groq": StubClient({"letter": "a proper letter"})},
                       cache_dir=cache)
    assert again.complete_json("letter_write", "sys", "user") == {"letter": "a proper letter"}


def test_a_cached_answer_from_the_preferred_model_is_reused(tmp_path):
    from pitwall.apply.llm import TaskRouter

    cache = tmp_path / "cache"
    client = StubClient({"letter": "cached"})
    r = TaskRouter(clients={"groq": client}, cache_dir=cache)
    r.complete_json("letter_write", "sys", "user")
    calls = len(client.prompts)
    r.complete_json("letter_write", "sys", "user")
    assert len(client.prompts) == calls, "the second call should have hit the cache"


# ── the history ──

def _generation(root, name, *, job_id=930, version="v5", letter="I built a pipeline.",
                needs_review=False):
    import json

    directory = root / name
    directory.mkdir(parents=True)
    (directory / "provenance.json").write_text(json.dumps({
        "generated_at": f"2026-09-25T1{job_id % 10}:00:00+00:00",
        "job": {"id": job_id, "company": "Waymo", "title": "ML Intern",
                "url": "https://example.test/job", "prompt_version": version},
        "fact_check": {"sentences": 8, "supported": 7, "independent": True},
        "coverage": {"overall": 0.62},
    }))
    suffix = "_Cover_Letter_NEEDS_REVIEW.txt" if needs_review else "_Cover_Letter.txt"
    (directory / f"Simon{suffix}").write_text(letter)
    (directory / "Simon_CV.pdf").write_bytes(b"%PDF-1.4 stub")
    return directory


def test_the_history_is_read_off_disk_not_from_session(tmp_path):
    """Reading a letter, going to look at the posting and coming back used to
    lose it. Nothing here lives in session state, so it survives a page switch,
    a closed tab and a restarted server."""
    from pitwall.apply.history import scan

    _generation(tmp_path, "Waymo_A", job_id=930)
    _generation(tmp_path, "Waymo_B", job_id=2922, needs_review=True)
    found = scan(tmp_path)
    assert len(found) == 2
    assert found[0].generated_at >= found[1].generated_at, "newest first"
    assert {g.company for g in found} == {"Waymo"}
    assert any(g.needs_review for g in found)
    assert all(g.url for g in found)


def test_a_directory_without_provenance_is_not_an_application(tmp_path):
    """`output/preview/` holds loose CVs from `apply preview`."""
    from pitwall.apply.history import scan

    _generation(tmp_path, "Real")
    (tmp_path / "preview").mkdir()
    (tmp_path / "preview" / "Simon_CV.pdf").write_bytes(b"%PDF stub")
    assert [g.name for g in scan(tmp_path)] == ["Real"]


def test_a_grade_attaches_to_one_generation_not_to_the_job(tmp_path, monkeypatch):
    """The same job at the same prompt version can be generated more than once —
    three Waymo letters exist at v5. A grade has to say which."""
    import pitwall.apply.history as history_mod
    from pitwall.apply.voice_eval import Rating, append, load

    log = tmp_path / "voice_eval.csv"
    monkeypatch.setattr("pitwall.apply.voice_eval.EVAL_PATH", log)
    monkeypatch.setattr(history_mod, "load", lambda: load(log))

    _generation(tmp_path / "out", "Waymo_v1", job_id=2922)
    _generation(tmp_path / "out", "Waymo_v2", job_id=2922)
    append(Rating(prompt_version="v5", directory="Waymo_v2", job_id=2922,
                  naturalness=4, edit_burden_pct=25), log)

    by_name = {g.name: g for g in history_mod.scan(tmp_path / "out")}
    assert by_name["Waymo_v2"].graded
    assert by_name["Waymo_v2"].rating.naturalness == 4
    assert not by_name["Waymo_v1"].graded, "the grade must not leak to the other run"


def test_an_ungraded_generation_says_so(tmp_path, monkeypatch):
    import pitwall.apply.history as history_mod

    monkeypatch.setattr(history_mod, "load", list)
    _generation(tmp_path, "Fresh")
    assert history_mod.scan(tmp_path)[0].summary == "not graded yet"


# ───────────────── v6: the three claims nobody was checking ─────────────────
#
# Five letters graded by Simon on 25 Sep 2026 said the voice was already there
# (4.54/5 naturalness, 27% rewrite burden, against a 30% ship bar) and that the
# generator was wrong about three facts it had never been given: where the job
# is, when he can start, and how much one experiment establishes. Each of these
# is a regression test for a letter that actually went into `output/`.


ANTHROPIC = {
    "id": 930, "title": "Research Engineer, Code RL", "posting_company": "Anthropic",
    "location": "San Francisco, CA | New York City", "city": "San Francisco",
    "cities": '["San Francisco", "New York"]',
}
PREDIUM = {
    "id": 5499, "title": "Associate Data Scientist", "posting_company": "Predium Technology",
    "location": "Munich", "city": "Munich", "cities": '["Munich"]', "country": "DE",
}
HELSING = {
    "id": 13, "title": "AI Research Intern", "posting_company": "Helsing",
    "location": "Barcelona; Berlin; London; Munich", "city": "Barcelona",
    "cities": '["Barcelona", "Berlin", "London", "Munich"]',
}


def test_posting_locations_reads_every_shape_the_sources_use():
    from pitwall.apply.letter import posting_locations

    assert "San Francisco" in posting_locations(ANTHROPIC)
    assert "New York" in posting_locations(ANTHROPIC)
    # "Barcelona; Berlin; London; Munich" — semicolons, not commas.
    assert set(posting_locations(HELSING)) >= {"Barcelona", "Berlin", "London", "Munich"}
    assert posting_locations({}) == []


@pytest.mark.parametrize(
    ("job", "sentence", "caught"),
    [
        # The four real ones. London for a San Francisco role, Berlin for a
        # Munich one, and two employers used as though they were places.
        (ANTHROPIC, "I would relocate to London for the placement.", True),
        (PREDIUM, "I would relocate to Berlin for the role.", True),
        (HELSING, "I will relocate to Helsing for the internship.", True),
        ({"posting_company": "Waymo", "city": "Mountain View"},
         "I would relocate to the Waymo campus.", True),
        # Naming the right city, and naming none, both have to pass.
        (PREDIUM, "I would relocate to Munich for the role.", False),
        (ANTHROPIC, "I would relocate to New York for the role.", False),
        (PREDIUM, "I would relocate for the role.", False),
        # Nothing known about the location is not a licence to invent one.
        ({}, "I would relocate to Zurich for the role.", True),
        ({}, "I would relocate for the role.", False),
    ],
)
def test_a_destination_the_posting_does_not_name_is_a_violation(job, sentence, caught):
    from pitwall.apply.letter import invented_location_hits

    hits = invented_location_hits(sentence, job)
    assert bool(hits) is caught, (sentence, [h.detail for h in hits])


def test_a_posted_city_written_with_a_thin_space_is_not_an_invented_location():
    """The gate compared raw strings, and gpt-oss-20b does not write raw strings.

    A real letter for job 930 wrote "relocate to San\u202fFrancisco" with a narrow
    no-break space. The posting says "San Francisco, CA", the substring test
    between the two failed, and the gate reported an invented city that the
    posting itself names — then the correction asked for a posted location, the
    writer named San Francisco again, and three attempts died identically with no
    PDF at the end. Whether it bites is luck: an accepted letter from the same run
    carried twenty-four such characters and passed, because none landed in a
    place name.
    """
    from pitwall.apply.letter import invented_location_hits

    assert not invented_location_hits(
        "I am available immediately and would relocate to San\u202fFrancisco.", ANTHROPIC
    )
    assert not invented_location_hits(
        "I would relocate to New\u2011York City for the role.", ANTHROPIC
    )
    # Still a violation when the city is genuinely not in the posting.
    assert invented_location_hits("I would relocate to Z\u00fcrich.", ANTHROPIC)


def test_the_thousands_separator_rule_survives_the_lookalike_flattening():
    """`plain_spaces` is for COMPARING. A thin space between digits is still a
    violation, because the profile records numbers with commas."""
    from pitwall.apply.letter import THIN_SPACE_DIGITS, plain_spaces

    assert THIN_SPACE_DIGITS.search("an equal 63\u202f000-evaluation budget")
    assert plain_spaces("San\u202fFrancisco") == "San Francisco"


def test_an_employer_is_not_somewhere_you_can_move_to():
    from pitwall.apply.letter import invented_location_hits

    (hit,) = invented_location_hits("I will relocate to Helsing for the internship.", HELSING)
    assert "employer, not a place" in hit.detail


def test_his_own_city_is_permitted_without_being_suggested():
    from pitwall.apply.letter import invented_location_hits

    assert not invented_location_hits(
        "I am based in Lisbon and would relocate for the role.",
        PREDIUM, allowed=["Lisbon, Portugal"],
    )
    (hit,) = invented_location_hits("I would relocate to Berlin.", PREDIUM,
                                    allowed=["Lisbon, Portugal"])
    assert "Munich" in hit.detail and "Lisbon" not in hit.detail


@pytest.mark.parametrize(
    ("sentence", "code"),
    [
        # `personal.yaml` says "available immediately, full-time" and records the
        # conflict as resolved: the MSc runs alongside the work.
        ("I am available from July 2027.", "STALE_AVAILABILITY"),
        ("I would be able to start once I finish my MSc.", "STALE_AVAILABILITY"),
        ("After completing my master's degree I would join full-time.", "STALE_AVAILABILITY"),
        # Dating the degree and saying nothing leaves the reader to infer it —
        # which is what every letter before v6 did.
        ("I finish my MSc in July 2027 and would relocate for the role.",
         "UNSTATED_AVAILABILITY"),
        # Stating it is enough, even with the company's name in between.
        ("I finish my MSc in July 2027 and am available to join Wayve immediately.", ""),
        ("I am available immediately, full-time; my MSc in July 2027 runs alongside it.", ""),
        # A fixed-start scheme may legitimately name the month, provided it also
        # says when he is free — `personal.yaml` anticipates exactly this.
        ("I am available immediately and finish my MSc in July 2027, when the "
         "placement starts.", ""),
        ("I would be glad to talk it through.", ""),
    ],
)
def test_availability_is_stated_never_inferred_from_the_msc(profile, sentence, code):
    from pitwall.apply.letter import stale_availability_hits

    codes = [h.code for h in stale_availability_hits(sentence, profile)]
    assert codes == ([code] if code else []), (sentence, codes)


@pytest.mark.parametrize(
    ("sentence", "caught"),
    [
        ("equal budgets reveal which optimiser is truly better", True),
        ("a check most projects skip until failure is costly", True),
        ("a finding that highlights how credit assignment is the core challenge", True),
        ("These findings illustrate that a careful experiment exposes hidden issues", True),
        # The scoped versions the reviews asked for have to survive.
        ("using an equal evaluation budget made the comparison meaningful rather than "
         "rewarding the optimiser that got more evaluations", False),
        ("in this benchmark the ranking tracked how much labelled data each had", False),
        ("under the training budget I had, deep RL did not beat a linear model", False),
        ("I documented two negative results and traced the residual errors", False),
    ],
)
def test_one_experiment_is_not_a_law(sentence, caught):
    from pitwall.apply.letter import overclaim_hits

    assert bool(overclaim_hits(sentence)) is caught, (sentence, overclaim_hits(sentence))


def test_the_writer_is_told_where_the_role_is_and_when_he_is_free(profile):
    """Both were absent until v6, and the model filled the silence with a guess."""
    from pitwall.apply.letter import _user_prompt

    truth = build_truth(profile)
    prompt = _user_prompt(
        profile, NarrativePlan(evidence_order=["ev.pitwall"]),
        [profile.evidence_block("ev.pitwall")], truth, PREDIUM,
    )
    assert "Munich" in prompt
    assert "available immediately" in prompt
    # And a posting with no location says so, rather than saying nothing.
    blank = _user_prompt(
        profile, NarrativePlan(evidence_order=["ev.pitwall"]),
        [profile.evidence_block("ev.pitwall")], truth, {"title": "Engineer"},
    )
    assert "does not say" in blank


def test_the_gate_rejects_the_real_predium_closing(profile):
    """The letter that went out said Berlin. The posting says Munich."""
    truth = build_truth(profile)
    bad = {"letter": "Dear Hiring Manager,\n\nI built a pipeline over 74 years of "
                     "data in 14 tables. I finish my MSc in July 2027 and would "
                     "relocate to Berlin for the role.\n\nSimon Sazonov"}
    good = {"letter": "Dear Hiring Manager,\n\nI built a pipeline over 74 years of "
                      "data in 14 tables. I am available immediately, full-time, and "
                      "would relocate to Munich.\n\nSimon Sazonov"}
    result = write_letter(
        profile, NarrativePlan(evidence_order=["ev.pitwall"]),
        [profile.evidence_block("ev.pitwall")], truth,
        router(groq=StubClient(bad, good)), PREDIUM,
    )
    assert result.accepted
    assert result.attempts == 2
    assert "Munich" in result.text and "Berlin" not in result.text


def test_the_plan_carries_what_draws_him_to_the_work(profile):
    """`company_angle` needs research Simon has not done; this needs only the
    posting, and it is what the closing paragraph was missing."""
    from pitwall.apply.letter import _user_prompt

    plan = NarrativePlan(
        evidence_order=["ev.pitwall"],
        role_attraction="designing environments and the infrastructure that makes the "
                        "experiments trustworthy",
        domain_bridge="the domain differed but the constraint did not",
    )
    prompt = _user_prompt(
        profile, plan, [profile.evidence_block("ev.pitwall")], build_truth(profile), PREDIUM,
    )
    assert "infrastructure that makes the experiments trustworthy" in prompt
    assert "the constraint did not" in prompt


def test_no_gate_ever_fights_the_profiles_own_words(profile):
    """A gate that rejects a fact the writer was TOLD to use cannot be satisfied.

    Learned the hard way on the first live v6 run: "most projects skip" was
    flagged as an overclaim while sitting in `ev.data_engineering.one_line_hook`,
    which `writer_whitelist` hands to the writer as quotable truth. Three
    attempts, three rejections, nothing written — and the same trap was waiting
    in `ev.rl_sepsis.one_line_hook` ("is the whole problem").

    Every phrase the writer may quote is checked here against every gate that
    could reject it. A new pattern that matches Simon's own wording fails this
    test instead of deadlocking a live run.
    """
    from pitwall.apply.letter import (
        STALE_AVAILABILITY,
        overclaim_hits,
        summarising_close_hits,
    )

    quotable: list[tuple[str, str]] = []
    for block in profile.evidence:
        quotable += [(f"{block.id}.facts", fact) for fact in block.facts]
        if block.one_line_hook:
            quotable.append((f"{block.id}.one_line_hook", block.one_line_hook))
        if block.story:
            quotable.append((f"{block.id}.story", block.story))
    for entry in profile.style.exemplars:
        if isinstance(entry, dict) and entry.get("text"):
            quotable.append((f"style.exemplars[{entry.get('id')}]", str(entry["text"])))
    for sample in profile.style.writing_samples:
        if isinstance(sample, dict) and sample.get("text"):
            quotable.append((f"style.writing_samples[{sample.get('id')}]", str(sample["text"])))

    assert len(quotable) > 40, "the profile scan found almost nothing — has loading changed?"

    clashes = [
        f"{where}: {hit.span!r} ({hit.code})"
        for where, text in quotable
        for hit in overclaim_hits(text)
    ] + [
        f"{where}: {match.group(0)!r} (STALE_AVAILABILITY)"
        for where, text in quotable
        for pattern in STALE_AVAILABILITY
        if (match := pattern.search(text))
    ] + [
        f"{where}: {hit.span!r} ({hit.code})"
        for where, text in quotable
        for hit in summarising_close_hits(text)
    ]
    assert not clashes, "a gate rejects wording the profile tells the writer to use:\n  " + \
        "\n  ".join(clashes)


def test_a_draft_survives_the_next_call_failing(profile):
    """A generated draft is on disk before the rewrite is attempted.

    On 25 Sep a v6 draft that cost 5,937 tokens was lost: the deterministic gate
    rejected it, the rewrite hit Groq's daily cap, and the exception propagated
    past the only line that wrote the draft down. It was recoverable only from
    the content-addressed cache.
    """
    truth = build_truth(profile)
    kept: list[tuple[int, int, str]] = []
    bad = {"letter": "I built a seven-stage pipeline, truly better than the baseline."}
    write_letter(
        profile, NarrativePlan(evidence_order=["ev.pitwall"]),
        [profile.evidence_block("ev.pitwall")], truth,
        router(groq=StubClient(bad)), {"title": "Engineer"},
        on_draft=lambda attempt, text: kept.append((0, attempt, text)),
    )
    # Three attempts, three drafts kept — none of them waited for the loop to end.
    assert [a for _, a, _ in kept] == [1, 2, 3]
    assert all("seven-stage" in text for _, _, text in kept)


# ──────────────── parallel shards: what breaks when N run at once ────────────


def test_two_shards_never_claim_the_same_output_directory(tmp_path):
    """`claim=True` must be atomic, because look-then-return is a race.

    Twelve workers asked for the same company and role. Without claiming, three
    pairs shared a directory and the second of each pair wrote over the first.
    An earlier version of the fix still lost one, because it adopted a directory
    that existed but looked empty — the winner simply had not written its first
    file yet.
    """
    import concurrent.futures as cf

    from pitwall.apply.packager import output_dir

    def claim(_: int) -> str:
        directory = output_dir(tmp_path, "Predium Technology GmbH",
                               "Associate Data Scientist", claim=True)
        (directory / "marker.txt").write_text("x")
        return directory.name

    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        names = list(pool.map(claim, range(12)))
    assert len(set(names)) == 12, f"shards collided: {sorted(names)}"


def test_looking_without_claiming_still_works_for_a_single_run(tmp_path):
    """The default stays non-destructive for the one-at-a-time path."""
    from pitwall.apply.packager import output_dir

    first = output_dir(tmp_path, "Acme", "Engineer")
    assert first.name == "Acme_Engineer"
    first.mkdir(parents=True)
    (first / "letter.txt").write_text("sent")
    assert output_dir(tmp_path, "Acme", "Engineer").name == "Acme_Engineer_v2"


def test_the_cache_is_never_read_half_written(tmp_path):
    """Shards share one cache directory, so a write must be atomic.

    `write_text` leaves the file empty for an instant; a reader in that instant
    gets a JSONDecodeError and the entry looks poisoned. Writing to a uniquely
    named temp file and renaming is atomic on POSIX. The temp name must be unique
    per CALL rather than per process — threads share a pid, and a shared name
    made one thread rename the file out from under another.
    """
    import concurrent.futures as cf

    from pitwall.apply.llm import TaskRouter

    router = TaskRouter.__new__(TaskRouter)
    router.cache_dir = tmp_path
    digest = "deadbeef" * 4
    payload = {"provider": "groq", "model": "m", "result": {"letter": "x" * 40_000}}
    problems: list[str] = []

    def hammer(index: int) -> None:
        for _ in range(30):
            if index % 2:
                try:
                    router._store(digest, payload)
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"write: {exc!r}")
            else:
                path = tmp_path / f"{digest}.json"
                if path.exists():
                    try:
                        json.loads(path.read_text())
                    except Exception as exc:  # noqa: BLE001
                        problems.append(f"torn read: {exc!r}")

    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(hammer, range(16)))

    assert not problems, problems[:3]
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")], "temp files left"


# ─────────────────────────────── the batch ───────────────────────────────────


def test_shards_partition_the_jobs_exactly_once():
    """Every job goes to exactly one worker, and the split is reproducible."""
    from pitwall.apply.batch import split

    jobs = [930, 13, 5426, 5499, 2922, 225, 7, 88, 1001, 44]
    parts = [split(jobs, 3, i) for i in range(3)]
    assert sorted(j for part in parts for j in part) == sorted(jobs)
    assert not set(parts[0]) & set(parts[1]) & set(parts[2])
    # Reproducible: the same call gives the same answer, so a rerun of shard 1
    # cannot pick up a job shard 0 already did.
    assert parts == [split(jobs, 3, i) for i in range(3)]
    # Balanced to within one.
    assert max(len(p) for p in parts) - min(len(p) for p in parts) <= 1
    # Duplicates in the input do not duplicate the work.
    assert split([5, 5, 5, 7], 1, 0) == [5, 7]


def test_the_batch_sorts_letters_into_ready_and_named_reasons(profile, tmp_path, monkeypatch):
    """The whole point: Simon reads a table, not a hundred letters."""
    from pitwall.apply import batch as batch_mod
    from pitwall.apply.pipeline import ApplicationResult

    monkeypatch.setattr(batch_mod, "LEDGER_DIR", tmp_path / "ledger")
    truth = build_truth(profile)
    good = (
        "Dear Hiring Manager,\n\n"
        "I built a seven-stage resumable pipeline over SQLite, scoring candidates "
        "with explainable heuristics alongside batched model verdicts that carry "
        "their reasons and their red flags. I hand-labelled a 60-candidate set and "
        "chose an operating threshold that kept recall above 0.90, accepting the "
        "lower precision that came with it, because a missed lead costs more than a "
        "wasted review. Each stage writes its own artefacts, so a run that dies "
        "halfway resumes from where it stopped rather than starting again.\n\n"
        "I wrote 260 hermetic tests at roughly 90 per cent coverage and saved real "
        "search captures as fixtures, so the suite never touches the network and a "
        "failure always means the code changed. I built provider fallback chains for "
        "search and inference, which let an exhausted provider disable itself "
        "mid-run instead of failing the whole pipeline, and I made the cache record "
        "which model answered so a weaker one cannot be served back later.\n\n"
        "On a separate problem I designed crossover to operate at whole-triangle "
        "granularity so that shape and colour stayed co-adapted, then tuned in "
        "stages: one-at-a-time experiments to set defensible ranges, Optuna TPE over "
        "those ranges, and structural ablations afterwards. I validated across 30 "
        "seeds with Mann-Whitney U and Wilcoxon tests against a simulated annealing "
        "baseline given the same evaluation budget, and an independent replication "
        "reproduced the result.\n\n"
        "What draws me to this is the combination of building data pipelines and "
        "validating what comes out of them against real data, which is the part of "
        "my own projects I have spent the most time on. I am available immediately, "
        "full-time, and would relocate to Munich for the position. I would be glad "
        "to talk it through.\n\n"
        "Simon Sazonov"
    )
    bad = good.replace("would relocate to Munich", "would relocate to Berlin")

    def fake_generate(job_id, **kwargs):
        text = good if job_id == 1 else bad
        return ApplicationResult(
            job=PREDIUM, company="Predium Technology", title="Associate Data Scientist",
            truth=truth, analysis=SimpleNamespace(responsibilities=["build data pipelines"]),
            letter=Letter(text=text, words=len(text.split()), accepted=True),
            letter_state="clean",
            directory=tmp_path / f"job{job_id}", prompt_tokens=9000, completion_tokens=500,
        )

    rows = batch_mod.run_batch(
        [1, 2], name="t", out=tmp_path, cv=False, audit=False, generate=fake_generate,
    )
    verdicts = {r.job_id: r.status for r in rows}
    assert verdicts == {1: "ready", 2: "review"}
    (flagged,) = [r for r in rows if r.status == "review"]
    assert any("berlin" in reason.lower() for reason in flagged.reasons), flagged.reasons

    report = batch_mod.format_report(rows)
    assert "ready: 1" in report and "review: 1" in report


def test_the_batch_resumes_and_keeps_going_after_one_job_dies(profile, tmp_path, monkeypatch):
    from pitwall.apply import batch as batch_mod
    from pitwall.apply.pipeline import ApplicationResult

    monkeypatch.setattr(batch_mod, "LEDGER_DIR", tmp_path / "ledger")
    seen: list[int] = []

    def flaky(job_id, **kwargs):
        seen.append(job_id)
        if job_id == 2:
            raise RuntimeError("groq exploded")
        return ApplicationResult(job={}, company="Acme", title="Engineer",
                                 letter=Letter(text="", accepted=True))

    rows = batch_mod.run_batch([1, 2, 3], name="t", out=tmp_path, cv=False,
                               generate=flaky)
    # Job 2 blew up; 3 still ran.
    assert seen == [1, 2, 3]
    assert {r.job_id: r.status for r in rows}[2] == "error"
    assert "groq exploded" in " ".join(rows[1].reasons)

    # A second pass retries only the failure — 1 and 3 are done.
    seen.clear()
    batch_mod.run_batch([1, 2, 3], name="t", out=tmp_path, cv=False, generate=flaky)
    assert seen == [2], "resume should retry the error and skip the finished jobs"


def test_a_template_reused_across_letters_is_caught():
    """The failure that would make a hundred letters worthless."""
    from pitwall.apply.batch import duplicate_sentences

    shared = "I built a seven-stage resumable pipeline that degrades gracefully instead of failing."
    letters = {
        "A": f"Dear Hiring Manager, {shared} And something specific to A happened here.",
        "B": f"Dear Hiring Manager, {shared} And something specific to B happened here.",
        "C": "Dear Hiring Manager, an entirely different opening that shares nothing at all.",
    }
    repeated = duplicate_sentences(letters)
    assert any(shared.rstrip(".") in sentence for sentence in repeated)
    # A short house closing is not a collapse.
    assert not duplicate_sentences({"A": "I would be glad to talk.", "B": "I would be glad to talk."})


# ─────────────────── several keys for one provider ───────────────────────────


def test_the_pool_prefers_the_key_with_the_most_room():
    """Round-robin would send a call to a key with 200 tokens left while
    another had 90,000, and the daily ceiling is what actually stops a batch."""
    from pitwall.apply.keys import KeyPool, fingerprint

    pool = KeyPool(["k_aaa1", "k_bbb2", "k_ccc3"], daily=20_000, minute=8_000,
                   usage_path=None)
    used = []
    for _ in range(6):
        key = pool.choose(6_000)
        assert key is not None
        used.append(fingerprint(key))
        pool.record(key, 6_000)
    # Spread evenly rather than draining one key first.
    assert sorted(set(used)) == ["...aaa1", "...bbb2", "...ccc3"]
    assert max(used.count(f) for f in set(used)) == 2


def test_blank_and_duplicate_keys_do_not_look_like_extra_quota():
    from pitwall.apply.keys import KeyPool

    assert len(KeyPool(["k_a", "", "  ", "k_a", None], usage_path=None)) == 1  # type: ignore[list-item]
    assert len(KeyPool([], usage_path=None)) == 0


def test_a_day_limit_parks_longer_than_a_minute_limit():
    from pitwall.apply.keys import KeyPool

    pool = KeyPool(["k_aaa1", "k_bbb2"], usage_path=None)
    pool.note_refusal("k_aaa1", "quota", 30.0)
    pool.note_refusal("k_bbb2", "rate_limit", 20.0)
    rows = {r["key"]: r for r in pool.status()}
    assert rows["...aaa1"]["parked_for"] > 600, "a daily ceiling is not a 30-second pause"
    assert 10 < rows["...bbb2"]["parked_for"] <= 30
    assert "daily" in rows["...aaa1"]["reason"]


def test_a_refused_key_is_swapped_not_slept_on():
    """The whole reason for several keys.

    Before this, `classify_error` returning "quota" ended the provider outright —
    which with one key was correct and with three meant giving up while two sat
    idle. A per-minute limit was handled by sleeping, so three keys would have
    taken turns waiting instead of working.
    """
    import time

    from pitwall.apply.keys import KeyPool

    class Flaky:
        """Groq's real daily-cap message, twice, then an answer."""

        def __init__(self) -> None:
            self.calls = 0
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs):
            self.calls += 1
            if self.calls <= 2:
                exc = Exception(
                    "Rate limit reached for model `openai/gpt-oss-20b` on tokens per "
                    "day (TPD): Limit 200000, Used 199174, Requested 3721. "
                    "Please try again in 20m50.64s."
                )
                exc.status_code = 429  # type: ignore[attr-defined]
                raise exc
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"ok": 1})))],
                usage=SimpleNamespace(prompt_tokens=5000, completion_tokens=400),
            )

    client, slept = Flaky(), []
    pool = KeyPool(["k_aaa1", "k_bbb2", "k_ccc3"], usage_path=None)
    router = TaskRouter(
        clients={"groq": client}, pools={"groq": pool},
        on_wait=lambda *a: slept.append(a),
    )
    started = time.monotonic()
    assert router.complete_json("job_analysis", "sys", "user") == {"ok": 1}
    assert time.monotonic() - started < 1.0, "it waited instead of switching key"
    assert not slept, "no sleep should happen while another key is free"
    assert client.calls == 3
    # And the trace says which key answered, so a letter is traceable to one.
    assert [c.key for c in router.calls] == ["...aaa1", "...bbb2", "...ccc3"]
    assert router.calls[-1].outcome == "ok"


def test_one_key_behaves_exactly_as_before(profile):
    """A machine with a single key must see no change at all."""
    from pitwall.apply.llm import TaskRouter as Router

    router = Router(clients={"groq": StubClient({"ok": 1})})
    assert router.pool_for("groq") is None
    assert router.complete_json("job_analysis", "s", "u") == {"ok": 1}
    assert router.calls[0].key == ""


def test_provider_keys_reads_the_extra_env_vars(monkeypatch):
    """However many keys are configured, in order, stopping at the first gap.

    `_env_file=None` rather than `reload_settings()`, because the developer's own
    `.env` holds five keys and the point of this test is the discovery rule, not
    how many keys this machine happens to have. Environment variables still win
    over the file, so the monkeypatched values are what is read.
    """
    from pitwall.config import Settings

    for number in range(1, 9):
        monkeypatch.delenv(f"GROQ_API_KEY_{number}", raising=False)
    for name, value in (("GROQ_API_KEY", "gsk_one"), ("GROQ_API_KEY_2", "gsk_two"),
                        ("GROQ_API_KEY_3", "gsk_three")):
        monkeypatch.setenv(name, value)
    assert Settings(_env_file=None).provider_keys("groq") == [
        "gsk_one", "gsk_two", "gsk_three",
    ]

    # A fourth and fifth are picked up with no code change — this is the part
    # that was broken: `.env` held five keys and only three were ever read.
    monkeypatch.setenv("GROQ_API_KEY_4", "gsk_four")
    monkeypatch.setenv("GROQ_API_KEY_5", "gsk_five")
    assert Settings(_env_file=None).provider_keys("groq") == [
        "gsk_one", "gsk_two", "gsk_three", "gsk_four", "gsk_five",
    ]

    # A gap stops the walk, so a stray GROQ_API_KEY_9 cannot quietly join the
    # pool and a blank one cannot produce an empty entry.
    monkeypatch.delenv("GROQ_API_KEY_4")
    monkeypatch.setenv("GROQ_API_KEY_9", "gsk_nine")
    assert Settings(_env_file=None).provider_keys("groq") == [
        "gsk_one", "gsk_two", "gsk_three",
    ]


def test_a_company_entry_matches_the_posting_even_with_a_legal_suffix():
    """Work only Simon can do must not be silently wasted.

    The first version compared raw lowercased strings, so an entry called
    "Predium" would never have matched a posting that says "Predium Technology
    GmbH" — the entry would sit in the file looking correct and contribute
    nothing, and the letter would keep its generic closing with no sign why.
    """
    from pitwall.apply.planner import _company_notes

    profile = SimpleNamespace(companies=[
        {"id": "co.predium", "name": "Predium", "notes": "They model buildings.",
         "angle": "Same shape as the pipeline work."},
        {"id": "co.meta", "name": "Meta", "aliases": ["Facebook"], "notes": "Big."},
    ])
    for posting in ("Predium Technology GmbH", "PREDIUM", "predium", "Predium GmbH"):
        assert "They model buildings." in _company_notes(profile, posting), posting
    # An alias covers what normalising cannot.
    assert _company_notes(profile, "Facebook") == "Big."
    # And an employer with no entry still returns nothing rather than guessing.
    assert _company_notes(profile, "Some Other Company") == ""
    assert _company_notes(profile, "") == ""


def test_a_malformed_companies_entry_does_not_break_a_run():
    """The file is hand-written, so it will be wrong at some point."""
    from pitwall.apply.planner import _company_notes

    profile = SimpleNamespace(companies=["not a mapping", None, 42,
                                         {"name": "Predium", "notes": "ok"}])
    assert _company_notes(profile, "Predium Technology GmbH") == "ok"



def test_the_closing_may_invite_but_may_not_enumerate():
    """All ten letters generated before v6 ended by listing his own qualities.

    "...discuss how my rigorous, leak-free evaluation and reproducible pipelines
    fit the simulator evaluation work at Waymo" — a précis of the three
    paragraphs above it, in every single letter.

    Simon asked first for that long version to go, then for a plain professional
    close to stay. Both, because what separates them is the ENUMERATION: one
    generic noun reads as courtesy, a list reads as padding. Getting this
    backwards in either direction is a regression, so both halves are asserted.
    """
    from pitwall.apply.letter import summarising_close_hits

    # Lists his qualities — the failure.
    for closing in (
        "I would welcome the opportunity to discuss how my experience in "
        "data-pipeline design and optimisation rigour can support your team.",
        "I would be glad to discuss how my rigorous, leak-free evaluation and "
        "reproducible pipelines fit the simulator evaluation work at Waymo.",
        "I would be glad to discuss how my work on sparse-reward environments, "
        "resilient pipelines and rigorous optimisation can support your research.",
        "I would welcome the chance to discuss how my rigorous experimentation "
        "and end-to-end pipeline work align with their needs.",
        "What you gain is a researcher who tests his own results.",
    ):
        assert summarising_close_hits(closing), closing

    # Names one thing, or nothing — both are ordinary professional closes, and
    # Simon asked for the second of these back by name.
    for closing in (
        "I would be glad to talk it through.",
        "I would be glad to discuss the role.",
        "I would welcome a conversation.",
        "I would welcome the opportunity to discuss how my experience could "
        "contribute to Predium's data-science team.",
        "I would be glad to discuss how my work can contribute to your team.",
    ):
        assert not summarising_close_hits(closing), closing


def test_an_unsigned_letter_can_never_ship(profile):
    """The batch report and the generation gate must not disagree.

    They did: `quality_checks` reported "not signed" while `_gate` said the
    letter was fine, so an unsigned letter was written to `output/` and only
    flagged afterwards by a report that runs too late to stop it. Both now call
    `shape_hits`.
    """
    from pitwall.apply.batch import quality_checks
    from pitwall.apply.letter import _gate, shape_hits

    truth = build_truth(profile)
    unsigned = (
        "Dear Hiring Manager,\n\n"
        "I built a seven-stage resumable pipeline over SQLite.\n\n"
        "I would be glad to talk it through."
    )
    assert any(h.code == "NOT_SIGNED" for h in shape_hits(unsigned, profile))
    violations, _ = _gate(unsigned, profile, truth, {})
    assert any(v.code == "NOT_SIGNED" for v in violations), "the gate let it through"
    assert any("not_signed" in r.lower()
               for r in quality_checks(unsigned, job={}, profile=profile, truth=truth))


def test_a_spaced_thousands_group_is_rejected(profile):
    """"1 000" and "63 000" — a narrow no-break space where the facts use a comma."""
    from pitwall.apply.letter import shape_hits

    spaced = "Dear Hiring Manager,\n\nI ran a 1 000-dimensional search.\n\nSimon Sazonov"
    (hit,) = [h for h in shape_hits(spaced, profile) if h.code == "NUMBER_FORM"]
    assert "63,000" in hit.detail
    comma = "Dear Hiring Manager,\n\nI ran a 1,000-dimensional search.\n\nSimon Sazonov"
    assert not shape_hits(comma, profile)


def test_corrections_accumulate_across_attempts(profile):
    """Attempt three must still know what attempt one was told.

    Live, three attempts produced three different violations in turn — an
    overclaim, then a listing closing, then a missing signature — because the
    correction was rebuilt from scratch each round. The model fixed what it was
    shown and reintroduced what it had been shown before, burning the whole
    retry budget without converging.
    """
    truth = build_truth(profile)
    head = "Dear Hiring Manager,\n\n"
    drafts = [
        # Each one fixes the last complaint and introduces a fresh one.
        {"letter": head + "I built a pipeline, proving that equal budgets settle "
                          "it.\n\nSimon Sazonov"},
        {"letter": head + "I would discuss how my rigorous evaluation and reproducible "
                          "pipelines can support you.\n\nSimon Sazonov"},
        {"letter": head + "I built a seven-stage resumable pipeline over SQLite."},
    ]
    client = StubClient(*drafts)
    write_letter(
        profile, NarrativePlan(evidence_order=["ev.pitwall"]),
        [profile.evidence_block("ev.pitwall")], truth,
        router(groq=client), {"title": "Engineer"},
    )
    last_prompt = client.prompts[-1]
    assert "proving that" in last_prompt, "the first correction was forgotten"
    assert "can support" in last_prompt, "the second correction was forgotten"
    assert last_prompt.count("you wrote") >= 2, last_prompt[-600:]


# ─────────────── severity: what blocks, and what merely marks ───────────────


def test_an_unknown_violation_code_is_treated_as_a_claim():
    """The most important line in the taxonomy. A gate added in six months must
    block until a human has decided which pile it belongs in."""
    from pitwall.apply.truth import severity

    assert severity("SOMETHING_NOBODY_CLASSIFIED") == "claim"
    assert severity("NUMBER_FORM") == "style"
    assert severity("FORBIDDEN_TOPIC") == "policy"


def test_a_thin_space_alone_does_not_stop_a_letter_shipping(profile):
    """One narrow no-break space used to fail the same test as a fabricated
    metric, and about half of all runs ended with nothing on disk."""
    from pitwall.apply.letter import Letter, repair_mechanics
    from pitwall.apply.truth import Violation

    draft = "Dear Hiring Manager,\n\nI ran 63 000 evaluations.\n\nSimon Sazonov"
    violations = [Violation("NUMBER_FORM", "use a comma", "3 000")]
    assert Letter(text=draft, violations=violations).shippable

    fixed, repairs = repair_mechanics(draft, profile, violations, language="en")
    assert "63,000" in fixed
    assert [r.code for r in repairs] == ["NUMBER_FORM"]


def test_the_shipped_letter_carries_no_lookalike_hyphens_or_spaces(profile):
    """The letter is a .txt that gets pasted into an ATS box.

    Every letter generated before 28 Sep 2026 carried 16-33 U+2011 non-breaking
    hyphens, none of which appear anywhere in profile/ or the prompts. A keyword
    search for "macro-F1" does not match "macro\u2011F1", so the posting's own
    vocabulary failed to match a letter written to contain it.
    """
    from pitwall.apply.letter import ship_safe
    from pitwall.apply.truth import Violation

    out = ship_safe(
        "Dear Hiring Manager,\n\n"
        "I used macro\u2011F1 on a bronze\u2011silver\u2011gold pipeline and "
        "would relocate to San\u202fFrancisco. Simon\u2019s 2018\u20132026 data "
        "\u2014 all of it \u2014 ran 63\u202f000 evaluations.\n\nSimon Sazonov"
    )
    assert "\u2011" not in out and "macro-F1" in out
    assert "San Francisco" in out           # a place name must match the posting
    assert "Simon's" in out                 # curly apostrophe flattened
    assert "2018\u20132026" in out and "\u2014 all of it \u2014" in out  # real punctuation kept
    assert out.count("\n\n") == 2          # paragraphs survive

    # The one thin space with an owner is left for NUMBER_FORM to turn into a
    # comma, rather than being quietly downgraded to a plain space here.
    assert "63\u202f000" in out
    fixed, repairs = repair_mechanics(
        out, profile, [Violation("NUMBER_FORM", "use a comma", "3\u202f000")], language="en"
    )
    assert "63,000" in fixed and [r.code for r in repairs] == ["NUMBER_FORM"]


def test_a_fabricated_number_is_never_shippable(profile):
    from pitwall.apply.letter import Letter
    from pitwall.apply.truth import Violation

    letter = Letter(
        text="I cut latency by 47%.",
        violations=[Violation("UNSUPPORTED_NUMBER", "no profile record", "47")],
    )
    assert not letter.shippable
    assert [v.code for v in letter.blocking] == ["UNSUPPORTED_NUMBER"]


def test_an_incidental_year_from_the_posting_is_not_a_fabricated_number(profile):
    """A metric and a date look identical to this gate, and treating "the 2027
    cohort" as a lie cost letters that were true. A metric is never a bare
    four-digit integer, so the carve-out cannot launder one."""
    from pitwall.apply.truth import build_truth

    truth = build_truth(profile, job={"title": "Intern", "description": "the 2029 cohort"})
    codes = {v.code for v in truth.unsupported_numbers("I would join the 2029 cohort.")}
    assert codes == {"UNSUPPORTED_YEAR"}
    # A year the posting never mentions still blocks.
    assert {v.code for v in truth.unsupported_numbers("I would join in 2031.")} == {
        "UNSUPPORTED_NUMBER"
    }


def test_a_german_greeting_is_not_a_missing_greeting(profile):
    from pitwall.apply.letter import shape_hits

    text = "Sehr geehrte Damen und Herren,\n\nIch habe gebaut.\n\nSimon Sazonov"
    assert not [v for v in shape_hits(text, profile) if v.code == "NO_GREETING"]


def test_the_thousands_separator_is_only_rewritten_in_english(profile):
    """In Ukrainian typography the thin space IS the separator and the comma is
    the decimal mark, so rewriting would turn "1 000" into "one point zero"."""
    from pitwall.apply.letter import repair_mechanics
    from pitwall.apply.truth import Violation

    draft = "I ran 63 000 evaluations."
    violations = [Violation("NUMBER_FORM", "use a comma", "3 000")]
    assert repair_mechanics(draft, profile, violations, language="uk") == (draft, [])


# ─────────────── the "own" loop, and the correction budget ───────────────


def test_a_gated_verb_correction_names_a_verb_the_writer_may_use(profile):
    """The smoking gun behind a real three-attempt failure. The detail used to
    interpolate the YAML lemma, so a letter that wrote "own" was told "nothing in
    the profile says 'owned'" — a word it never used, with no alternative."""
    from pitwall.apply.truth import build_truth

    truth = build_truth(profile)
    (hit,) = [v for v in truth.gated_verb_hits("I would design and own the full pipeline.")
              if v.code == "UNSUPPORTED_SENIORITY_VERB"]
    assert "owned" not in hit.detail, "the correction still quotes the dictionary form"
    assert "own" in hit.span
    assert any(verb in hit.detail for verb in ("built", "designed", "implemented"))


def test_the_writer_prompt_offers_the_verbs_it_is_allowed_to_use(profile):
    """`preferred_verbs` was computed on every run and read by nothing."""
    from pitwall.apply.letter import _user_prompt
    from pitwall.apply.truth import build_truth

    truth = build_truth(profile)
    prompt = _user_prompt(
        profile, NarrativePlan(evidence_order=["ev.pitwall"], target_words=300),
        [profile.evidence_block("ev.pitwall")], truth, {"title": "Engineer"},
    )
    assert "USE THESE INSTEAD" in prompt
    assert "built" in prompt and "designed" in prompt


# ─────────────── the double analyze ───────────────


def test_the_repair_stage_never_runs_on_a_clean_letter(profile):
    truth = build_truth(profile)
    plan = NarrativePlan(evidence_order=["ev.pitwall"], target_words=300)
    good = {"letter": "Dear Hiring Manager,\n\nI built a seven-stage resumable pipeline "
                      "over SQLite.\n\nSimon Sazonov"}
    client = StubClient(good)
    from pitwall.apply.letter import draft_and_check

    letter, _ = draft_and_check(
        profile, plan, [profile.evidence_block("ev.pitwall")], truth,
        router(groq=client), {"title": "Engineer"}, audit=False,
    )
    assert letter.accepted
    assert letter.repairs == []


def test_the_model_cannot_dismiss_a_violation(profile):
    """The reply shape has no field for disagreement, and a reply that keeps the
    offending words is discarded with the original restored."""
    from pitwall.apply.letter import REPAIR_SCHEMA, repair_violations

    assert set(REPAIR_SCHEMA["properties"]) == {"sentence"}, "a channel to argue was added"
    assert REPAIR_SCHEMA["additionalProperties"] is False

    truth = build_truth(profile, missing_requirements=["AWS"])
    text = "Dear Hiring Manager,\n\nI deployed the models on AWS at scale.\n\nSimon Sazonov"
    # Returns the sentence unchanged, plus an invented key claiming the rule is wrong.
    client = StubClient({"sentence": "I deployed the models on AWS at scale.",
                         "verdict": "THE RULE IS WRONG"})
    out, repairs, notes = repair_violations(
        text, profile, truth, router(groq=client), {"title": "Engineer"},
    )
    assert out == text, "the letter was changed by a reply that fixed nothing"
    assert repairs == []
    assert any("still in it" in n for n in notes)


def test_a_repaired_sentence_is_spliced_without_disturbing_the_paragraphs(profile):
    from pitwall.apply.letter import repair_violations

    truth = build_truth(profile, missing_requirements=["AWS"])
    text = ("Dear Hiring Manager,\n\nI built a resumable pipeline over SQLite.\n\n"
            "I deployed the models on AWS at scale.\n\nSimon Sazonov")
    client = StubClient({"sentence": "I deployed the models at scale."})
    out, repairs, _ = repair_violations(
        text, profile, truth, router(groq=client), {"title": "Engineer"},
    )
    assert repairs and repairs[0].tier == "surgery"
    assert len(out.split("\n\n")) == len(text.split("\n\n")), "the paragraph shape moved"
    assert "I built a resumable pipeline over SQLite." in out, "an untouched sentence changed"
    assert "AWS" not in out


def test_only_the_offending_sentence_is_sent_to_the_repair_model(profile):
    from pitwall.apply.letter import repair_violations

    truth = build_truth(profile, missing_requirements=["AWS"])
    text = ("Dear Hiring Manager,\n\nI built a resumable pipeline over SQLite.\n\n"
            "I deployed the models on AWS at scale.\n\nSimon Sazonov")
    client = StubClient({"sentence": "I deployed the models at scale."})
    repair_violations(text, profile, truth, router(groq=client), {"title": "Engineer"})
    (prompt,) = client.prompts
    assert "AWS" in prompt
    assert "resumable pipeline over SQLite" not in prompt, "another paragraph was sent"
    assert "Dear Hiring Manager" not in prompt


def test_the_repair_stage_is_capped(profile):
    from pitwall.apply.letter import MAX_REPAIRS, repair_violations

    truth = build_truth(profile, missing_requirements=["AWS", "Kubernetes", "Terraform"])
    text = ("Dear Hiring Manager,\n\nI used AWS here.\n\nI used Kubernetes here.\n\n"
            "I used Terraform here.\n\nI used AWS again here.\n\nSimon Sazonov")
    client = StubClient({"sentence": "I worked here."})
    repair_violations(text, profile, truth, router(groq=client), {"title": "Engineer"})
    assert len(client.prompts) <= MAX_REPAIRS, "the cap was exceeded"
