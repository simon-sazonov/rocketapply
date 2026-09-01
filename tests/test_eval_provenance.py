"""The eval set's provenance guard — §1.3.

`tests/fixtures/eval_set.csv` is 60 rows hand-labelled on `ddgs` SERP shapes.
SearXNG's `content` field is not DuckDuckGo's `body`, so the same person reads
differently to the heuristic layer and the labels do not transfer. A threshold
swept against them is a number about a world that is no longer the one being
searched.

The guard makes that impossible to do by accident: `sweep` still computes and
prints the table, because seeing it is useful, but refuses to recommend a value.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_eval_module():
    spec = importlib.util.spec_from_file_location(
        "eval_scoring", ROOT / "scripts" / "eval_scoring.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("eval_scoring", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ev():
    return _load_eval_module()


def test_the_shipped_eval_set_is_still_the_ddgs_one(ev):
    """A tripwire: if this starts failing, the re-labelling actually happened."""
    rows = list((ROOT / "tests" / "fixtures" / "eval_set.csv").open(encoding="utf-8-sig"))
    header = rows[0]
    assert "relevant" in header
    counts = ev._provenance(list(csv.DictReader((ROOT / "tests" / "fixtures" / "eval_set.csv").open(encoding="utf-8-sig"))))
    assert counts.most_common(1)[0][0] == "ddgs"


def test_a_row_without_provenance_is_treated_as_ddgs(ev):
    """v1 only ever used one provider, so a missing column is not ambiguous."""
    assert ev.LEGACY_PROVIDER == "ddgs"
    assert ev._provenance([{"relevant": "yes"}])["ddgs"] == 1


def test_the_worksheet_schema_carries_the_provider(ev):
    assert "serp_provider" in ev.EVAL_COLUMNS


def test_mismatched_provenance_is_reported(ev, settings):
    settings.search_providers = ["ddgs"]
    rows = [{"serp_provider": "searxng", "relevant": "yes"}]
    assert ev._warn_if_stale(rows, settings) == ("searxng", "ddgs")


def test_matching_provenance_passes_silently(ev, settings):
    settings.search_providers = ["ddgs"]
    rows = [{"serp_provider": "ddgs", "relevant": "yes"}]
    assert ev._warn_if_stale(rows, settings) is None


def test_the_active_provider_is_the_head_of_the_chain(ev, settings, monkeypatch):
    from pitwall.search.searxng import SearxngProvider, reset_reachability_cache

    reset_reachability_cache()
    monkeypatch.setattr(SearxngProvider, "is_reachable", lambda self, **kw: True)
    settings.search_providers = ["searxng", "ddgs"]
    assert ev._active_provider(settings) == "searxng"


def test_an_unreachable_head_falls_back_to_the_configured_first(ev, settings, monkeypatch):
    """A dead provider must not make the guard itself explode."""
    from pitwall.search.searxng import SearxngProvider, reset_reachability_cache

    reset_reachability_cache()
    monkeypatch.setattr(SearxngProvider, "is_reachable", lambda self, **kw: False)
    settings.search_providers = ["searxng"]
    assert ev._active_provider(settings) in {"searxng", "?"}


def test_the_threshold_in_settings_is_still_the_ddgs_one():
    """§1.3 forbids moving it until the re-labelling is done and measured."""
    import yaml

    data = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    assert data["scoring"]["heuristic_threshold"] == 12
