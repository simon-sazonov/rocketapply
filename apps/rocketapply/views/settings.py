"""Settings — what the search looks for, what it knows about you, who to contact.

The same files PitWall's Job Setup edits (`config/job_search.yaml`,
`config/my_skills.yaml`, `config/target_companies.csv`, `config/my_profile.md`),
so the two apps can never disagree while both exist. Plus RocketApply's own:
the roles to look for at an employer (`config/contact_roles.csv`), and a check
of the profile your CV and letters are built from (`profile/`).
"""

from __future__ import annotations

import csv
import io
import pathlib

import pandas as pd
import streamlit as st

from pitwall.config import config_dir
from pitwall.jobsearch.companies import (
    PRIORITIES,
    SPONSORSHIP_VALUES,
    TARGET_COLUMNS,
    TargetCompany,
    load_targets,
    save_targets,
    sync_targets,
)
from pitwall.jobsearch.locations import COUNTRIES, country_name
from pitwall.jobsearch.market import REGISTRY
from pitwall.jobsearch.people import GROUP_LABELS, GROUPS, contact_roles
from pitwall.jobsearch.queries import core, query_texts
from pitwall.jobsearch.roles import LABELS
from pitwall.jobsearch.settings import (
    EMPLOYMENT_TYPES,
    WORK_MODES,
    JobSearchConfig,
    config_path,
    load_config,
    save_config,
)
from pitwall.jobsearch.skills import load_my_skills, my_skills_path, save_my_skills, vocabulary
from pitwall.sources.jobs import ATS_KINDS
from rocket import components, data, runner
from rocket import format as fmt

CODES = sorted(COUNTRIES, key=country_name)


def _lines(text: str) -> list[str]:
    return [t.strip() for t in text.replace(",", "\n").splitlines() if t.strip()]


def _language_mismatch(cfg: JobSearchConfig) -> list[str]:
    """Levels the search profile and `profile/personal.yaml` disagree on. The
    search scores with the first; the CV and letters are written from the second."""
    from pitwall.apply.loader import load_profile
    from pitwall.jobsearch.scoring import LANGUAGE_NAMES

    try:
        spoken = load_profile().personal.languages or []
    except Exception:  # noqa: BLE001 - the Your documents tab reports a broken profile
        return []
    codes = {name.lower(): code for code, name in LANGUAGE_NAMES.items()}
    out = []
    for item in spoken:
        code = codes.get(str(item.get("name", "")).lower())
        mine = cfg.language_level(code) if code else None
        theirs = str(item.get("level", ""))
        if mine and mine.casefold() != theirs.casefold():
            out.append(f"{item.get('name')}: {mine} here, {theirs} in profile/personal.yaml")
    return out


def _saved_rerank() -> None:
    st.success("Saved. Re-score the stored jobs so Search reflects it (a few minutes, no "
               "network).", icon=":material/check:")
    if st.button("Re-score now", icon=":material/refresh:", disabled=runner.search_active()):
        runner.start_search(rerank=True)
        st.switch_page("views/search.py")


def profile_tab(cfg: JobSearchConfig) -> None:
    using = config_path()
    st.caption(f"Reading `{using.name}`" + (
        " — the example. Saving writes your own `job_search.yaml`."
        if using.name.endswith("example.yaml") else "."))
    s = data.settings()
    with st.form("profile"):
        st.markdown("#### Where")
        a, b = st.columns(2)
        countries = a.multiselect("Countries you would work in", CODES,
                                  default=cfg.locations.countries, format_func=country_name)
        home = b.selectbox("Where you live (decides which remote jobs are open to you)",
                           [None, *CODES], format_func=lambda c: country_name(c) or "—",
                           index=([None, *CODES].index(cfg.locations.home_country)
                                  if cfg.locations.home_country in CODES else 0))
        c, d, e = st.columns(3)
        preferred = c.text_area("Preferred cities", "\n".join(cfg.locations.preferred_cities),
                                height=110)
        excluded = d.text_area("Excluded cities", "\n".join(cfg.locations.excluded_cities),
                               height=110)
        modes = e.multiselect("Work modes", list(WORK_MODES), default=cfg.locations.work_modes)

        st.markdown("#### What")
        accepted = st.multiselect("Kinds of role that pass", list(LABELS),
                                  default=cfg.roles.accepted,
                                  format_func=lambda x: fmt.ROLE_NAMES.get(x, x))
        with st.expander("How much each kind of role is worth (0–1)"):
            cols = st.columns(4)
            preference = {
                label: cols[i % 4].slider(fmt.ROLE_NAMES.get(label, label), 0.0, 1.0,
                                          float(cfg.roles.preference.get(label, 0.0)), 0.05)
                for i, label in enumerate(LABELS)
            }
        titles = st.text_area("Titles that are exactly what you want (one per line)",
                              "\n".join(cfg.roles.preferred_titles), height=120)
        f, g = st.columns(2)
        max_years = f.slider("Filter out jobs that REQUIRE more than … years", 0.0, 10.0,
                             float(cfg.seniority.max_experience_years), 0.5)
        types = g.multiselect("Employment types", list(EMPLOYMENT_TYPES),
                              default=cfg.employment_types,
                              format_func=lambda t: t.replace("_", " "))
        h, i = st.columns(2)
        excluded_words = h.text_input("Title words that filter a job out (seniority)",
                                      ", ".join(cfg.seniority.excluded_title_words))
        allowed_phrases = i.text_input("…unless the title also has",
                                       ", ".join(cfg.seniority.allowed_title_phrases))

        st.markdown("#### Eligibility")
        j, k, m = st.columns(3)
        needs = j.toggle("I need visa sponsorship", value=cfg.work_authorization.needs_sponsorship)
        authorized = k.multiselect("Countries I can already work in", CODES,
                                   default=cfg.work_authorization.authorized_countries,
                                   format_func=country_name)
        degree = m.selectbox("Highest degree (finished or in progress)",
                             ["bachelors", "masters", "phd"],
                             index=["bachelors", "masters", "phd"].index(cfg.degree))
        langs = st.text_input("Languages, with your level", ", ".join(cfg.languages_spoken),
                              help="en, pt:B1, it:A2, uk:native — B2 and up count as yours, "
                                   "B1 half, A1–A2 as not spoken.")

        st.markdown("#### Direction and ranking")
        interests = st.text_area("Career interests — pull jobs up, never required",
                                 ", ".join(cfg.career_interests), height=70)
        avoid = st.text_input("Topics you avoid — marked down, never hidden",
                              ", ".join(cfg.avoid_topics))
        w = cfg.weights
        wcols = st.columns(8)
        weights = {
            name: wcols[n].number_input(fmt.COMPONENT_NAMES.get(name, name), 0.0, 100.0,
                                        float(getattr(w, name)), 1.0)
            for n, name in enumerate(("role", "experience", "skills", "location", "visa",
                                      "career", "company", "fit"))
        }

        st.markdown("#### Sources")
        s1, s2, s3, s4 = st.columns(4)
        target_ats = s1.toggle("Target companies' boards", value=cfg.sources.target_ats)
        people_ats = s1.toggle("Motorsport teams' boards", value=cfg.sources.people_list_ats)
        an_on = s2.toggle("Arbeitnow", value=cfg.sources.arbeitnow.enabled)
        an_pages = s2.number_input("Arbeitnow pages (×250)", 1, 20, cfg.sources.arbeitnow.max_pages)
        az_on = s3.toggle("Adzuna", value=cfg.sources.adzuna.enabled)
        paces = ["auto", "burst", "even"]
        az_pace = s3.selectbox("Adzuna pace", paces, index=paces.index(cfg.sources.adzuna.pace))
        cache_hours = s4.number_input("Reuse fetched pages for (hours)", 0.0, 168.0,
                                      float(cfg.sources.cache_hours))
        ai_top = s4.number_input("AI second opinion on top N (0 = off)", 0, 200, cfg.ai_fit_top_n)
        market_on: dict[str, bool] = {}
        mcols = st.columns(4)
        for n, spec in enumerate(REGISTRY):
            conf = getattr(cfg.sources, spec.key)
            missing = [env for attr, env in zip(spec.needs, spec.env, strict=True)
                       if not getattr(s, attr, "")]
            market_on[spec.key] = mcols[n % 4].toggle(
                f"{spec.label} · {spec.covers}", value=conf.enabled,
                help=(f"Needs {', '.join(missing)} in .env") if missing else None)
        generated = query_texts(cfg)
        st.caption("Searches built from your roles and titles: " + ", ".join(generated))
        extra_queries = st.text_input("Extra searches (comma-separated)",
                                      ", ".join(q for q in cfg.sources.queries
                                                if core(q) not in generated))
        saved = st.form_submit_button("Save", type="primary")
    for line in _language_mismatch(cfg):
        st.warning(f"{line}. The search uses the first, your CV and letters the second — "
                   "change both so they agree.", icon=":material/translate:")
    if saved:
        d = cfg.model_dump()
        d["locations"].update(countries=countries, home_country=home,
                              preferred_cities=_lines(preferred),
                              excluded_cities=_lines(excluded), work_modes=modes)
        d["roles"].update(accepted=accepted, preference=preference,
                          preferred_titles=[t.strip() for t in titles.splitlines() if t.strip()])
        d["seniority"].update(max_experience_years=max_years,
                              excluded_title_words=_lines(excluded_words),
                              allowed_title_phrases=_lines(allowed_phrases))
        d["employment_types"] = types
        d["work_authorization"].update(needs_sponsorship=needs, authorized_countries=authorized)
        d["degree"] = degree
        d["languages_spoken"] = _lines(langs)
        d["career_interests"] = _lines(interests)
        d["avoid_topics"] = _lines(avoid)
        d["weights"] = weights
        d["sources"].update(target_ats=target_ats, people_list_ats=people_ats,
                            cache_hours=cache_hours)
        d["sources"]["arbeitnow"].update(enabled=an_on, max_pages=int(an_pages))
        d["sources"]["adzuna"].update(enabled=az_on, pace=az_pace)
        d["sources"]["queries"] = _lines(extra_queries)
        for key, on in market_on.items():
            d["sources"][key]["enabled"] = on
        d["ai_fit_top_n"] = int(ai_top)
        try:
            save_config(JobSearchConfig.model_validate(d))
        except Exception as exc:  # noqa: BLE001 - pydantic's message is the useful part
            st.error(f"Not saved: {exc}")
        else:
            st.session_state["profile_saved"] = True
    if st.session_state.pop("profile_saved", False):
        _saved_rerank()


def cv_tab() -> None:
    from pitwall.scoring.jobs import PERSONAL_PROFILE_FILE

    path = config_dir() / PERSONAL_PROFILE_FILE
    st.markdown("The **AI second opinion** on the best jobs reads this text beside each "
                "posting. The score itself never uses it — only your profile and skills. "
                "Leave out phone, email and address; the AI does not need them.")
    text = st.text_area("Your CV and goals, in plain text",
                        path.read_text(encoding="utf-8") if path.exists() else "", height=380)
    if st.button("Save", type="primary", key="save_cv"):
        path.write_text(text.strip() + "\n", encoding="utf-8")
        st.toast(f"Saved to config/{PERSONAL_PROFILE_FILE}")


def skills_tab() -> None:
    vocab = vocabulary()
    owned, warnings = load_my_skills(vocab=vocab)
    st.caption(f"`{my_skills_path().name}` — a skill a posting names and this list does not "
               "is shown as missing. List what you could defend in an interview.")
    for w in warnings:
        st.warning(w)
    rows = [{"skill": vocab.label(s.id), "proficiency": s.proficiency, "years": s.years,
             "evidence": "; ".join(s.evidence)} for s in owned.values()]
    edited = st.data_editor(
        pd.DataFrame(rows, columns=["skill", "proficiency", "years", "evidence"]),
        num_rows="dynamic", hide_index=True, width="stretch", key="skills-editor",
        column_config={
            "skill": st.column_config.SelectboxColumn(
                "Skill", options=sorted({sk.label for sk in vocab.skills.values()}), required=True),
            "proficiency": st.column_config.SelectboxColumn(
                "Proficiency", options=[1, 2, 3], required=True,
                help="1 touched it · 2 working knowledge · 3 strong"),
            "years": st.column_config.NumberColumn("Years", min_value=0, max_value=40),
            "evidence": st.column_config.TextColumn("Evidence", width="large"),
        })
    if st.button("Save skills", type="primary"):
        save_my_skills(edited.to_dict("records"))
        st.session_state.pop("skills-editor", None)
        st.session_state["profile_saved"] = True
        st.rerun()


def targets_tab() -> None:
    targets, report = load_targets()
    st.caption("Employers you want to watch: their postings get a priority bonus, and those "
               "with a public job board are read directly.")
    for issue in report.issues:
        (st.error if issue.level == "error" else st.warning)(f"Row {issue.row}: {issue.message}")
    with st.expander("Upload a company list", expanded=not targets):
        upload = st.file_uploader("A CSV with a name column, or one company per line",
                                  type=["csv", "txt"], key="targets-upload")
        mode = st.radio("How", ["Add to my list", "Replace my list"], horizontal=True)
        if upload is not None and st.button("Import", type="primary"):
            from pitwall.jobsearch.importer import merge, parse_upload

            incoming, problems = parse_upload(upload.getvalue())
            for p in problems:
                st.warning(p)
            if incoming:
                result = merge(targets, incoming, replace=mode.startswith("Replace"))
                save_targets(result.companies)
                data.write(sync_targets, load_targets()[0])
                st.session_state.pop("targets-editor", None)
                st.success(f"{len(result.added)} added, {len(result.updated)} updated.")
                st.rerun()
    frame = pd.DataFrame([t.as_row() for t in targets], columns=list(TARGET_COLUMNS))
    frame["active"] = frame["active"].eq("true")
    edited = st.data_editor(
        frame, num_rows="dynamic", hide_index=True, width="stretch", key="targets-editor",
        column_config={
            "name": st.column_config.TextColumn("Company", required=True),
            "priority": st.column_config.SelectboxColumn("Priority", options=list(PRIORITIES),
                                                         required=True, width="small"),
            "careers_url": st.column_config.LinkColumn("Careers page"),
            "ats_kind": st.column_config.SelectboxColumn("Board", options=["", *ATS_KINDS]),
            "sponsorship": st.column_config.SelectboxColumn(
                "Sponsors?", options=list(SPONSORSHIP_VALUES), width="small"),
            "active": st.column_config.CheckboxColumn("Active", width="small"),
        })
    if st.button("Save target companies", type="primary"):
        new = [
            TargetCompany(
                name=str(r["name"]).strip(),
                aliases=[a.strip() for a in str(r["aliases"]).split("|") if a.strip()],
                country=str(r["country"]).strip() or None,
                cities=[c.strip() for c in str(r["cities"]).split("|") if c.strip()],
                industry=str(r["industry"]).strip() or None, priority=str(r["priority"] or "B"),
                careers_url=str(r["careers_url"]).strip() or None,
                ats_kind=str(r["ats_kind"]).strip() or None,
                ats_token=str(r["ats_token"]).strip() or None,
                sponsorship=str(r["sponsorship"] or "unknown"),
                visa_notes=str(r["visa_notes"]).strip() or None,
                tags=[t.strip() for t in str(r["tags"]).split("|") if t.strip()],
                active=bool(r["active"]),
            )
            for r in edited.fillna("").to_dict("records") if str(r["name"]).strip()
        ]
        save_targets(new)
        data.write(sync_targets, load_targets()[0])
        st.session_state.pop("targets-editor", None)
        st.session_state["profile_saved"] = True
        st.rerun()


def contacts_tab() -> None:
    st.markdown("Who **Find people** looks for at an employer you applied to. Peers are found "
                "from each job's own title, and alumni from the schools and past employers in "
                "your profile; these rows are the recruiters and hiring managers.")
    rows = contact_roles()
    frame = pd.DataFrame([{"title": r.title, "aliases": "|".join(r.aliases), "family": r.family,
                           "group": r.group, "priority": r.priority, "active": True} for r in rows])
    edited = st.data_editor(
        frame, num_rows="dynamic", hide_index=True, width="stretch", key="contacts-editor",
        column_config={
            "title": st.column_config.TextColumn("Title", required=True),
            "aliases": st.column_config.TextColumn("Also called", help="pipe-separated",
                                                   width="large"),
            "group": st.column_config.SelectboxColumn("Group", options=list(GROUPS),
                                                      format_func=lambda g: GROUP_LABELS[g]),
            "family": st.column_config.SelectboxColumn(
                "Family", options=["operations", "leadership", "data_science", "software",
                                   "data_engineering"]),
            "priority": st.column_config.NumberColumn("Priority", min_value=1, max_value=3),
            "active": st.column_config.CheckboxColumn("Active"),
        })
    if st.button("Save", type="primary", key="save_contacts"):
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["title", "aliases", "family", "group",
                                                 "priority", "active"])
        writer.writeheader()
        for r in edited.fillna("").to_dict("records"):
            if str(r["title"]).strip():
                writer.writerow({**r, "active": "true" if r["active"] else "false"})
        (config_dir() / "contact_roles.csv").write_text(buf.getvalue(), encoding="utf-8")
        st.toast("Saved config/contact_roles.csv")


def documents_tab() -> None:
    from pitwall.apply.validate import validate

    st.markdown("Your CV and letters are built only from `profile/` — every line on the CV is "
                "a record there, and every claim in a letter must trace to one. This checks it.")
    report = validate()
    st.html(components.chips(
        [f"{len(report.errors)} error(s)" + (" — must be fixed" if report.errors else "")],
        "fail" if report.errors else "ok")
        + components.chips([f"{len(report.warnings)} warning(s)"],
                           "warn" if report.warnings else "")
        + components.chips([f"{len(report.allowed_numbers)} numbers a letter may use"]))
    for e in report.errors:
        st.error(e)
    if report.warnings:
        with st.expander(f"{len(report.warnings)} warning(s)"):
            for w in report.warnings:
                st.markdown(f"- {w}")
    from pitwall.apply.history import scan

    latest = next((g for g in scan("output") if g.cv_pdf), None)
    if latest:
        with st.container(border=True):
            thumb, info = st.columns([1, 4], vertical_alignment="center")
            with thumb:
                components.pdf_preview(latest.cv_pdf, caption=" ", width=150)
            with info:
                pdf = pathlib.Path(latest.cv_pdf)
                st.markdown(f"**The most recent CV** · {latest.company} · {latest.title}")
                st.caption(pdf.name)
                st.download_button("Open the PDF", pdf.read_bytes(), file_name=pdf.name,
                                   mime="application/pdf", icon=":material/picture_as_pdf:")


components.header(
    "Settings",
    "What the search looks for, what it knows about you, and who to contact. Shared with "
    "PitWall's Job Setup while both exist.",
)
try:
    cfg = load_config()
except Exception as exc:  # noqa: BLE001 - show the problem, still show the form
    st.error(f"The profile file is invalid, so defaults are shown: {exc}")
    cfg = JobSearchConfig()
tabs = st.tabs(["Search profile", "CV for the AI", "Skills", "Target companies",
                "Who to contact", "Your documents"])
with tabs[0]:
    profile_tab(cfg)
with tabs[1]:
    cv_tab()
with tabs[2]:
    skills_tab()
with tabs[3]:
    targets_tab()
with tabs[4]:
    contacts_tab()
with tabs[5]:
    documents_tab()
