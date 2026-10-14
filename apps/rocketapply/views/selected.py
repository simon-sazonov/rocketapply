"""Selected — the shortlist, and what today's quota can turn into applications.

Pick the documents (CV only, or CV + letter), see how many fit in what is left
of today's Groq budget, and generate. The best-scored jobs go first; anything
past the cap waits for tomorrow rather than being queued past the limit.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from pitwall.apply.budget import MODE_LABELS, fits_today, rolling_reset_hint
from pitwall.jobsearch import tracker
from rocket import components, data, runner
from rocket import format as fmt

components.header(
    "Selected",
    "Your shortlist. Choose what to make, check today's budget, and generate — every stage "
    "is shown live on the next screen.",
)

apps = data.applications("SHORTLISTED")
if runner.generation_active():
    gen = runner.registry().generation
    st.info(f"A generation is running: {len(gen.finished_jobs)} of {len(gen.job_ids)} done.",
            icon=":material/bolt:")
    st.page_link("views/generate.py", label="Watch it", icon=":material/visibility:")

if not apps:
    components.empty("Nothing selected yet.",
                     "Tick jobs on Search, or take the top N, and they arrive here.")
    st.page_link("views/search.py", label="Go to Search", icon=":material/search:")
    st.stop()

# ── what to make ──
left, right = st.columns([1.1, 1.6], gap="large")
with left:
    choice = st.segmented_control(
        "Documents", ["cv_letter", "cv"], default="cv_letter", key="gen_mode",
        format_func=lambda m: MODE_LABELS[m], width="stretch")
    choice = choice or "cv_letter"
    audit = False
    if choice == "cv_letter":
        with st.expander("Advanced"):
            audit = st.toggle(
                "Independent fact-check of every sentence",
                help="A second model (a different family from the writer) reads each sentence "
                     "about you against your profile. Roughly 2-3x the tokens; it never decides "
                     "whether a letter is sendable — the deterministic gates do.")
            workers = st.slider("Jobs at once", 1, 5, 3,
                                help="One per Groq key is the sensible maximum.")
    else:
        workers = 3
    mode = "cv_letter_audit" if audit else choice
    st.caption({
        "cv": "The CV is chosen and laid out deterministically from your profile; one model "
              "call reads the posting. Letters can be added later.",
        "cv_letter": "CV, then a letter planned from the posting and your evidence, written, "
                     "checked against every claim rule, and typeset on the CV's letterhead.",
        "cv_letter_audit": "As CV + letter, plus an independent model reading every sentence "
                           "about you. Slowest and dearest; best for the few you care most about.",
    }[mode])

ordered = sorted(apps, key=lambda a: -(a.get("score") or 0))
budget = fits_today(mode, job_ids=[int(a["job_id"]) for a in ordered if a.get("job_id")],
                    settings=data.settings())
with right:
    c1, c2, c3 = st.columns(3)
    with c1:
        components.card("Fits today", f"{budget.fits:,}",
                        f"of {len(ordered):,} selected")
    with c2:
        components.card("Tokens left", fmt.tokens(budget.room),
                        f"of {fmt.tokens(budget.daily_limit)} · rolling 24 h")
    with c3:
        components.card("Per job", fmt.tokens(budget.per_job), budget.basis.split(";")[0])
    hint = rolling_reset_hint(budget.keys)
    st.caption(f"{budget.basis}" + (f" · {hint}" if hint else "")
               + (f" · {budget.unresearched} employer(s) still to research" if budget.unresearched
                  else ""))

# ── the list ──
st.markdown("#### The shortlist, best first")
cap = budget.fits
frame = pd.DataFrame([{
    "Make": i < cap,
    "Fit": a.get("score") or 0,
    "Role": a["title"],
    "Company": a["company_name"],
    "Where": a.get("location") or "",
    "Today": "✓ fits" if i < cap else "waits for tomorrow",
    "Hint for the letter": "",
    "app_id": a["id"],
    "job_id": a.get("job_id"),
} for i, a in enumerate(ordered)])

edited = st.data_editor(
    frame, key="shortlist", hide_index=True, width="stretch",
    height=min(620, 60 + 35 * len(frame)),
    column_order=["Make", "Fit", "Role", "Company", "Where", "Today", "Hint for the letter"],
    disabled=["Fit", "Role", "Company", "Where", "Today"],
    column_config={
        "Make": st.column_config.CheckboxColumn("Make", width="small"),
        "Fit": st.column_config.ProgressColumn("Fit", min_value=0, max_value=100, format="%.0f",
                                               width="small"),
        "Role": st.column_config.TextColumn("Role", width="large"),
        "Hint for the letter": st.column_config.TextColumn(
            "Hint for the letter", help="Optional. E.g. 'lead with the RL project'. "
            "Ignored for CV only.", width="medium"),
    },
)
chosen = edited[edited["Make"]]
over = max(0, len(chosen) - cap)
to_make = chosen.head(cap)

bar = st.columns([1.6, 1.4, 1.2], vertical_alignment="center")
label = (f"Generate {len(to_make)} · {MODE_LABELS[mode]}" if len(to_make)
         else "Nothing to generate")
disabled = not len(to_make) or runner.generation_active() or cap == 0
if bar[0].button(label, type="primary", icon=":material/bolt:", disabled=disabled,
                 width="stretch"):
    hints = {int(r["job_id"]): str(r["Hint for the letter"]).strip()
             for _, r in to_make.iterrows()
             if mode != "cv" and str(r["Hint for the letter"] or "").strip()}
    run = runner.start_generation([int(j) for j in to_make["job_id"]], mode=mode,
                                  hints=hints, workers=workers)
    if run is None:
        st.warning("A generation is already running.")
    else:
        st.switch_page("views/generate.py")
unticked = edited[~edited["Make"]]
if bar[1].button(f"Dismiss {len(unticked)} unticked", icon=":material/block:",
                 disabled=not len(unticked), width="stretch",
                 help="Not interested: they leave the pipeline and a later search does not "
                      "bring them back. Undo from Search → Filters → Include dismissed."):
    job_ids = [int(j) for j in unticked["job_id"].dropna()]
    data.reject(job_ids, "Not interested")
    manual = [int(a) for a, j in zip(unticked["app_id"], unticked["job_id"], strict=True)
              if pd.isna(j)]
    if manual:
        data.write(lambda c: [tracker.skip(c, a) for a in manual])
    st.rerun()
if over:
    bar[2].caption(f"{over} ticked job(s) are past today's budget and will wait.")
elif cap == 0:
    bar[2].caption("No quota left today. " + (hint or ""))
