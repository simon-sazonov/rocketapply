"""Every read RocketApply makes, and the one write lock.

The database is the source of truth for where each application is in the
pipeline; this module never keeps a second copy. Reads that cost more than a
few milliseconds (the filter counts, the budget) are cached briefly, keyed on
`version()`, which every write and every finished background run bumps — so a
change made in one tab shows in every other on its next rerun.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

import streamlit as st

from pitwall.config import Settings, get_settings
from pitwall.db import get_conn, init_schema, writing
from pitwall.jobsearch import store, tracker
from pitwall.jobsearch.store import JobQuery


@st.cache_resource
def _state() -> dict[str, Any]:
    """Process-wide: the data version every session reads, and which database
    files have had their schema checked."""
    return {"version": 0, "ready": set()}


def version() -> int:
    return _state()["version"]


def bump() -> None:
    _state()["version"] += 1


def settings() -> Settings:
    return get_settings()


def conn() -> sqlite3.Connection:
    """This thread's connection, schema guaranteed (a fresh install has no tables)."""
    c = get_conn(settings())
    key = str(settings().db_file)
    ready = _state()["ready"]
    if key not in ready:
        init_schema(c)
        ready.add(key)
    return c


def write(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """One write, under the process-wide lock, then everyone refreshes."""
    with writing():
        out = fn(conn(), *args, **kwargs)
    bump()
    return out


# ───────────────────────────── search ─────────────────────────────


def search(q: JobQuery, *, limit: int, offset: int = 0) -> store.SearchPage:
    return store.search_jobs(conn(), q, limit=limit, offset=offset)


@st.cache_data(ttl=120, show_spinner=False)
def _facets(q: JobQuery, _v: int) -> dict[str, dict[str, int]]:
    return store.search_facets(conn(), q)


def facets(q: JobQuery) -> dict[str, dict[str, int]]:
    return _facets(q, version())


def stage_counts() -> dict[str, int]:
    return store.stage_counts(conn())


def job(job_id: int) -> dict[str, Any] | None:
    return store.job_detail(conn(), job_id)


def top_ids(q: JobQuery, n: int) -> list[int]:
    return store.search_ids(conn(), q, limit=n)


# ─────────────────────────── applications ───────────────────────────


def applications(*statuses: str) -> list[dict[str, Any]]:
    return tracker.by_status(conn(), tuple(statuses))


def application(app_id: int) -> dict[str, Any] | None:
    return tracker.get(conn(), app_id)


def batches() -> list[dict[str, Any]]:
    return tracker.batches(conn())


def batch_items(batch_id: int) -> list[dict[str, Any]]:
    return tracker.batch_items(conn(), batch_id)


def last_search_run() -> dict[str, Any] | None:
    row = conn().execute(
        "SELECT r.*, p.message AS last_message FROM runs r"
        " LEFT JOIN run_progress p ON p.run_id = r.id"
        " WHERE r.command = 'job-search' ORDER BY r.id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


# ───────────────────────────── profile ─────────────────────────────


@st.cache_data(ttl=300, show_spinner=False)
def owner_name() -> str:
    """The profile's name — never hardcoded, so the app is not one person's."""
    try:
        from pitwall.apply.loader import load_profile

        return str(load_profile().personal.name or "")
    except Exception:  # noqa: BLE001 - no profile yet: the app still opens
        return ""


@st.cache_data(ttl=30, show_spinner=False)
def job_config_summary(_v: int) -> dict[str, Any]:
    from pitwall.jobsearch.settings import load_config

    cfg = load_config()
    return {
        "countries": list(cfg.locations.countries),
        "work_modes": list(cfg.locations.work_modes),
        "roles": list(cfg.roles.accepted),
        "max_years": cfg.seniority.max_experience_years,
    }
