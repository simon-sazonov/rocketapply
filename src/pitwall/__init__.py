"""PitWall — a motorsport networking lead finder.

Turns two CSV files (companies, roles) into a scored, filterable, note-taking
pipeline of real people you can reach out to. Built to the specification in
`PITWALL_PLAN.md`.

The pipeline runs in six stages, each writing to SQLite and each independently
resumable::

    query builder -> search -> extract & dedupe -> heuristics -> LLM -> dashboard

Entry points:

* ``python scripts/run_pipeline.py run`` — the CLI
* ``streamlit run app/Home.py`` — the dashboard
* :class:`pitwall.pipeline.Pipeline` — the orchestrator, if you want to embed it

Search-engine metadata only: PitWall never fetches LinkedIn profile pages, never
logs in, and has no outreach-sending capability by design (§1.3 of the plan).
"""

import sys

# noqa is deliberate: ruff calls this block outdated because target-version is
# py311, but that is the assumption this guard exists to check at RUNTIME.
if sys.version_info < (3, 11):  # noqa: UP036  # pragma: no cover
    # Checked here so the failure names itself. Without it the first module to
    # use `match` or `X | None` raises a SyntaxError from somewhere deep in the
    # package, which reads like a bug in PitWall rather than a wrong interpreter.
    raise RuntimeError(
        f"pitwall needs Python >= 3.11, running {sys.version.split()[0]} at "
        f"{sys.executable}. Use `.venv/bin/python`, or `make venv` to create it."
    )

__version__ = "2.0.0"

__all__ = ["__version__"]
