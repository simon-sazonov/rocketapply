"""How it works — where the jobs come from, how they are scored, how an
application is made, and how good the output has been.

Everything on this page is read from the running system — the sources table,
the scoring weights in your settings, the model routing the engine actually
uses, the applications on disk — not written down once and left to drift.
"""

from __future__ import annotations

import json
import pathlib
import statistics

import pandas as pd
import streamlit as st

from pitwall.apply.budget import MODE_LABELS, _samples, key_room
from pitwall.apply.events import STAGE_TASKS
from pitwall.apply.llm import CAPABLE_ONLY, TASK_MODELS, TASK_ROUTING
from pitwall.apply.pipeline import STAGES
from rocket import components, data
from rocket import format as fmt

COMPONENT_HELP = {
    "role": "How closely the title and duties match the kinds of role you accept.",
    "experience": "Years or level asked for, against your limit. Unknown sits in the middle.",
    "skills": "Skills the posting names that your profile has.",
    "location": "In a country you want; a preferred city scores higher.",
    "visa": "Whether you can work there: your authorisation, the posting's own words, "
            "government sponsor registers.",
    "career": "Matches the directions you want to grow in.",
    "company": "On your target list, and its priority.",
    "fit": "How close the whole posting reads to your CV (text similarity, as a percentile).",
}
STAGE_HELP = {
    "analyse": "Reads the posting: required and preferred skills, each kept only if its quote "
               "really appears in the text.",
    "match": "Matches those skills against your profile and taxonomy; computes coverage.",
    "truth": "Builds the do-not-claim list: gaps, numbers you may use, work that is not yours.",
    "select": "Chooses projects and skill rows for the CV with a solver that respects the "
              "one-page frame.",
    "plan": "Plans the letter: the theme, the evidence order, the angle on the company "
            "(from its own pages, verified).",
    "render": "Draws the CV (PDF and DOCX, checked to hold the same content).",
    "write": "Writes the letter, runs every gate, repairs what it can, and (optionally) has "
             "a second model check each sentence.",
    "package": "Files the letter by its verdict, types it on the CV's letterhead, writes the "
               "review note and the provenance record.",
}
GATES = (
    ("Numbers", "Every number must be one your profile records."),
    ("Forbidden claims", "Skills the posting wants and you lack cannot be claimed."),
    ("Not my work", "Team results cannot be written as yours alone."),
    ("Seniority verbs", "“Led”, “owned”, “founded” only where the profile says so."),
    ("Contamination", "No sentence copied from another letter or an exemplar."),
    ("Topics", "Subjects the profile rules out stay out."),
    ("Banned phrases", "Your list of phrases that make a letter sound generic."),
    ("Places & dates", "No invented relocation or availability."),
)


def _model_for(task: str) -> str:
    providers = TASK_ROUTING.get(task, ("groq",))
    out = []
    for p in providers:
        model = TASK_MODELS.get((task, p)) or (data.settings().groq_model if p == "groq"
                                               else "local")
        out.append(f"{p}:{model}")
    return " → ".join(out) + (" (no fallback)" if task in CAPABLE_ONLY else "")


def sources() -> None:
    s = data.settings()
    st.markdown("Jobs are collected once a day at 07:00 (and whenever you press *Fetch new jobs*) "
                "from public job-board APIs and from the careers pages of the companies on your "
                "lists. Each posting is normalised, de-duplicated across sources, filtered by "
                "your hard rules and scored.")
    keys = {
        "Adzuna": bool(s.adzuna_app_id and s.adzuna_app_key),
        "Jooble (US)": bool(s.jooble_api_key_us),
        "ITJobs.pt": bool(s.itjobs_api_key),
        "Findwork": bool(s.findwork_api_key),
    }
    st.html("<div class='ra-kicker'>Keyed sources</div>" + "".join(
        components.chips([f"{k} {'· key set' if v else '· no key — skipped'}"],
                         "ok" if v else "warn") for k, v in keys.items())
            + "<div class='ra-kicker' style='margin-top:0.4rem'>Keyless</div>"
            + components.chips(["Arbeitnow", "EURES", "Workable", "JobTech (SE)", "DevJobs",
                                "MyCareersFuture (SG)", "Himalayas", "Jobicy", "Remote OK",
                                "Company ATS boards: Greenhouse, Lever, Ashby, Workable, "
                                "SmartRecruiters, Recruitee, Personio, Teamtailor"]))
    rows = data.conn().execute(
        "SELECT name, kind, jobs_found, jobs_new, duration_ms, last_success_at, last_failure_at,"
        " last_error FROM job_sources WHERE kind != 'people_list'"
        " ORDER BY COALESCE(last_success_at, last_failure_at) DESC").fetchall()
    if rows:
        frame = pd.DataFrame([{
            "Source": r["name"], "Kind": r["kind"],
            "Last read": fmt.age(r["last_success_at"]) if r["last_success_at"] else "never",
            "Found": r["jobs_found"] or 0, "New": r["jobs_new"] or 0,
            "Time": fmt.seconds((r["duration_ms"] or 0) / 1000),
            "Problem": (r["last_error"] or "")[:120]
            if (r["last_failure_at"] or "") > (r["last_success_at"] or "") else "",
        } for r in rows])
        st.dataframe(frame, hide_index=True, height=320)
        st.caption(f"{len(frame)} sources, including one per company careers board.")
    runs = data.conn().execute(
        "SELECT r.id, r.status, r.started_at, r.finished_at, p.message FROM runs r"
        " LEFT JOIN run_progress p ON p.run_id = r.id WHERE r.command = 'job-search'"
        " ORDER BY r.id DESC LIMIT 6").fetchall()
    if runs:
        st.markdown("**Recent collections**")
        for r in runs:
            st.markdown(f"- #{r['id']} · {r['status']} · started {fmt.age(r['started_at'])}"
                        f" · {r['message'] or ''}")


def scoring() -> None:
    from pitwall.jobsearch.settings import load_config

    cfg = load_config()
    weights = cfg.weights.model_dump()
    total = sum(weights.values()) or 1
    st.markdown("Every job that passes your hard rules gets a score from 0 to 100: eight parts, "
                "each 0-1, weighted by your settings, minus penalties for languages you lack, "
                "topics you avoid and recruitment agencies. **Strong** is 75+, **good** 60+, "
                "**fair** 45+.")
    st.dataframe(pd.DataFrame([{
        "Part": fmt.COMPONENT_NAMES.get(k, k), "Weight": v / total * 100,
        "What it measures": COMPONENT_HELP.get(k, ""),
    } for k, v in sorted(weights.items(), key=lambda kv: -kv[1])]), hide_index=True,
        column_config={"Weight": st.column_config.ProgressColumn(
            "Share of the score", min_value=0, max_value=100, format="%.0f%%")})
    reasons = data.conn().execute(
        "SELECT json_extract(value, '$.code') AS code, COUNT(*) AS n FROM job_matches m,"
        " json_each(m.filter_reasons) WHERE m.passed = 0 GROUP BY code ORDER BY n DESC"
    ).fetchall()
    if reasons:
        st.markdown("**Hard rules, and how many jobs each removed**")
        st.dataframe(pd.DataFrame([{"Rule": r["code"], "Jobs removed": r["n"]} for r in reasons]),
                     hide_index=True)
    if st.button("Measure the score against my ratings", icon=":material/query_stats:"):
        from pitwall.jobsearch.calibrate import calibrate

        report = calibrate(data.conn(), weights)
        if not report.good and not report.bad:
            st.info("No ratings yet — rate jobs 👍/👎 in PitWall's Job Search to measure this.")
        else:
            st.markdown(f"On **{report.good} good** and **{report.bad} bad** jobs you rated, the "
                        f"current score ranks a good one above a bad one "
                        f"**{(report.current_auc or 0) * 100:.0f}%** of the time "
                        f"(50% would be a coin flip).")
            st.dataframe(pd.DataFrame([{"Part": fmt.COMPONENT_NAMES.get(k, k),
                                        "Agrees with you": v * 100}
                                       for k, v in report.auc.items()]), hide_index=True,
                         column_config={"Agrees with you": st.column_config.ProgressColumn(
                             "Agrees with you", min_value=0, max_value=100, format="%.0f%%")})


def generation() -> None:
    from pitwall.apply.letter import PROMPT_VERSION

    st.markdown("An application is made in eight stages. Only three ever call a model; the CV "
                "itself is chosen and laid out deterministically from your profile, so it can "
                "only ever say what the profile says.")
    rows = []
    for n, (key, label) in enumerate(STAGES, start=1):
        models = ", ".join(_model_for(t) for t in STAGE_TASKS.get(key, ()))
        rows.append(
            f"<div class='ra-stage' style='grid-template-columns:1.6rem 13rem 1fr'>"
            f"<span class='ra-faint'>{n}</span>"
            f"<span class='what'>{label[:1].upper() + label[1:]}</span>"
            f"<span class='out'>{STAGE_HELP.get(key, '')}<br>"
            + (f"<span class='ra-chip accent'>model · {models}</span>" if models
               else "<span class='ra-chip ok'>no model — deterministic rules</span>")
            + "</span></div>")
    st.html("".join(rows))
    st.markdown("**The gates every letter passes** — deterministic, so a model cannot talk its "
                "way past them. A claim or policy failure marks the letter *do not send*; a "
                "style issue only asks for a look.")
    st.html(components.chips([f"{name}: {what}" for name, what in GATES]))
    st.caption(f"Letter prompt {PROMPT_VERSION} · note prompt n1 · models via Groq's free tier")
    rooms = key_room(data.settings())
    if rooms:
        st.markdown("**Groq keys, rolling 24 hours**")
        st.dataframe(pd.DataFrame([{
            "Key": r.key, "Used": r.day_used, "Left": r.room,
            "Share used": r.day_used / max(1, r.day_limit) * 100,
            "Note": r.reason or "",
        } for r in rooms]), hide_index=True, column_config={
            "Share used": st.column_config.ProgressColumn("Used", min_value=0, max_value=100,
                                                          format="%.0f%%")})


def quality() -> None:
    from pitwall.apply.history import scan
    from pitwall.apply.voice_eval import by_version, load

    gens = [g for g in scan("output") if "3_archive" not in str(g.directory)]
    if not gens:
        st.info("Nothing generated yet.")
        return
    states = {s: sum(1 for g in gens if g.state == s) for s in ("clean", "review", "unsent", "")}
    c = st.columns(4)
    with c[0]:
        components.card("Applications on disk", str(len(gens)), "not counting the archive")
    with c[1]:
        components.card("Ready to send", str(states["clean"]),
                        f"{states['clean'] / max(1, len(gens)):.0%} of them")
    with c[2]:
        checked = [g for g in gens if g.sentences]
        rate = (sum(g.supported for g in checked) / max(1, sum(g.sentences for g in checked)))
        components.card("Sentences supported", f"{rate:.0%}" if checked else "—",
                        f"by the independent check, {len(checked)} letter(s)")
    with c[3]:
        cov = [g.coverage for g in gens if g.coverage]
        components.card("Skill coverage", f"{statistics.mean(cov):.0%}" if cov else "—",
                        "of each posting's required skills")
    samples = _samples(pathlib.Path("output"))
    st.markdown("**What a job costs, by mode**")
    st.dataframe(pd.DataFrame([{
        "Mode": MODE_LABELS[m], "Runs": len(v),
        "Median tokens": statistics.median([n for _, n in v]) if v else None,
        "80th percentile": sorted(n for _, n in v)[int(0.8 * (len(v) - 1))] if v else None,
    } for m, v in samples.items()]), hide_index=True)
    ratings = load()
    if ratings:
        st.markdown("**Your own ratings of the letters** — the only measure that is not "
                    "the machine grading itself")
        st.dataframe(pd.DataFrame([{
            "Prompt": s.version, "Letters rated": s.count,
            "Sounds human (1-5)": round(s.naturalness, 2),
            "You would rewrite": f"{s.edit_burden:.0f}%",
            "Meets the bar (≤30%)": "yes" if s.ships else "not yet",
        } for s in by_version(ratings)]), hide_index=True)
    letters = {g.name: g.letter_path.read_text(encoding="utf-8")
               for g in gens[:40] if g.letter_path and g.letter_path.exists()}
    if len(letters) >= 2:
        from pitwall.apply.batch import duplicate_sentences

        repeated = duplicate_sentences(letters)
        if repeated:
            st.warning(f"{len(repeated)} sentence(s) appear word for word in more than one recent "
                       "letter — a template creeping in.")
            with st.expander("Which"):
                st.code(json.dumps(repeated, indent=2, ensure_ascii=False)[:4000], language=None)
        else:
            st.caption(f"No sentence repeats across the last {len(letters)} letters.")


components.header(
    "How it works",
    "Where the jobs come from, how each is scored, how an application is made — and how "
    "good the output has been. All read live from the running system.",
)
tabs = st.tabs(["Sources", "Scoring", "Making an application", "Quality"])
with tabs[0]:
    sources()
with tabs[1]:
    scoring()
with tabs[2]:
    generation()
with tabs[3]:
    quality()
