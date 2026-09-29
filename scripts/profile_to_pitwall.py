#!/usr/bin/env python
"""Feed `profile/` into the two files PitWall's job search actually reads.

    python3 scripts/profile_to_pitwall.py            # review only
    python3 scripts/profile_to_pitwall.py --apply    # write both files

`profile/` is the single source of truth for Simon's CV and cover-letter data:
tagged bullets, honest confidence levels, evidence pointers, metrics with a
stated method. PitWall's job search wants the same facts in two other shapes:

    config/my_skills.yaml   §18-19 — skill inventory with proficiency + evidence,
                            mapped onto config/skills_vocab.yaml. Without it the
                            loader falls back to my_skills.example.yaml, where
                            every entry sits at a flat proficiency 2 and almost
                            nothing carries evidence.

    config/my_profile.md    §2.6 — prose CV, scored against each open posting for
                            a 0-100 fit. config/profile.md is still the shipped
                            template, so nothing is scoring today.

Both outputs are gitignored, so this writes personal files and touches no
repository content. Re-run it whenever profile/ changes; nothing is hand-edited
downstream, which is the point.

Two rules enforced here rather than left to whoever edits the output:

  * Work authorisation is never written. profile/forbidden.yaml carries a
    never_mention block for it, and config/profile.md's Constraints section
    invites exactly that - so the Constraints section is emitted without it.
  * confidence -> proficiency maps high=3, medium=2, low=1. Skills at
    confidence `none` are emitted only where profile/skills.yaml records an
    explicit user_override, and then at proficiency 1 with no evidence.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROFILE = ROOT / "profile"
CONFIG = ROOT / "config"

PROFICIENCY = {"high": 3, "medium": 2, "low": 1}

#: profile/skills.yaml is finer-grained than config/skills_vocab.yaml (129 records
#: against 83 concepts), so some skills need to be told where they land. This map is
#: explicit on purpose: fuzzy matching here would invent claims. "Experimental Design"
#: is NOT A/B Testing and "Azure OpenAI" is NOT Azure — both are in the gap map, and a
#: near-miss would quietly undo that.
#:
#: Anything not listed and not directly matched is dropped, which is the safe default:
#: a skill absent from the inventory reads as "missing" on a posting that asks for it.
EXTRA_MAP = {
    "skill.monte_carlo": "monte_carlo",
    "skill.time_series": "time_series",
    "skill.etl": "data_pipelines",
    "skill.data_quality": "data_pipelines",
    "skill.dimensional_modelling": "data_pipelines",
    "skill.er_modelling": "data_pipelines",
    "skill.parquet": "data_pipelines",
    "skill.simulated_annealing": "optimization",
    "skill.hyperparameter_tuning": "optimization",
    "skill.multi_objective": "optimization",
    "skill.optuna": "optimization",
    "skill.decision_making": "sequential_decisions",
    "skill.walk_forward": "model_evaluation",
    "skill.class_imbalance": "model_evaluation",
    "skill.threshold_tuning": "calibration",
    "skill.drift_monitoring": "mlops",
    "skill.kedro": "airflow",       # the vocab's orchestration concept — see CONCEPT_NOTES
    "skill.big_data": "spark",
    "skill.sparksql": "spark",
    "skill.mllib": "spark",
    "skill.system_design": "software_engineering",
    "skill.sqlserver": "sql",
    "skill.tsql": "sql",
    "skill.gymnasium": "reinforcement_learning",
    "skill.sb3": "reinforcement_learning",
    "skill.nltk": "nlp",
    "skill.association_rules": "data_mining",
    "skill.llm_agents": "llm",
    "skill.azure_openai": "llm",     # the inference API — NEVER cloud Azure
    "skill.ollama": "llm",
    "skill.groq": "llm",
    "skill.bi": "bi_tools",
    "skill.dax": "bi_tools",
    "skill.powerquery": "bi_tools",
    "skill.powerbi": "bi_tools",
    "skill.matplotlib": "data_visualization",
    "skill.github_actions": "ci_cd",
    "skill.git": "git",
}

#: config/skills_vocab.yaml groups Kedro under `airflow` ("Airflow / orchestration"),
#: because it is a concept entry, not a product one. profile/forbidden.yaml forbids
#: claiming Airflow. Both are right: Simon has real orchestration experience via Kedro
#: and none via Airflow. The mapping is kept so postings saying "orchestration" match,
#: and the emitted line carries a note so nothing downstream reads it as Airflow.
CONCEPT_NOTES = {
    "airflow": "via Kedro — NOT Airflow, which is in the gap map",
}

#: Any other vocabulary label that would amount to claiming something in the gap map.
#: Emitting one is a bug, so it stops the run rather than printing a warning.
GAP_LABELS = {
    "AWS", "Azure", "Google Cloud", "Kubernetes", "Databricks", "Snowflake", "dbt",
    "Kafka / streaming", "Scala", "Java", "C++", "A/B Testing", "Causal Inference",
    "Bayesian Methods",
}

#: Dropped, but a posting might genuinely name them. Surfaced as a suggestion rather
#: than silently mapped onto something adjacent.
WORTH_ADDING_TO_VOCAB = {
    "MongoDB", "Query Optimisation", "Model Explainability", "Transformers",
    "Experimental Design", "Clustering", "Entity Resolution", "Simulation",
}


def load(name: str):
    return yaml.safe_load((PROFILE / name).read_text(encoding="utf-8"))


def readable_evidence(profile_ids, projects, experience) -> list[str]:
    """Turn bullet ids into the short names a human would recognise."""
    names: dict[str, str] = {}
    for p in projects:
        short = p["name"].split("–")[0].split("(")[0].strip()
        for b in p.get("bullets") or []:
            names[b["id"]] = short
    for e in experience:
        for b in e.get("bullets") or []:
            names[b["id"]] = e["org"]
    out: list[str] = []
    for i in profile_ids:
        n = names.get(i) or ("MSc coursework" if i.startswith("edu.") else None)
        if n and n not in out:
            out.append(n)
    return out


def build_skills() -> tuple[str, list[str]]:
    vocab = yaml.safe_load((CONFIG / "skills_vocab.yaml").read_text(encoding="utf-8"))
    skills = load("skills.yaml")["skills"]
    projects, experience = load("projects.yaml"), load("experience.yaml")

    # Every spelling the vocabulary knows -> its canonical id.
    spelling_to_id: dict[str, str] = {}
    for vid, meta in vocab.items():
        for s in [meta["label"], *(meta.get("synonyms") or [])]:
            spelling_to_id.setdefault(s.lower(), vid)

    best: dict[str, dict] = {}
    unmatched: list[str] = []
    for s in skills:
        conf = s.get("confidence")
        override = bool(s.get("user_override"))
        if conf == "none" and not override:
            continue
        candidates = [s["name"], *(s.get("aliases") or [])]
        vid = EXTRA_MAP.get(s["id"]) or next(
            (spelling_to_id[c.lower()] for c in candidates
             if c.lower() in spelling_to_id), None)
        if vid is not None and vid not in vocab:
            raise SystemExit(f"EXTRA_MAP sends {s['id']} to '{vid}', "
                             f"which is not in skills_vocab.yaml")
        if vid is None:
            unmatched.append(s["name"])
            continue
        prof = 1 if (conf == "none" or override) else PROFICIENCY.get(conf, 1)
        ev = [] if (conf == "none" or override) else readable_evidence(
            s.get("evidence") or [], projects, experience)
        prior = best.get(vid)
        if prior is None or prof > prior["proficiency"]:
            entry = {"skill": vocab[vid]["label"], "proficiency": prof}
            if s.get("years"):
                entry["years"] = s["years"]
            if ev:
                entry["evidence"] = ev
            if override:
                entry["_note"] = f"no evidence — {s['user_override']}"
            if vid in CONCEPT_NOTES:
                entry["_note"] = CONCEPT_NOTES[vid]
            best[vid] = entry

    leaked = {e["skill"] for e in best.values()} & GAP_LABELS
    if leaked:
        raise SystemExit(
            "refusing to write: these are in profile/skills.yaml's gap map "
            f"(confidence none, no evidence) but reached the inventory: {sorted(leaked)}")

    by_cat: dict[str, list] = {}
    for vid, entry in best.items():
        by_cat.setdefault(vocab[vid].get("category", "other"), []).append(entry)

    lines = [
        "# Simon's skill inventory — GENERATED from profile/skills.yaml.",
        "#",
        "#   python3 scripts/profile_to_pitwall.py --apply",
        "#",
        "# Do not hand-edit: edit profile/skills.yaml and regenerate, or the CV and the",
        "# job scoring drift apart. proficiency is derived from `confidence` there,",
        "# where high=3 (2+ pieces of work, interviewable for 30 minutes), medium=2",
        "# (one real piece of work), low=1 (coursework or brief exposure).",
        "#",
        "# `evidence` is what a CV line could point to. A skill without it is one the CV",
        "# tailoring will not put forward, which is the behaviour you want.",
        "",
        "skills:",
    ]
    for cat in sorted(by_cat):
        lines.append(f"  # {cat}")
        for e in sorted(by_cat[cat], key=lambda x: (-x["proficiency"], x["skill"])):
            note = e.pop("_note", None)
            body = ", ".join(
                f"{k}: {yaml.safe_dump(v, default_flow_style=True).strip().removesuffix('...').strip()}"
                for k, v in e.items())
            lines.append(f"  - {{{body}}}" + (f"   # {note}" if note else ""))
        lines.append("")
    return "\n".join(lines), unmatched


MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def pretty_date(v) -> str:
    """YAML parses 2025-09 as a string but 2025-09-01 as a date; handle both."""
    t = str(v)
    parts = t.split("-")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{MONTHS[int(parts[1])]} {parts[0]}"
    return t


def build_profile_md() -> str:
    personal, edu = load("personal.yaml"), load("education.yaml")
    exp, projects, skills = load("experience.yaml"), load("projects.yaml"), load("skills.yaml")["skills"]
    thesis = edu[0]["degrees"][0]["thesis"]

    def row(r):
        return ", ".join(s["name"] for s in skills
                         if s.get("row") == r and s.get("printable")
                         and s.get("confidence") in {"high", "medium"})

    top = [p for p in projects if p.get("strength") == "strong"]
    proj_lines = []
    for p in top:
        proj_lines.append(f"**{p['name'].split('–')[0].strip()}** — "
                          + " ".join(b["text"].rstrip(".") + "." for b in p["bullets"]))

    return f"""# Target role

Graduate or junior applied data scientist / machine-learning engineer, and
research-engineering roles of the same level. The work I am aiming at is
decision-making under uncertainty: simulation, risk modelling, reinforcement
learning, and models whose output feeds a decision rather than a report. Open to
quantitative research and data engineering where that overlap exists. Not
looking for senior, lead or management level.

# Background

Part-way through an MSc in Data Science and Advanced Analytics at NOVA IMS in
Lisbon ({pretty_date(edu[0]['degrees'][0]['start'])} to {pretty_date(edu[0]['degrees'][0]['end'])}), after a BSc in Information Systems and
Technologies at the same school and an Erasmus exchange at Milano-Bicocca.

Currently a contract machine-learning engineer at Ophis Research (from
{pretty_date(exp[0]['start'])}), in a four-person team on a quantitative trading system. Built a
swing-point classifier on four years of 15-minute ETH bars, and a Monte Carlo
pre-trade risk engine that simulates thousands of price paths per trade and
returns an accept, reduce or reject decision.

Earlier: a technical program associate role at FreedomCode working to the CTO
and CIO on delivery process, and mobile development on BeeSurfing.

MSc thesis, currently at proposal stage and not yet supervised: {thesis['title']} —
models F1 safety-car deployment as a discrete-time hierarchical hazard fitted to
race-control messages across 2018-2026, coupled to a pit-stop agent optimised for
the bad tail rather than the expected case.

# Selected work

{chr(10).join('- ' + line for line in proj_lines)}

# Skills

Languages: {row('row.programming')}

Methods: {row('row.methods')}

Tools: {row('row.tools')}

# Constraints

Based in Lisbon, Portugal. {personal['notice_period'].capitalize()}. Willing to
relocate; targets include {', '.join(personal['relocation_targets'][:4])}.

Languages spoken: {', '.join(f"{lang['name']} ({lang['level']})" for lang in personal['languages'])}.

<!-- Work authorisation is deliberately not stated here, per
     profile/forbidden.yaml `never_mention`. The template invites it; do not add it.
     A constraint that is not stated is one the model is told not to invent. -->
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the files")
    args = ap.parse_args()

    skills_yaml, unmatched = build_skills()
    profile_md = build_profile_md()
    n_skills = skills_yaml.count("\n  - {")

    targets = [(CONFIG / "my_skills.yaml", skills_yaml),
               (CONFIG / "my_profile.md", profile_md)]

    print(f"my_skills.yaml   {n_skills} skills mapped onto the vocabulary")
    if unmatched:
        notable = sorted(set(unmatched) & WORTH_ADDING_TO_VOCAB)
        rest = sorted(set(unmatched) - WORTH_ADDING_TO_VOCAB)
        print(f"  {len(unmatched)} dropped — not in config/skills_vocab.yaml.")
        if notable:
            print("  Worth adding to the vocabulary, a posting may name these:")
            print("   ", ", ".join(notable))
        if rest:
            print(f"  Correctly dropped ({len(rest)}), too specific or not a job-posting skill:")
            print("   ", ", ".join(rest))
    print(f"my_profile.md    {len(profile_md.splitlines())} lines, "
          f"{len(profile_md.split())} words")

    if not args.apply:
        print("\nReview only. Re-run with --apply to write.")
        return 0
    for path, body in targets:
        existed = path.exists()
        path.write_text(body, encoding="utf-8")
        print(f"{'overwrote' if existed else 'wrote'}  {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
