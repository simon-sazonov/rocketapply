"""Applications — what went out, and who to write to about it.

In process: each exported CSV is a batch; untick anything you did not send,
and one button marks the rest applied. Applied: each application opens its
People panel — LinkedIn searches you can click, PitWall's own search for
recruiters, the hiring manager, peers and alumni at that employer, their
email (free first, Hunter only on a click), and a short note to send them.
Nothing is ever sent from here.
"""

from __future__ import annotations

import html
import pathlib
import time
from typing import Any

import pandas as pd
import streamlit as st

from pitwall.jobsearch import tracker
from pitwall.jobsearch.linkedin_links import presets
from pitwall.jobsearch.people import GROUP_LABELS, GROUPS, known_in_pitwall, people_at
from rocket import components, data, runner
from rocket import format as fmt

TABS = {"in_process": "In process", "applied": "Applied", "outcomes": "Outcomes",
        "exports": "Exports"}
OUTCOMES = ("APPLIED", "ONLINE_ASSESSMENT", "INTERVIEW", "FINAL_INTERVIEW", "OFFER",
            "REJECTED", "WITHDRAWN")


# ─────────────────────────────── in process ──────────────────────────────


def in_process() -> None:
    batches = [b for b in data.batches() if (b.get("in_process") or 0) > 0]
    if not batches:
        components.empty("Nothing in process.",
                         "Export approved applications from Review; each CSV becomes a batch here.")
        return
    for batch in batches:
        items = data.batch_items(int(batch["id"]))
        waiting = [i for i in items if i["status"] == "IN_PROCESS"]
        with st.container(border=True):
            top = st.columns([3, 1.2], vertical_alignment="center")
            top[0].markdown(f"**{batch['name']}** · exported {fmt.age(batch['created_at'])} · "
                            f"{len(waiting)} of {batch['size']} still in process")
            csv_path = pathlib.Path(batch["csv_path"]) if batch.get("csv_path") else None
            if csv_path and csv_path.exists():
                top[1].download_button("The CSV", csv_path.read_bytes(), file_name=csv_path.name,
                                       mime="text/csv", icon=":material/download:",
                                       width="stretch", key=f"csv_{batch['id']}")
            frame = pd.DataFrame([{
                "Sent": True, "#": i["position"], "Company": i["company_name"],
                "Role": i["title"], "Apply at": i.get("application_url") or "",
                "app_id": i["application_id"],
            } for i in waiting])
            edited = st.data_editor(
                frame, key=f"batch_{batch['id']}", hide_index=True, width="stretch",
                column_order=["Sent", "#", "Company", "Role", "Apply at"],
                disabled=["#", "Company", "Role", "Apply at"],
                column_config={
                    "Sent": st.column_config.CheckboxColumn("Sent", width="small",
                                                            help="Untick any you did not send."),
                    "#": st.column_config.NumberColumn("#", width="small"),
                    "Apply at": st.column_config.LinkColumn("Apply at", display_text="Open ↗"),
                })
            sent = edited[edited["Sent"]]
            unsent = [int(a) for a in edited[~edited["Sent"]]["app_id"]]
            if st.button(f"Mark {len(sent)} as applied", type="primary",
                         icon=":material/done_all:", disabled=not len(sent),
                         key=f"apply_{batch['id']}"):
                moved = data.write(tracker.mark_batch_applied, int(batch["id"]), exclude=unsent)
                st.toast(f"{moved} application(s) marked applied — today's date recorded, "
                         "documents moved to 4_already_sent.")
                st.rerun()


# ─────────────────────────────── people ──────────────────────────────────


def _person_card(person: dict[str, Any], app: dict[str, Any], profile: Any = None) -> None:
    from pitwall.apply.outreach import clean_title, concerns, template_texts
    from pitwall.jobsearch.people import employer_repos, employer_settings

    pid = int(person["id"])
    with st.container(border=True):
        top = st.columns([3, 1.1], vertical_alignment="center")
        email = person.get("email")
        about = clean_title(person)[0] or "no clear title"
        flags = concerns(person, app["company_name"])[0]
        top[0].html(
            f"<b>{html.escape(person.get('full_name') or '?')}</b> · "
            f"<span class='ra-dim'>{html.escape(about)}</span>"
            + (f"<br><span class='ra-chip {'ok' if person.get('email_status') == 'verified' else 'accent'}'>"
               f"{html.escape(email)} · {person.get('email_status')}</span>" if email else "")
            + (f"<br><span class='ra-dim'>⚠ {html.escape('; '.join(flags))}</span>"
               if flags else ""))
        if person.get("profile_url"):
            top[1].link_button("LinkedIn", person["profile_url"], icon=":material/open_in_new:",
                               width="stretch")
        b = st.columns(3)
        if b[0].button("Find email", key=f"em_{pid}", icon=":material/alternate_email:",
                       width="stretch", help="Free: the company's domain, its address pattern, "
                       "and whether the address is published anywhere. No credits."):
            from pitwall import enrich

            with st.spinner("Looking for their address…"):
                ctx = enrich.enrich_candidate(pid, employer_repos(), employer_settings(),
                                              steps=enrich.free_chain())
            found = enrich.summary(ctx)
            st.toast(f"{found.get('email') or 'No address found'}"
                     + (f" ({found.get('status')})" if found.get("email") else ""))
            st.rerun()
        with b[1].popover("Hunter", icon=":material/toll:", width="stretch"):
            st.caption("One Hunter Email Finder lookup — 1 credit, and only if it finds an "
                       "address. Credits are shared with PitWall.")
            if st.button("Spend 1 credit", key=f"hu_{pid}", type="primary"):
                from pitwall import enrich

                with st.spinner("Asking Hunter…"):
                    ctx = enrich.enrich_candidate(pid, employer_repos(), employer_settings(),
                                                  steps=enrich.escalation_chain("finder"))
                st.toast(enrich.summary(ctx).get("email") or "Hunter found nothing.")
                st.rerun()
        note, message = template_texts(person, app["company_name"], [app], profile=profile)
        with st.expander("Note and message, ready to paste"):
            st.caption(f"With the connection request · {len(note)} characters")
            st.code(note, language=None, wrap_lines=True)
            st.caption("Once they accept")
            st.code(message, language=None, wrap_lines=True)
        if b[2].button("Personalise with AI", key=f"nt_{pid}", icon=":material/edit_note:",
                       width="stretch", help="Two short LinkedIn notes (≤200 characters) from "
                       "this application's own evidence, instead of the template. About 1.5k "
                       "tokens. The one you save goes into the outreach CSV. Never sent."):
            from pitwall.apply.outreach import draft_note

            with st.spinner("Drafting…"):
                try:
                    st.session_state[f"note_{pid}"] = draft_note(person=person, application=app)
                except Exception as exc:  # noqa: BLE001 - quota, network: say so
                    st.error(f"No draft: {exc}")
        draft = st.session_state.get(f"note_{pid}")
        if draft is not None:
            _note_editor(pid, person, app, draft)


def _note_editor(pid: int, person: dict[str, Any], app: dict[str, Any], draft: Any) -> None:
    from pitwall.apply.outreach import LIMIT, check_note, save_note

    cols = st.columns(len(draft.variants) or 1)
    for i, text in enumerate(draft.variants):
        with cols[i]:
            edited = st.text_area(f"Variant {i + 1}", text, key=f"nv_{pid}_{i}", height=120,
                                  max_chars=LIMIT + 60)
            live = check_note(edited, applied=app.get("status") in tracker.SUBMITTED, limit=LIMIT)
            st.caption(f"{len(edited)}/{LIMIT} characters"
                       + (" · " + "; ".join(live) if live else " · passes every check"))
            st.code(edited, language=None, wrap_lines=True)
            if st.button("Save this one", key=f"ns_{pid}_{i}", width="stretch"):
                data.write(save_note, int(app["id"]), person, edited, edited=edited != text,
                           checks=live, model=draft.model, tokens=draft.tokens if i == 0 else 0)
                st.toast("Saved. Copy it with the button on the box above.")
    st.caption(f"{draft.model} · {fmt.tokens(draft.tokens)} tokens · "
               f"{draft.attempts} attempt(s). Copy, paste into LinkedIn yourself — nothing is sent "
               "from here.")


@st.fragment(run_every=1.5)
def _people_progress(company: str) -> None:
    run = runner.people_run(company)
    if run is None or not run.alive:
        st.rerun(scope="app")
        return
    st.info("Searching the web for people at " + company + " … "
            + (run.messages[-1] if run.messages else "starting"), icon=":material/travel_explore:")


def people_panel(app: dict[str, Any]) -> None:
    company, title = app["company_name"], (app["title"] or "").strip()
    st.markdown(f"#### People at {company}")
    st.caption(f"For your application to **{title}**"
               + (f", sent {app['date_applied']}" if app.get("date_applied") else ""))

    with st.expander("Search LinkedIn yourself — one click each, free", expanded=True):
        try:
            from pitwall.apply.loader import load_profile

            profile = load_profile()
        except Exception:  # noqa: BLE001 - no profile: no alumni links
            profile = None
        links = presets(company, title, profile=profile)
        cols = st.columns(len(GROUPS))
        for col, group in zip(cols, GROUPS, strict=True):
            with col:
                st.markdown(f"**{GROUP_LABELS[group]}**")
                for link in (x for x in links if x.group == group):
                    st.markdown(f"[{link.label}]({link.url})")

    run = runner.people_run(company)
    busy = run is not None and run.alive
    found = people_at(company)
    c1, c2 = st.columns([1.3, 3], vertical_alignment="center")
    label = "Search again" if found else "Find people with PitWall"
    if c1.button(label, type="secondary" if found else "primary", disabled=busy,
                 icon=":material/travel_explore:", width="stretch",
                 help="Web search → LinkedIn profiles, for recruiters, the hiring manager, "
                 "peers and alumni. About a minute; no AI tokens. Kept apart from PitWall's "
                 "motorsport list."):
        runner.start_people(company, job_title=title)
        time.sleep(0.4)
        st.rerun()
    c2.caption(f"{len(found)} found so far" if found else
               "Uses PitWall's search engine on this one employer.")
    if busy:
        _people_progress(company)
    if run is not None and run.error:
        st.error(f"The search stopped: {run.error}")
    if found:
        from pitwall.jobsearch.people import employer_domain, set_employer_domain

        known = employer_domain(company)
        d1, d2 = st.columns([2, 1], vertical_alignment="bottom")
        typed = d1.text_input(
            "Their email domain", value=known, placeholder="e.g. waymo.com",
            key=f"dom_{company}",
            help="Find email guesses addresses from the company's pattern at this domain. "
                 "It is looked up automatically when local search (SearXNG) is running; "
                 "otherwise type it once here.")
        if d2.button("Save domain", key=f"domsave_{company}", disabled=not typed or typed == known,
                     width="stretch"):
            try:
                st.toast(f"Using {set_employer_domain(company, typed)} for {company}")
            except ValueError as exc:
                st.error(str(exc))
            st.rerun()

    from pitwall.apply.outreach import concerns

    for group in GROUPS:
        # The order the CSV picks in: nothing to check first, then best score.
        people = sorted((p for p in found if p["group"] == group),
                        key=lambda p: concerns(p, company)[1])
        if not people:
            continue
        st.markdown(f"**{GROUP_LABELS[group]}** · {len(people)}")
        for person in people[:8]:
            _person_card(person, app, profile)

    known = known_in_pitwall(data.conn(), company)
    if known:
        with st.expander(f"Already in PitWall · {len(known)}"):
            for p in known:
                st.markdown(f"- [{p['full_name']}]({p['profile_url']}) — {p.get('headline') or ''}")


@st.fragment(run_every=1.5)
def _bulk_progress() -> None:
    run = runner.outreach_run()
    if run is None or not run.alive:
        st.rerun(scope="app")
        return
    st.progress(min(run.current / max(run.total, 1), 1.0),
                text=f"Company {run.current} of {run.total} · "
                     + (run.messages[-1] if run.messages else "starting"))
    if st.button("Stop after this company", icon=":material/stop_circle:", key="bulk_stop"):
        run.stop = True


def bulk_panel(apps: list[dict[str, Any]]) -> None:
    """People for many applications at once: one run, one CSV, one row a person."""
    from pitwall.apply.outreach import OUTREACH_COLUMNS, BulkPlan, outreach_rows
    from pitwall.jobsearch.export import _csv

    plan = BulkPlan(applications=apps)
    companies = plan.companies()
    st.markdown(f"#### People for {len(apps)} applications")
    st.caption(f"At {len(companies)} companies. Each company is searched once, and each person "
               "gets one row however many roles you applied to there. Nothing is ever sent.")

    run = runner.outreach_run()
    busy = run is not None and run.alive

    o = st.columns([1, 1], vertical_alignment="bottom")
    per_company = o[0].number_input(
        "People per company", 1, 6, 3, key="bulk_per_company",
        help="Picked one from each group in turn: a recruiter, the hiring manager, a peer, "
        "then alumni — and within a group, the ones with nothing to check first.")
    emails = o[1].toggle("Find emails (free)", value=True, key="bulk_emails",
                         help="The company's address pattern and public sources. Never Hunter "
                         "— that stays a per-person click.")
    research = st.checkbox("Search companies again even if people were already found",
                           key="bulk_research")

    # What is there already, per company — the same `people_at` the panel uses.
    from pitwall.jobsearch.people import people_at

    found = {name: people_at(name) for name in companies}
    summary = pd.DataFrame([{
        "Company": name, "Roles": len(group), "People": len(found[name]),
        "With email": sum(1 for p in found[name] if p.get("email")),
    } for name, group in companies.items()])
    to_search = sum(1 for name in companies if research or not found[name])

    label = (f"Find people at {to_search} compan{'y' if to_search == 1 else 'ies'}"
             if to_search else "Update emails")
    if st.button(label, type="primary", icon=":material/travel_explore:", disabled=busy,
                 width="stretch", key="bulk_go",
                 help="About a minute per company searched. Runs in the background — you can "
                 "keep using the app; the People panel of each application fills in as it goes."):
        runner.start_outreach(apps, per_company=int(per_company), emails=emails,
                              research=research)
        time.sleep(0.4)
        st.rerun()
    st.caption(f"{to_search} to search, {len(companies) - to_search} already searched · "
               "no AI tokens.")
    if busy:
        _bulk_progress()
    elif run is not None and run.status in ("done", "stopped"):
        c = run.counts
        st.success(f"{'Stopped' if run.status == 'stopped' else 'Done'}: {c.get('searched', 0)} "
                   f"searched, {c.get('emails', 0)} emails found"
                   + (f", {c['failed']} compan(ies) skipped" if c.get("failed") else ""),
                   icon=":material/check_circle:")
    elif run is not None and run.error:
        st.error(f"The run stopped: {run.error}")

    st.dataframe(summary, hide_index=True, width="stretch",
                 height=min(420, 35 * (len(summary) + 1) + 3))

    by_name = {name.casefold(): people for name, people in found.items()}
    rows = outreach_rows(data.conn(), apps, per_company=int(per_company),
                         people=lambda company: by_name.get(company.strip().casefold(), []))
    st.download_button(
        f"Outreach CSV · {len(rows)} people", _csv(rows, OUTREACH_COLUMNS).encode("utf-8-sig"),
        file_name=f"outreach_{time.strftime('%Y_%m_%d')}.csv", mime="text/csv",
        icon=":material/download:", disabled=not rows, width="stretch", key="bulk_csv",
        help="One row per person: LinkedIn, email if found, a note for the connection request "
        "(≤200 characters) and a message for after they accept. The wording is in "
        "config/outreach_templates.yaml; a note you save in a People panel replaces it.")
    if rows:
        flagged = sum(1 for r in rows if r["check"])
        with st.expander(f"Preview · {len(rows)} people, {flagged} to check before sending"):
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                         column_order=["company", "name", "group", "note", "check"])


def applied() -> None:
    apps = data.applications("APPLIED")
    if not apps:
        components.empty("No applications marked applied yet.",
                         "Mark a batch applied under In process; each one then gets its "
                         "People panel here.")
        return
    frame = pd.DataFrame([{
        "Company": a["company_name"], "Role": a["title"],
        "Applied": a.get("date_applied") or "", "Fit": a.get("score") or 0, "id": a["id"],
    } for a in sorted(apps, key=lambda a: a.get("date_applied") or "", reverse=True)])
    left, right = st.columns([1, 1.9], gap="medium")
    with left:
        st.caption("Pick one to work on it, or tick several — the box in the header ticks all "
                   "— to find people for every one at once.")
        event = st.dataframe(frame, key="applied_list", on_select="rerun",
                             selection_mode="multi-row", hide_index=True,
                             height=min(620, 60 + 35 * len(frame)),
                             column_order=["Company", "Role", "Applied", "Fit"],
                             column_config={"Fit": st.column_config.ProgressColumn(
                                 "Fit", min_value=0, max_value=100, format="%.0f",
                                 width="small")})
        rows = list(event.selection.rows) if event else []
    by_id = {int(a["id"]): a for a in apps}
    chosen = [by_id[int(frame.iloc[r]["id"])] for r in rows]
    with right:
        if len(chosen) > 1:
            bulk_panel(chosen)
        else:
            people_panel(chosen[0] if chosen else by_id[int(frame.iloc[0]["id"])])


# ─────────────────────────────── outcomes ────────────────────────────────


def outcomes() -> None:
    apps = data.applications(*OUTCOMES)
    if not apps:
        components.empty("No outcomes to track yet.")
        return
    frame = pd.DataFrame([{
        "Company": a["company_name"], "Role": a["title"], "Applied": a.get("date_applied") or "",
        "Status": a["status"], "Next step": a.get("next_action") or "",
        "When": a.get("next_action_date") or "", "Notes": a.get("notes") or "", "id": a["id"],
    } for a in apps])
    edited = st.data_editor(
        frame, key="outcomes", hide_index=True, width="stretch",
        column_order=["Company", "Role", "Applied", "Status", "Next step", "When", "Notes"],
        disabled=["Company", "Role", "Applied"],
        column_config={"Status": st.column_config.SelectboxColumn(
            "Status", options=list(OUTCOMES), format_func=lambda s: fmt.STATUS_NAMES.get(s, s))})
    if st.button("Save changes", icon=":material/save:"):
        before = frame.set_index("id")
        changed = 0
        for _, row in edited.iterrows():
            old = before.loc[row["id"]]
            if row["Status"] != old["Status"]:
                data.write(tracker.set_status, int(row["id"]), row["Status"])
                changed += 1
            fields = {"next_action": row["Next step"], "next_action_date": row["When"],
                      "notes": row["Notes"]}
            if any(str(fields[k] or "") != str(old[c] or "") for k, c in
                   (("next_action", "Next step"), ("next_action_date", "When"),
                    ("notes", "Notes"))):
                data.write(tracker.update, int(row["id"]), **fields)
                changed += 1
        st.toast(f"{changed} change(s) saved")
        st.rerun()


def exports() -> None:
    batches = data.batches()
    if not batches:
        components.empty("No exports yet.")
        return
    for b in batches:
        c = st.columns([3, 1.2, 1.2], vertical_alignment="center")
        c[0].markdown(f"**{b['name']}** · {b['size']} application(s) · {b['applied'] or 0} applied"
                      f" · exported {fmt.age(b['created_at'])}")
        path = pathlib.Path(b["csv_path"]) if b.get("csv_path") else None
        if path and path.exists():
            c[1].download_button("CSV", path.read_bytes(), file_name=path.name, mime="text/csv",
                                 icon=":material/download:", key=f"exp_{b['id']}",
                                 width="stretch")
            c[2].caption(str(path))


# ─────────────────────────────── the page ────────────────────────────────


components.header(
    "Applications",
    "Mark what went out, then reach the people behind each role — one at a time, or all at once. Nothing is ever sent "
    "from this app — it finds, drafts and hands you the text.",
)
wanted = st.query_params.get("tab", "in_process")
tabs = st.tabs(list(TABS.values()), default=TABS.get(wanted, "In process"), key="app_tabs")
with tabs[0]:
    in_process()
with tabs[1]:
    applied()
with tabs[2]:
    outcomes()
with tabs[3]:
    exports()
