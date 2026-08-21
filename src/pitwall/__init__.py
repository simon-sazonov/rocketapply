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

__version__ = "1.0.0"

__all__ = ["__version__"]
