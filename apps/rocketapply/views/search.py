"""Search — every collected job, filtered as you type, graded on your CV.

The box and the Filters popover query the jobs already collected (the daily
07:00 run, or "Fetch new jobs"); nothing here calls a job board. Tick rows, or
take the top N, and they move to Selected.
"""

from __future__ import annotations

import html
import time
from typing import Any

import pandas as pd
import streamlit as st

from pitwall.jobsearch import tracker
from pitwall.jobsearch.store import DISMISS_REASONS, JobQuery
from rocket import components, data, runner
from rocket import format as fmt

PAGE = 200
POSTED = {"Any time": None, "24 hours": 1, "3 days": 3, "A week": 7, "2 weeks": 14,
          "A month": 30}
SORTS = {"Best fit": "score", "Newest": "posted", "Company": "company"}
FILTER_KEYS = ("f_countries", "f_roles", "f_experience", "f_modes", "f_visa", "f_posted",
               "f_min_score", "f_salary", "f_currency", "f_no_salary", "f_lists",
               "f_include_filtered", "f_include_dismissed", "f_hide_tracked")


def _defaults() -> None:
    ss = st.session_state
    ss.setdefault("f_text", "")
    ss.setdefault("f_sort", "Best fit")
    ss.setdefault("f_posted", "Any time")
    ss.setdefault("f_min_score", 0)
    ss.setdefault("f_salary", 0)
    ss.setdefault("f_no_salary", True)
    # On by default: a job you applied to, or closed out, is done with — it
    # should not come back up the list as if it were new.
    ss.setdefault("f_hide_tracked", True)
    ss.setdefault("f_include_filtered", False)
    ss.setdefault("f_include_dismissed", False)
    ss.setdefault("ra_limit", PAGE)


def query() -> JobQuery:
    ss = st.session_state
    return JobQuery(
        text=ss.get("f_text", ""),
        countries=tuple(ss.get("f_countries") or ()),
        roles=tuple(ss.get("f_roles") or ()),
        experience=tuple(ss.get("f_experience") or ()),
        work_modes=tuple(ss.get("f_modes") or ()),
        visa=tuple(ss.get("f_visa") or ()),
        posted_within_days=POSTED.get(ss.get("f_posted", "Any time")),
        min_score=float(ss["f_min_score"]) if ss.get("f_min_score") else None,
        min_salary=float(ss["f_salary"]) if ss.get("f_salary") else None,
        salary_currency=ss.get("f_currency"),
        include_no_salary=bool(ss.get("f_no_salary", True)),
        lists=tuple(ss.get("f_lists") or ()),
        include_filtered=bool(ss.get("f_include_filtered")),
        include_dismissed=bool(ss.get("f_include_dismissed")),
        hide_tracked=bool(ss.get("f_hide_tracked")),
        sort=SORTS.get(ss.get("f_sort", "Best fit"), "score"),
    )


def _clear() -> None:
    for key in FILTER_KEYS:
        st.session_state.pop(key, None)
    st.session_state.pop("chips", None)


def _chips(q: JobQuery) -> dict[str, tuple[str, Any]]:
    """Every active filter as a removable chip: label -> (session key, value)."""
    out: dict[str, tuple[str, Any]] = {}
    for c in q.countries:
        out[fmt.country(c)] = ("f_countries", c)
    for r in q.roles:
        out[fmt.ROLE_NAMES.get(r, r)] = ("f_roles", r)
    for e in q.experience:
        out[fmt.EXPERIENCE_NAMES.get(e, e)] = ("f_experience", e)
    for m in q.work_modes:
        out[fmt.MODE_NAMES.get(m, m)] = ("f_modes", m)
    for v in q.visa:
        out[fmt.VISA_NAMES.get(v, v)] = ("f_visa", v)
    if q.posted_within_days:
        out[f"Posted ≤ {st.session_state['f_posted'].lower()}"] = ("f_posted", None)
    if q.min_score:
        out[f"Fit ≥ {q.min_score:.0f}"] = ("f_min_score", None)
    if q.min_salary:
        out[f"Salary ≥ {q.salary_currency or ''} {q.min_salary:,.0f}"] = ("f_salary", None)
    for lst in q.lists:
        out["Target companies" if lst == "target" else "Motorsport list"] = ("f_lists", lst)
    if q.include_filtered:
        out["Incl. filtered out"] = ("f_include_filtered", None)
    if q.include_dismissed:
        out["Incl. dismissed"] = ("f_include_dismissed", None)
    if q.hide_tracked:
        out["Hiding your applications"] = ("f_hide_tracked", None)
    return out


def _on_chips() -> None:
    """A deselected chip removes its filter."""
    ss = st.session_state
    active = _chips(query())
    kept = set(ss.get("chips") or [])
    for label, (key, value) in active.items():
        if label in kept:
            continue
        if isinstance(ss.get(key), bool):
            ss[key] = False          # popping would restore a default that is on
        elif value is None:
            ss.pop(key, None)
        else:
            ss[key] = [v for v in (ss.get(key) or []) if v != value]
    ss.pop("chips", None)


def _labelled(options: dict[str, int], names: dict[str, str] | None = None,
              chosen: list[str] | None = None) -> list[str]:
    keys = list(options)
    for c in chosen or []:
        if c not in options:
            keys.append(c)
    return keys


def filters_popover(q: JobQuery) -> None:
    facets = data.facets(q)
    active = len(_chips(q))
    with st.popover(f"Filters{f' · {active}' if active else ''}", icon=":material/tune:",
                    width=135):
        left, right = st.columns(2, gap="medium")
        with left:
            counts = facets.get("countries", {})
            st.multiselect(
                "Countries", _labelled(counts, chosen=st.session_state.get("f_countries")),
                key="f_countries", placeholder="Any country",
                format_func=lambda c: f"{fmt.country(c)}  ({counts.get(c, 0):,})")
            counts_r = facets.get("roles", {})
            st.multiselect(
                "Kind of role", _labelled(counts_r, chosen=st.session_state.get("f_roles")),
                key="f_roles", placeholder="Any role",
                format_func=lambda r: f"{fmt.ROLE_NAMES.get(r, r)}  ({counts_r.get(r, 0):,})")
            counts_e = facets.get("experience", {})
            st.pills("Experience", list(fmt.EXPERIENCE_NAMES), selection_mode="multi",
                     key="f_experience",
                     format_func=lambda e: f"{fmt.EXPERIENCE_NAMES[e]} {counts_e.get(e, 0):,}")
            counts_m = facets.get("work_modes", {})
            st.pills("Work mode", list(fmt.MODE_NAMES), selection_mode="multi", key="f_modes",
                     format_func=lambda m: f"{fmt.MODE_NAMES[m]} {counts_m.get(m, 0):,}")
            counts_v = facets.get("visa", {})
            st.multiselect("Visa", list(fmt.VISA_NAMES), key="f_visa", placeholder="Any",
                           format_func=lambda v: f"{fmt.VISA_NAMES[v]}  ({counts_v.get(v, 0):,})")
        with right:
            posted = facets.get("posted", {})
            st.segmented_control(
                "Posted within", list(POSTED), key="f_posted",
                format_func=lambda p: p if POSTED[p] is None
                else f"{p} ({posted.get(str(POSTED[p]), 0):,})")
            st.slider("Minimum fit score", 0, 100, step=5, key="f_min_score",
                      help="0-100, from eight parts: role, experience, skills, location, "
                           "visa, direction, company and CV match. See How it works.")
            currencies = list(facets.get("currencies", {})) or ["EUR"]
            c1, c2 = st.columns([1, 2])
            c1.selectbox("Currency", currencies, key="f_currency")
            c2.number_input("Minimum salary (yearly)", min_value=0, step=5000, key="f_salary")
            st.checkbox("Keep jobs that state no salary", key="f_no_salary")
            lists = facets.get("lists", {})
            st.pills("Only companies on", ["target", "people"], selection_mode="multi",
                     key="f_lists",
                     format_func=lambda x: (f"My target list {lists.get('target', 0):,}"
                                            if x == "target"
                                            else f"Motorsport list {lists.get('people', 0):,}"))
            st.toggle("Include jobs my rules filtered out", key="f_include_filtered",
                      help="Hard rules — countries, seniority, visa, degree — from Settings.")
            st.toggle("Include dismissed jobs", key="f_include_dismissed")
            st.toggle("Hide jobs you selected, applied to or closed", key="f_hide_tracked",
                      help="Anything in Selected, Generate, Review or Applications — "
                           "including jobs marked Closed because the posting went down "
                           "before you applied. Jobs you removed from Selected still show.")
        st.button("Clear all filters", on_click=_clear, icon=":material/restart_alt:",
                  type="tertiary")


# ───────────────────────────── fetching ─────────────────────────────


@st.fragment(run_every=1.5)
def fetch_monitor() -> None:
    conn = data.conn()
    reg = runner.registry()
    row = conn.execute(
        "SELECT r.id, r.started_at, p.stage, p.message, p.current, p.total FROM runs r"
        " LEFT JOIN run_progress p ON p.run_id = r.id WHERE r.command = 'job-search'"
        " ORDER BY r.id DESC LIMIT 1").fetchone()
    if not runner.search_active():
        if reg.search is not None and reg.search.error:
            st.error(f"The fetch stopped: {reg.search.error}")
        st.rerun(scope="app")
        return
    with st.container(border=True):
        total, current = int((row and row["total"]) or 0), int((row and row["current"]) or 0)
        st.markdown(f"**Collecting new jobs** · run #{row['id'] if row else '…'}")
        st.progress(min(1.0, current / total) if total else 0.02,
                    text=(row["message"] if row and row["message"] else "starting…")[:160])
        if row:
            sources = conn.execute(
                "SELECT name, kind, jobs_found, jobs_new, duration_ms, last_error,"
                " CASE WHEN last_failure_at >= ? THEN 'failed' ELSE 'ok' END AS state"
                " FROM job_sources WHERE COALESCE(last_success_at, last_failure_at) >= ?"
                " ORDER BY COALESCE(last_success_at, last_failure_at) DESC LIMIT 60",
                (row["started_at"], row["started_at"])).fetchall()
            if sources:
                st.dataframe(pd.DataFrame([{
                    "Source": s["name"], "Found": s["jobs_found"], "New": s["jobs_new"],
                    "Time": fmt.seconds((s["duration_ms"] or 0) / 1000), "State": s["state"],
                } for s in sources]), hide_index=True, height=220)
        st.caption("Keep working — this runs in the background and the list refreshes when "
                   "it finishes.")


def fetch_bar(total: int) -> None:
    last = data.last_search_run()
    when = fmt.age(last["finished_at"] or last["started_at"]) if last else "never"
    with st.container(horizontal=True, vertical_alignment="center"):
        st.markdown(f"**{total:,} jobs** match · last collected {when}"
                    + (f" · {last['last_message']}" if last and last.get("last_message")
                       and last["status"] == "done" else ""), width="stretch")
        busy = runner.search_active()
        fetch = st.button("Fetch new jobs", icon=":material/refresh:", width="content",
                          disabled=busy, help="Polls every source now (1-15 minutes). The "
                          "daily 07:00 run does the same on its own.")
        if fetch and runner.start_search():
            time.sleep(0.5)
            st.rerun()
    if runner.search_active():
        fetch_monitor()


# ───────────────────────────── the detail ─────────────────────────────


def detail(job_id: int, ticked: list[int]) -> None:
    job = data.job(job_id)
    if job is None:
        st.caption("That job is no longer in the database.")
        return
    with st.container(border=True):
        if len(ticked) > 1:
            st.caption(f"Showing the first of {len(ticked)} ticked jobs")
        st.markdown(f"#### {job['title']}")
        st.markdown(f"**{job['company']}** · {fmt.where(job)} · posted "
                    f"{fmt.age(job.get('posted_at') or job.get('first_seen_at')) or 'date unknown'}")
        score = job.get("score")
        st.html(
            f'<div style="display:flex;flex-wrap:wrap;gap:0.3rem;align-items:center;margin:0.4rem 0 0.6rem">'
            f'<span class="ra-chip {components.score_color(score)}" style="font-size:1rem">'
            f'{score or 0:.0f} · {fmt.LABEL_NAMES.get(job.get("label") or "", "unscored")}</span>'
            f'<span class="ra-chip">{fmt.VISA_NAMES.get(job.get("visa_fit") or "unknown")}</span>'
            + (f'<span class="ra-chip">{html.escape(fmt.salary(job))}</span>' if fmt.salary(job) else "")
            + "</div>")
        b1, b2 = st.columns(2)
        if job.get("url"):
            b1.link_button("Open posting", job["url"], icon=":material/open_in_new:",
                           width="stretch")
        app = job.get("application")
        if app and app["status"] != "SKIPPED":
            b2.button(fmt.STATUS_NAMES.get(app["status"], app["status"]), disabled=True,
                      width="stretch", key=f"state_{job_id}")
        elif b2.button("Select", type="primary", icon=":material/add:", width="stretch",
                       key=f"sel_{job_id}"):
            data.write(tracker.select_jobs, [job_id])
            st.toast(f"Selected: {job['title']}")
            st.rerun()

        comps = job.get("components") or {}
        if comps:
            total_w = sum(float(c.get("weight") or 0) for c in comps.values()) or 1
            st.markdown("**Why this score**")
            rows = []
            for name, comp in comps.items():
                value = float(comp.get("value") or 0)
                weight = float(comp.get("weight") or 0)
                rows.append({"Part": fmt.COMPONENT_NAMES.get(name, name),
                             "Fit": value * 100, "Points": value * weight / total_w * 100,
                             "Note": comp.get("note") or comp.get("label") or ""})
            st.dataframe(pd.DataFrame(rows), hide_index=True, column_config={
                "Fit": st.column_config.ProgressColumn("Fit", min_value=0, max_value=100,
                                                       format="%.0f"),
                "Points": st.column_config.NumberColumn("Points", format="%.1f"),
            })
        plus = [str(r).lstrip("+ ").strip() for r in job.get("reasons_plus") or []]
        minus = [str(r).lstrip("?- ").strip() for r in job.get("reasons_minus") or []]
        if plus:
            st.html("<div class='ra-kicker'>For</div>" + components.chips(plus, "ok"))
        if minus:
            st.html("<div class='ra-kicker'>Watch out</div>" + components.chips(minus, "warn"))
        reasons = [r.get("message", "") if isinstance(r, dict) else str(r)
                   for r in job.get("filter_reasons") or []]
        if reasons:
            st.html("<div class='ra-kicker'>Filtered out because</div>"
                    + components.chips(reasons, "fail"))
        if job.get("matched_skills") or job.get("missing_skills") or job.get("missing_languages"):
            def nice(items: list[str]) -> list[str]:
                return [str(i).replace("_", " ") for i in items]

            st.html("<div class='ra-kicker'>Skills — yours · missing · languages</div>"
                    + components.chips(nice(job.get("matched_skills") or []), "accent")
                    + components.chips(nice(job.get("missing_skills") or []))
                    + components.chips(job.get("missing_languages") or [], "warn"))
        members = job.get("members") or []
        if len(members) > 1:
            with st.expander(f"Also posted on {len(members) - 1} other source(s)"):
                for m in members:
                    st.markdown(f"- [{m['source'] or m['kind']}]({m['url']}) · {m['status']}")
        with st.expander("The posting"):
            text = job.get("description") or "No description was captured."
            st.markdown(text[:6000] + ("…" if len(text) > 6000 else ""))
        n = len(ticked)
        with st.popover("Not interested" + (f" · {n} ticked" if n > 1 else ""),
                        icon=":material/block:", type="tertiary"):
            reason = st.selectbox("Why?", DISMISS_REASONS, key=f"why_{job_id}")
            st.caption("Gone from Search, Selected, Review and In process, and it will not "
                       "come back in a later search. Undo: Filters → Include dismissed, "
                       "then Select it.")
            if st.button(f"Dismiss {n} job(s)" if n > 1 else "Dismiss this job",
                         key=f"dismiss_{job_id}"):
                data.reject(ticked, reason)
                st.toast(f"Dismissed {n} job(s)." if n > 1 else "Dismissed.")
                st.rerun()


# ─────────────────────────────── the page ───────────────────────────────


_defaults()
components.header(
    "Search",
    "Every job collected from 13 job boards and your companies' career pages, scored "
    "0-100 against your CV. Filter, tick what you like, and send it to Selected.",
)

q = query()
with st.container(horizontal=True, vertical_alignment="bottom"):
    st.text_input("Search", key="f_text", placeholder="Role, company, skill or city — "
                  "e.g. reinforcement learning, Munich, Helsing", label_visibility="collapsed",
                  icon=":material/search:", width="stretch")
    filters_popover(q)
    st.selectbox("Sort", list(SORTS), key="f_sort", label_visibility="collapsed", width=150)
q = query()

chips = _chips(q)
if chips:
    st.pills("Active filters", list(chips), selection_mode="multi", default=list(chips),
             key="chips", on_change=_on_chips, label_visibility="collapsed")

limit = int(st.session_state["ra_limit"])
page = data.search(q, limit=limit)
fetch_bar(page.total)

if not page.rows:
    if page.total == 0 and not chips and not q.text:
        components.empty("No jobs collected yet.",
                         "Press Fetch new jobs — the first collection takes a few minutes.")
    else:
        components.empty("Nothing matches.", "Loosen a filter, or clear them all.")
    st.stop()

frame = pd.DataFrame([{
    "id": r["id"],
    "Fit": r["score"] or 0,
    "Role": r["title"],
    "Company": r["company"],
    "Where": fmt.where(r),
    "Posted": fmt.age(r.get("posted_at") or r.get("first_seen_at")),
    "Visa": fmt.VISA_NAMES.get(r.get("visa_fit") or "unknown", ""),
    "Salary": fmt.salary(r),
    "Status": fmt.STATUS_NAMES.get(r["application_status"], "") if r.get("application_status")
    else ("Filtered out" if not r["passed"] else ""),
    "Why": fmt.reasons(r.get("reasons_plus"), 2),
    "Link": r.get("url") or "",
} for r in page.rows])

table, side = st.columns([2.15, 1], gap="medium")
with table:
    event = st.dataframe(
        frame, key="results", on_select="rerun", selection_mode="multi-row", hide_index=True,
        height=min(620, 42 + 35 * len(frame)), column_order=["Fit", "Role", "Company", "Where", "Posted", "Visa", "Salary",
                                  "Status", "Why", "Link"],
        column_config={
            "Fit": st.column_config.ProgressColumn("Fit", min_value=0, max_value=100,
                                                   format="%.0f", width="small"),
            "Role": st.column_config.TextColumn("Role", width="large"),
            "Link": st.column_config.LinkColumn("Link", display_text="Open ↗", width="small"),
            "Why": st.column_config.TextColumn("Why it fits", width="large"),
        },
    )
    ticked = [int(frame.iloc[i]["id"]) for i in (event.selection.rows if event else [])]
    bar = st.columns([1.4, 0.9, 1.3, 1.2, 1.2], vertical_alignment="bottom")
    if bar[0].button(f"Select {len(ticked)} ticked" if ticked else "Tick rows to select",
                     type="primary", disabled=not ticked, icon=":material/playlist_add:",
                     width="stretch"):
        data.write(tracker.select_jobs, ticked)
        st.toast(f"{len(ticked)} job(s) moved to Selected")
        st.rerun()
    n = bar[1].number_input("Top N", min_value=1, max_value=1000, value=50, step=10,
                            key="top_n")
    if bar[2].button(f"Select the top {int(n)}", icon=":material/vertical_align_top:",
                     width="stretch", help="By the current sort, with every filter applied."):
        ids = data.top_ids(q, int(n))
        data.write(tracker.select_jobs, ids)
        st.toast(f"{len(ids)} job(s) moved to Selected")
        st.rerun()
    if page.total > limit and bar[3].button(f"Show {min(PAGE, page.total - limit)} more",
                                            width="stretch"):
        st.session_state["ra_limit"] = limit + PAGE
        st.rerun()
    bar[4].download_button(
        "These results", frame.drop(columns=["id"]).to_csv(index=False).encode("utf-8-sig"),
        file_name="rocketapply-search.csv", mime="text/csv", icon=":material/download:",
        width="stretch")
    st.caption(f"Showing {len(page.rows):,} of {page.total:,}. Fit is 0-100; hover a column "
               "header to sort within the page.")

with side:
    if ticked:
        detail(ticked[0], ticked)
    else:
        components.empty("Tick a job to see why it scored what it did.",
                         "The breakdown, the concerns, the posting itself — and Select.")
