"""Role-family tag presets — for `preview` only.

The real path from a posting to a `JobSignal` runs through the analyzer and the
matcher, which read the actual advertised requirements. These presets exist so
the frame can be exercised, compared and eyeballed without a job, a database or
an API key, and so the test suite can assert the frame is invariant across
genuinely different signals.

Tags are canonical, so they are checked against `profile/taxonomy.yaml` rather
than being free text that silently matches nothing.
"""

from __future__ import annotations

from .selector import JobSignal

__all__ = ["FAMILIES", "signal_for"]

FAMILIES: dict[str, tuple[set[str], set[str]]] = {
    "quantitative": (
        {"monte_carlo", "risk_modelling", "probability", "simulation", "statistics"},
        {"time_series", "finance", "optimization", "decision_making", "walk_forward"},
    ),
    "machine_learning": (
        {"machine_learning", "deep_learning", "model_evaluation", "classification"},
        {"pytorch", "cnn", "nlp", "transformers", "class_imbalance", "reinforcement_learning"},
    ),
    "data_engineering": (
        {"data_engineering", "etl", "sql", "databases"},
        {"pyspark", "parquet", "big_data", "data_quality", "dimensional_modelling",
         "query_optimization"},
    ),
    "research": (
        {"experimental_design", "hypothesis_testing", "statistics", "research"},
        {"ablation", "benchmarking", "probability", "optimization", "reinforcement_learning"},
    ),
    "analytics": (
        {"analytics", "visualization", "statistics", "sql"},
        {"eda", "clustering", "communication", "stakeholder_management"},
    ),
    "data_science": (
        {"machine_learning", "python", "statistics", "sql"},
        {"model_evaluation", "time_series", "feature_engineering", "data_quality"},
    ),
    "software": (
        {"software", "python", "testing", "system_design"},
        {"databases", "pipelines", "git"},
    ),
}


def signal_for(family: str) -> JobSignal:
    if family not in FAMILIES:
        raise KeyError(f"unknown role family {family!r} — one of {', '.join(sorted(FAMILIES))}")
    required, preferred = FAMILIES[family]
    return JobSignal(
        required=set(required), preferred=set(preferred), role_family=family,
        title=family.replace("_", " ").title(),
    )
