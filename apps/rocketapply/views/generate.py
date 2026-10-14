"""Generate — watch every application being made, stage by stage.

Not a spinner: each job shows what each stage produced as it produces it —
the skills the posting really asks for (with the quote that proves it), how
much of that your profile covers, the claims you may not make, which projects
the CV leads with and why, the letter's plan, every draft of the letter as the
model writes it, and the verdict on each sentence. Nothing pauses for you;
everything lands in Review.
"""

from __future__ import annotations

import html
import time

import streamlit as st

from pitwall.apply.budget import MODE_LABELS
from pitwall.apply.pipeline import STAGES
from rocket import components, data, runner
from rocket import format as fmt
from rocket.runner import STAGE_KEYS, GenerationRun, JobTrack

LABELS = dict(STAGES)
STATUS_ICON = {"queued": "⏳", "running": "⚙️", "ready": "✅", "review": "🟡",
               "blocked": "⛔", "error": "❗", "skipped": "⏭"}
STATUS_WORD = {"ready": "ready to send", "review": "needs a look", "blocked": "do not send",
               "error": "failed", "skipped": "skipped", "running": "working", "queued": "queued"}


def _esc(value: object) -> str:
    return html.escape(str(value or ""))


def _cap(text: str) -> str:
    """"rendering the CV" → "Rendering the CV" (str.capitalize lowercases the rest)."""
    return text[:1].upper() + text[1:]


def stage_summary(key: str, state: str, out: dict) -> str:
    """One line per stage: what it produced, in words."""
    if state == "skip":
        return f"<span class='ra-faint'>skipped — {_esc(out.get('reason') or 'not needed')}</span>"
    if state == "start":
        return "<span class='ra-dim'>working…</span>"
    if state == "error":
        return f"<span style='color:var(--ra-fail)'>{_esc(out.get('error'))}</span>"
    if state != "done":
        return ""
    if key == "analyse":
        req = out.get("required") or []
        chips = "".join(
            f"<span class='ra-chip accent' title='{_esc(r.get('quote'))}'>{_esc(r.get('term'))}</span>"
            for r in req)
        extra = " · read from cache" if out.get("cached") else ""
        gates = out.get("hard_gates") or []
        return (f"{len(req)} required skill(s), each backed by a quote from the posting "
                f"(hover){extra}.<br>{chips}"
                + (f"<br><span class='ra-dim'>Hard requirements: {_esc('; '.join(gates))}</span>"
                   if gates else ""))
    if key == "match":
        missing = [m.get("requirement") for m in out.get("missing") or []]
        return (f"Covers <b>{out.get('coverage_required', 0):.0%}</b> of the required and "
                f"<b>{out.get('coverage_preferred', 0):.0%}</b> of the preferred skills"
                + (" — a stretch role" if out.get("stretch") else "") + "."
                + (f"<br>Not in your profile: {components.chips(missing[:8])}" if missing else ""))
    if key == "truth":
        forbidden = out.get("forbidden") or []
        return (f"{out.get('forbidden_count', 0)} claim(s) the letter may not make."
                + (f"<br>{components.chips(forbidden[:8], 'fail')}" if forbidden else ""))
    if key == "select":
        projects = out.get("projects") or []
        rows = "".join(f"<br>· <b>{_esc(p.get('name'))}</b> <span class='ra-faint'>"
                       f"{_esc(p.get('why'))}</span>" for p in projects)
        return f"{len(projects)} project(s) on the CV:{rows}"
    if key == "render":
        return (f"{out.get('pages') or '?'} page · PDF and DOCX, identical content"
                + (f"<br><span style='color:var(--ra-warn)'>{_esc('; '.join(out.get('warnings') or []))}</span>"
                   if out.get("warnings") else ""))
    if key == "plan":
        urls = out.get("company_urls") or []
        return (f"Theme: <b>{_esc(out.get('main_theme'))}</b>"
                + (f"<br>Angle on the company: {_esc(out.get('company_angle'))}"
                   if out.get("company_angle") else "")
                + (f"<br>Opening: {_esc(out.get('opening_strategy'))}"
                   if out.get("opening_strategy") else "")
                + (f"<br><span class='ra-faint'>Company facts from: {_esc(', '.join(urls))}</span>"
                   if urls else ""))
    if key == "write":
        text = f"{out.get('words', 0)} words after {out.get('attempts', 0)} draft(s)"
        if "sentences" in out:
            text += (f" · {out.get('supported', 0)} of {out.get('sentences', 0)} sentences "
                     f"supported by your profile"
                     + (" (independent check)" if out.get("independent") else ""))
        flagged = out.get("flagged") or []
        if flagged:
            text += "<br>Questioned: " + components.chips(
                [f"{f.get('verdict')}: {f.get('span')}" for f in flagged], "warn")
        repairs = out.get("repairs") or []
        if repairs:
            text += "<br><span class='ra-faint'>Repaired: " + _esc(
                "; ".join(r.get("what") or r.get("code") for r in repairs)) + "</span>"
        return text
    if key == "package":
        state = out.get("letter_state") or ""
        kind = {"clean": "ok", "review": "warn", "unsent": "fail"}.get(state, "accent")
        files = [p for p in ("cv_pdf", "letter_pdf", "letter_txt") if out.get(p)]
        return (components.chips([fmt.STATE_NAMES.get(state, state)], kind)
                + " " + _esc(", ".join(f.replace("_", " ").upper() for f in files)))
    return ""


def job_panel(job: JobTrack, expanded: bool) -> None:
    icon = STATUS_ICON.get(job.status, "•")
    score = f"{job.score:.0f} · " if job.score else ""
    tail = f" — {STATUS_WORD.get(job.status, job.status)}"
    if job.status not in ("queued", "running"):
        tail += f" · {fmt.seconds(job.seconds)} · {fmt.tokens(job.tokens)} tokens"
    with st.expander(f"{icon} {score}{job.company} · {job.title}{tail}", expanded=expanded):
        st.html(components.stage_track(job.stages, STAGE_KEYS)
                + (f"<span class='ra-dim' style='margin-left:0.6rem;font-size:0.85rem'>"
                   f"{_esc(job.current)}</span>" if job.current else ""))
        left, right = st.columns([1.7, 1], gap="medium")
        with left:
            rows = []
            for key in STAGE_KEYS:
                state = job.stages.get(key, "")
                if not state:
                    continue
                dot = {"done": "done", "start": "run", "skip": "skip", "error": "error"}[state]
                rows.append(
                    f"<div class='ra-stage'><span class='ra-dot {dot}'></span>"
                    f"<span class='what'>{_esc(_cap(LABELS.get(key, key)))}</span>"
                    f"<span class='out'>{stage_summary(key, state, job.out.get(key, {}))}</span>"
                    f"</div>")
            st.html("".join(rows) or "<span class='ra-faint'>Waiting for a free worker…</span>")
            if job.waits:
                st.caption("Rate limit: " + job.waits[-1])
            reasons = job.row.get("reasons") or []
            if reasons and job.status in ("review", "blocked", "error"):
                st.html("<div class='ra-kicker' style='margin-top:0.6rem'>Why it needs a look"
                        "</div>" + components.chips([str(r) for r in reasons[:6]], "warn"))
        with right:
            draft = job.drafts[-1] if job.drafts else None
            final = (job.out.get("write") or {}).get("text")
            if final or draft:
                label = ("The letter" if final else
                         f"Draft · round {draft.get('round')}, attempt {draft.get('attempt')}")
                st.markdown(f"**{label}**")
                st.html(f"<div class='ra-letter'>{_esc(final or draft.get('text'))}</div>")
            pdf = (job.out.get("package") or {}).get("cv_pdf")
            if pdf:
                components.pdf_preview(pdf, caption="The CV")


def live(run: GenerationRun) -> None:
    with run.lock:
        jobs = [run.jobs[j] for j in run.job_ids if j in run.jobs]
        research = list(run.research)
    done = [j for j in jobs if j.status not in ("queued", "running")]
    active = [j for j in jobs if j.status == "running"]
    queued = [j for j in jobs if j.status == "queued"]
    elapsed = (run.finished or time.time()) - run.started
    eta = run.eta_seconds(3)

    c = st.columns(5)
    with c[0]:
        components.card("Done", f"{len(done)} / {len(jobs)}", MODE_LABELS.get(run.mode, run.mode))
    with c[1]:
        components.card("Ready to send", str(sum(j.status == "ready" for j in done)),
                        f"{sum(j.status == 'review' for j in done)} need a look")
    with c[2]:
        components.card("Tokens", fmt.tokens(run.tokens), "spent on uncached model calls")
    with c[3]:
        components.card("Time", fmt.seconds(elapsed),
                        f"about {fmt.seconds(eta)} left" if run.alive and eta else run.status)
    with c[4]:
        components.card("Problems", str(sum(j.status in ("blocked", "error") for j in done)),
                        "blocked or failed")
    st.progress(len(done) / max(1, len(jobs)))

    if run.status == "interrupted":
        st.warning("This run stopped before it finished. Finished jobs are kept; resume to "
                   "make the rest.", icon=":material/pause_circle:")
        if st.button("Resume", type="primary", icon=":material/play_arrow:"):
            runner.start_generation(run.job_ids, mode=run.mode, hints=run.hints, resume_of=run,
                                    workers=getattr(run, "workers", 3))
            st.rerun()
    if run.error:
        st.error(run.error)
    if research:
        with st.expander(f"Company research before the letters · {len(research)} employer(s)"):
            for line in research:
                st.markdown(f"- {line}")

    for job in active:
        job_panel(job, expanded=True)
    for job in sorted(done, key=lambda j: {"blocked": 0, "error": 1, "review": 2}.get(j.status, 3)):
        job_panel(job, expanded=len(jobs) <= 3)
    if queued:
        st.caption("Queued: " + " · ".join(f"{j.company} ({j.title[:40]})" for j in queued[:20])
                   + (f" … and {len(queued) - 20} more" if len(queued) > 20 else ""))

    if not run.alive and run.status != "interrupted":
        st.success(f"Finished: {len(done)} job(s). Read, edit and approve them in Review.",
                   icon=":material/task_alt:")
        st.page_link("views/review.py", label="Go to Review", icon=":material/rate_review:")


def _read_posting(url: str, pasted: str) -> None:
    """Read the link (or the pasted text) into `intake_posting`, or say why not."""
    from pitwall.apply import intake

    st.session_state.pop("intake_posting", None)
    st.session_state.pop("intake_error", None)
    try:
        with st.spinner("Reading the posting…"):
            posting = (intake.from_text(pasted, url) if pasted.strip()
                       else intake.fetch_posting(url))
    except intake.IntakeError as exc:
        st.session_state["intake_error"] = str(exc)
        # The toggle is already drawn this run; it opens on the next one.
        st.session_state["intake_open_paste"] = True
        st.rerun()
    st.session_state["intake_posting"] = posting
    for key in ("title", "company", "location", "description"):
        st.session_state[f"intake_{key}"] = getattr(posting, key)


def _application_status(job_id: int) -> str:
    from pitwall.jobsearch import tracker

    cluster = tracker.cluster_for_job(data.conn(), job_id)
    app = tracker.for_cluster(data.conn(), int(cluster["cluster_id"])) if cluster else None
    return str((app or {}).get("status") or "")


def add_from_link(*, open_: bool) -> None:
    """A job found somewhere else: paste its link (or its text), check what was
    read, and generate it like any other."""
    from pitwall.apply import intake

    with st.expander("Add a job you found elsewhere", expanded=open_,
                     icon=":material/add_link:"):
        st.caption("Paste the posting's link. If the site will not be read (LinkedIn, a login "
                   "wall, a page built by JavaScript), paste the posting's text instead. A "
                   "posting in Ukrainian or any other language is fine: the CV and the letter "
                   "are written in English.")
        url = st.text_input("Link to the posting", key="intake_url",
                            placeholder="https://…")
        pasted = ""
        if st.session_state.pop("intake_open_paste", False):
            st.session_state["intake_paste"] = True
        if st.toggle("Paste the text instead", key="intake_paste"):
            pasted = st.text_area("The posting's text", key="intake_text", height=220,
                                  placeholder="Select everything in the posting, copy, paste.")
        if st.button("Read the posting", icon=":material/travel_explore:",
                     disabled=not (url.strip() or pasted.strip())):
            _read_posting(url, pasted)
        if st.session_state.get("intake_error"):
            st.warning(_cap(st.session_state["intake_error"]) + ".",
                       icon=":material/link_off:")
        posting = st.session_state.get("intake_posting")
        if posting is None:
            return

        lang = intake.LANGUAGE_NAMES.get(posting.language, posting.language or "unknown")
        st.html(f"<span class='ra-dim'>Read from the {_esc(posting.method)} · posting in "
                f"<b>{_esc(lang)}</b> · CV and letter in <b>English</b></span>")
        for note in posting.notes:
            st.caption("⚠️ " + _cap(note))
        a, b, c = st.columns([1.4, 1, 1])
        posting.title = a.text_input("Job title", key="intake_title")
        posting.company = b.text_input("Employer", key="intake_company")
        posting.location = c.text_input("Where", key="intake_location")
        posting.description = st.text_area(
            "The posting, as the engine will read it", key="intake_description", height=260,
            help="Kept in the posting's own language: every skill the engine finds is checked "
                 "against these exact words. Cut anything that is not the posting.")
        missing = posting.missing
        if missing:
            st.caption("Still missing: " + ", ".join(missing))
        m, go, save = st.columns([1.4, 1, 1], vertical_alignment="bottom")
        mode = m.selectbox("Documents", ["cv_letter", "cv", "cv_letter_audit"],
                           format_func=lambda k: MODE_LABELS[k], key="intake_mode")
        busy = runner.generation_active()
        if go.button("Generate now", type="primary", icon=":material/bolt:",
                     disabled=bool(missing) or busy, width="stretch",
                     help="Another generation is running; save it to Selected for now."
                     if busy else None):
            job_id = data.write(intake.add_posting, posting)
            started = runner.start_generation([job_id], mode=mode, workers=1)
            if started is None:
                st.warning("Saved to Selected, but it could not start: another generation "
                           "is running.")
            else:
                st.session_state.pop("intake_posting", None)
                st.rerun()
        if save.button("Save to Selected", icon=":material/checklist:",
                       disabled=bool(missing), width="stretch"):
            job_id = data.write(intake.add_posting, posting)
            st.session_state.pop("intake_posting", None)
            status = _application_status(job_id)
            if status in ("", "SHORTLISTED"):
                st.success(f"{posting.company} · {posting.title} is on Selected.")
            else:
                st.info(f"{posting.company} · {posting.title} was added before and is already "
                        f"past Selected ({status.replace('_', ' ').lower()}); its posting text "
                        "was refreshed.")


components.header(
    "Generate",
    "Each application, stage by stage: what the model read in the posting, what your profile "
    "covers, what it may not claim, the CV it chose, the letter as it is written, and the "
    "checks it passed.",
)
run = runner.registry().generation
runner.sync(run)
add_from_link(open_=run is None or not run.alive)
if run is None:
    components.empty("Nothing generated yet.",
                     "Choose jobs on Selected and press Generate, or add a job from a link "
                     "above. This page then shows the work.")
    st.page_link("views/selected.py", label="Go to Selected", icon=":material/checklist:")
    st.stop()


@st.fragment(run_every=1.0)
def watch() -> None:
    current = runner.registry().generation
    if current is None:
        return
    runner.sync(current)
    was_alive = st.session_state.get("gen_alive", False)
    live(current)
    st.session_state["gen_alive"] = current.alive
    if was_alive and not current.alive:
        st.rerun(scope="app")      # refresh the counts in the navigation


if run.alive:
    watch()
else:
    live(run)
