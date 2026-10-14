"""RocketApply — find the jobs, make the documents, apply, then reach the people.

`make rocketapply` (streamlit run apps/rocketapply/app.py), from the repo root:
the engine writes `output/`, `run/` and `data/` relative to the working
directory, and this app shares them with PitWall. Its light theme is in
`.streamlit/config.toml` beside this file.

One process, left to right, and the navigation is that process — each page
carries the number of jobs waiting there, and a job's status anywhere in the
app is the name of the page it is on:

    Search → Selected → Generate → Review → Applications (in process → applied → people)

plus "How it works" (sources, scoring, the model at each stage, how good the
output is) and Settings.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import streamlit as st  # noqa: E402

from rocket import components, data, runner, theme  # noqa: E402

st.set_page_config(page_title="RocketApply", page_icon="🚀", layout="wide",
                   initial_sidebar_state="collapsed")
theme.inject()

try:
    counts = data.stage_counts()
except Exception:  # noqa: BLE001 - a fresh install with no jobs yet
    counts = {}
gen = runner.registry().generation
runner.sync(gen)
titles = components.nav_titles(
    counts, (len(gen.finished_jobs), len(gen.job_ids)) if gen is not None and gen.alive else None)

PAGES = [
    st.Page("views/search.py", title=titles["search"], icon=":material/search:",
            url_path="search", default=True),
    st.Page("views/selected.py", title=titles["selected"], icon=":material/checklist:",
            url_path="selected"),
    st.Page("views/generate.py", title=titles["generate"], icon=":material/bolt:",
            url_path="generate"),
    st.Page("views/review.py", title=titles["review"], icon=":material/rate_review:",
            url_path="review"),
    st.Page("views/applications.py", title=titles["applications"], icon=":material/send:",
            url_path="applications"),
    st.Page("views/engine.py", title="How it works", icon=":material/insights:",
            url_path="how-it-works"),
    st.Page("views/settings.py", title="Settings", icon=":material/tune:", url_path="settings"),
]

page = st.navigation(PAGES, position="top")
page.run()
