"""Review — read, edit, approve, then export the approved ones as a CSV.

Every generated application waits here. The letter is editable; saving runs
the same deterministic gates the model's draft passed, and the verdict renames
the file and moves the folder exactly as it would have for a generated letter.
Approved applications export to one CSV — the link to apply at and the path of
every file to upload — and move to In process.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import subprocess
import sys

import pandas as pd
import streamlit as st

from pitwall.apply.budget import fits_today
from pitwall.apply.history import read
from pitwall.apply.pipeline import DO_NOT_SEND_BANNER
from pitwall.jobsearch import tracker
from pitwall.jobsearch.export import write_batch_csv
from rocket import components, data, runner
from rocket import format as fmt

STATE_KIND = {"clean": "ok", "review": "warn", "unsent": "fail", "": "accent"}


def _letter_text(path: pathlib.Path | None) -> str:
    if not path or not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    return text[len(DO_NOT_SEND_BANNER):] if text.startswith(DO_NOT_SEND_BANNER) else text


def _reveal(path: pathlib.Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", str(path)], check=False)


def detail(app: dict) -> None:
    gen = read(app["generation_dir"]) if app.get("generation_dir") else None
    state = app.get("letter_state") or (gen.state if gen else "")
    with st.container(border=True):
        st.markdown(f"#### {app['title']}")
        st.html(f"<b>{app['company_name']}</b> · {app.get('location') or ''} &nbsp;"
                + components.chips([fmt.STATE_NAMES.get(state, state)], STATE_KIND.get(state, ""))
                + (components.chips(["approved"], "ok") if app.get("reviewed_at") else ""))
        if gen is None:
            st.warning("The application folder is missing — it may have been moved by hand. "
                       "Regenerate to make it again.")
            return
        tabs = st.tabs(["Letter", "CV", "Checks", "Files"])
        with tabs[0]:
            if gen.letter_path is None:
                st.caption("CV only — no letter was asked for. Regenerate with a letter to add one.")
            else:
                original = _letter_text(gen.letter_path)
                text = st.text_area("Letter", original, height=420, key=f"letter_{app['id']}",
                                    label_visibility="collapsed")
                st.caption(f"{len(text.split())} words · the model's own text is kept when you "
                           "save an edit")
                if gen.flagged:
                    st.html("<div class='ra-kicker'>Questioned by the fact-check</div>"
                            + components.chips([f"{f.get('verdict')}: {f.get('span') or f.get('sentence', '')[:90]}"
                                                for f in gen.flagged], "warn"))
                if st.button("Save edits", icon=":material/save:", disabled=text == original,
                             key=f"save_{app['id']}"):
                    from pitwall.apply.review_edit import save_edited_letter

                    with st.spinner("Checking the edit against every claim rule…"):
                        result = save_edited_letter(gen.directory, text)
                    data.write(tracker.set_generation, int(app["id"]),
                               directory=str(result.directory.resolve()),
                               letter_state=result.state, letter_version=result.letter_path.name)
                    if result.state == "clean":
                        st.toast("Saved — every gate passed.", icon=":material/check:")
                    else:
                        st.toast(f"Saved as “{fmt.STATE_NAMES[result.state]}”: "
                                 + "; ".join((result.violations + result.banned)[:2]))
                    st.rerun()
        with tabs[1]:
            components.pdf_preview(gen.cv_pdf, caption="The CV")
            if gen.letter_pdf:
                components.pdf_preview(gen.letter_pdf, caption="The letter, typeset")
        with tabs[2]:
            review = gen.directory / "REVIEW.md"
            st.markdown(review.read_text(encoding="utf-8") if review.exists()
                        else "No review note.")
        with tabs[3]:
            for label, path in (("CV (PDF)", gen.cv_pdf), ("CV (Word)", gen.cv_docx),
                                ("Letter (PDF)", gen.letter_pdf), ("Letter (text)", gen.letter_path)):
                if path:
                    st.code(str(pathlib.Path(path).resolve()), language=None)
                    st.caption(label)
            if st.button("Show in Finder", icon=":material/folder_open:", key=f"rev_{app['id']}"):
                _reveal(gen.directory)

        a1, a2, a3 = st.columns(3)
        if app.get("reviewed_at"):
            if a1.button("Take back approval", width="stretch", key=f"unap_{app['id']}"):
                data.write(tracker.set_review, int(app["id"]), approved=False)
                st.rerun()
        else:
            blocked = state == "unsent"
            if a1.button("Approve", type="primary", icon=":material/check:", width="stretch",
                         disabled=blocked, key=f"ap_{app['id']}",
                         help="Marked do not send: edit the letter until the gates pass."
                         if blocked else "Ready to go into the next CSV export."):
                data.write(tracker.set_review, int(app["id"]), approved=True)
                st.rerun()
        with a2.popover("Regenerate", icon=":material/autorenew:", width="stretch"):
            mode = "cv_letter" if gen.letter_path else "cv"
            budget = fits_today(mode, settings=data.settings())
            st.caption(f"A fresh letter from the model (not the cached one) · about "
                       f"{fmt.tokens(budget.per_job)} tokens · {fmt.tokens(budget.room)} left today")
            with_letter = st.toggle("With a letter", value=bool(gen.letter_path),
                                    key=f"rl_{app['id']}")
            hint = st.text_input("Steer it (optional)", key=f"hint_{app['id']}",
                                 placeholder="e.g. lead with the RL project; mention Lisbon")
            if st.button("Regenerate now", type="primary", key=f"regen_{app['id']}",
                         disabled=runner.generation_active() or budget.fits < 1):
                run = runner.start_generation(
                    [int(app["job_id"])], mode="cv_letter" if with_letter else "cv",
                    hints={int(app["job_id"]): hint} if hint else None, fresh=True)
                if run:
                    st.switch_page("views/generate.py")
        if a3.button("Remove", icon=":material/delete:", width="stretch", key=f"rm_{app['id']}",
                     help="Off the list, kept on record; the files stay in output/."):
            data.write(tracker.skip, int(app["id"]), note="removed in review")
            st.rerun()


def export_section(approved: list[dict]) -> None:
    st.markdown("#### Export")
    with st.container(border=True):
        if not approved:
            st.caption("Approve applications above; they are exported together as one CSV and "
                       "move to In process.")
            return
        n = len(data.batches()) + 1
        name = st.text_input("Batch name", value=f"{dt.date.today():%Y-%m-%d} batch {n}",
                             key="batch_name")
        c1, c2 = st.columns([1.3, 2], vertical_alignment="center")
        if c1.button(f"Export {len(approved)} approved → CSV", type="primary",
                     icon=":material/download:", width="stretch"):
            ids = [int(a["id"]) for a in approved]
            batch_id = data.write(tracker.create_export_batch, ids, name=name)
            path = data.write(write_batch_csv, batch_id)
            st.session_state["last_export"] = str(path)
            st.rerun()
        c2.caption("One row per application: company, role, score, why it fits, the link to "
                   "apply at, and the full path of every file to upload.")
    last = st.session_state.get("last_export")
    if last and pathlib.Path(last).exists():
        p = pathlib.Path(last)
        st.success(f"Exported to {p}", icon=":material/task_alt:")
        d1, d2, d3 = st.columns(3)
        d1.download_button("Download the CSV", p.read_bytes(), file_name=p.name, mime="text/csv",
                           icon=":material/download:", width="stretch")
        if d2.button("Show in Finder", icon=":material/folder_open:", width="stretch",
                     key="reveal_export"):
            _reveal(p)
        d3.page_link("views/applications.py", label="Go to In process",
                     icon=":material/send:", query_params={"tab": "in_process"})


components.header(
    "Review",
    "Read what was made, fix what needs fixing, approve what should go. Approved "
    "applications export to one CSV and move to In process.",
)
apps = data.applications("CV_READY")
if not apps:
    components.empty("Nothing to review.",
                     "Generated applications arrive here. Start from Selected.")
    last = st.session_state.get("last_export")
    if last:
        export_section([])
    st.page_link("views/selected.py", label="Go to Selected", icon=":material/checklist:")
    st.stop()

waiting = [a for a in apps if not a.get("reviewed_at")]
approved = [a for a in apps if a.get("reviewed_at")]
st.caption(f"**{len(waiting)}** to review · **{len(approved)}** approved")

frame = pd.DataFrame([{
    "✓": "✓" if a.get("reviewed_at") else "",
    "State": fmt.STATE_NAMES.get(a.get("letter_state") or "", ""),
    "Fit": a.get("score") or 0,
    "Role": a["title"],
    "Company": a["company_name"],
    "id": a["id"],
} for a in sorted(apps, key=lambda a: (bool(a.get("reviewed_at")),
                                       {"unsent": 0, "review": 1}.get(a.get("letter_state") or "", 2),
                                       -(a.get("score") or 0)))])
left, right = st.columns([1, 1.6], gap="medium")
with left:
    event = st.dataframe(
        frame, key="review_list", on_select="rerun", selection_mode="single-row",
        hide_index=True, height=min(640, 60 + 35 * len(frame)),
        column_order=["✓", "State", "Fit", "Role", "Company"],
        column_config={"Fit": st.column_config.ProgressColumn("Fit", min_value=0, max_value=100,
                                                              format="%.0f", width="small"),
                       "✓": st.column_config.TextColumn("✓", width="small")})
    rows = event.selection.rows if event else []
    chosen_id = int(frame.iloc[rows[0]]["id"]) if rows else int(frame.iloc[0]["id"])
    if not rows:
        st.caption("Showing the first one — click a row to switch.")
with right:
    app = next(a for a in apps if int(a["id"]) == chosen_id)
    detail(app)

export_section(approved)
