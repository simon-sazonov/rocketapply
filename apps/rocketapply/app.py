"""RocketApply — find the jobs, make the documents, apply, then reach the people.

`make rocketapply` (streamlit run apps/rocketapply/app.py), from the repo root:
the engine writes `output/`, `run/` and `data/` relative to the working
directory, and this app shares them with PitWall. Its light theme is in
`.streamlit/config.toml` beside this file.

One process, left to right, with the count at every step on the strip under
the navigation:

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

from rocket import components, theme  # noqa: E402

st.set_page_config(page_title="RocketApply", page_icon="🚀", layout="wide",
                   initial_sidebar_state="collapsed")
theme.inject()

PAGES = [
    st.Page("views/search.py", title="Search", icon=":material/search:", url_path="search",
            default=True),
    st.Page("views/selected.py", title="Selected", icon=":material/checklist:",
            url_path="selected"),
    st.Page("views/generate.py", title="Generate", icon=":material/bolt:", url_path="generate"),
    st.Page("views/review.py", title="Review", icon=":material/rate_review:", url_path="review"),
    st.Page("views/applications.py", title="Applications", icon=":material/send:",
            url_path="applications"),
    st.Page("views/engine.py", title="How it works", icon=":material/insights:",
            url_path="how-it-works"),
    st.Page("views/settings.py", title="Settings", icon=":material/tune:", url_path="settings"),
]

page = st.navigation(PAGES, position="top")
components.process_strip()
page.run()
