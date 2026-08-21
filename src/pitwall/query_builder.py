"""The combinatorial query engine — §6 of PITWALL_PLAN.md.

`company × role × template` explodes fast (25 companies × 14 roles × 6
templates = 2,100 queries), so three controls keep it usable:

1. **Priority scoring** — ``3·(4 − company.tier) + 2·(4 − role.priority) + template.weight``
2. **Budget cap** — ``max_queries_per_run``, taking the top N by priority
3. **Freshness skip** — a query whose hash ran successfully inside
   ``cache_ttl_days`` is skipped entirely, so run #2 only spends budget on new
   combinations.

Templates that need Google operators are dropped automatically when the active
provider reports ``supports_operators() == False`` (operator degradation,
§6.2); the heuristic layer wins that precision back client-side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import Settings, config_dir, get_settings
from .models import Company, QuerySpec, Role, make_query_hash

TEMPLATES_YAML = "query_templates.yaml"

#: Fan-out fields a template may iterate over.
FOR_EACH_FIELDS = ("company_alias", "role_alias", "seniority")


@dataclass(frozen=True)
class Template:
    id: str
    pattern: str
    weight: float = 1.0
    requires_operators: bool = False
    for_each: str | None = None
    families: tuple[str, ...] = field(default_factory=tuple)

    def applies_to(self, role: Role) -> bool:
        return not self.families or role.family in self.families


def load_templates(path: Path | str | None = None) -> list[Template]:
    """Read `config/query_templates.yaml` into Template objects."""
    p = Path(path) if path else config_dir() / TEMPLATES_YAML
    if not p.exists():
        raise FileNotFoundError(f"query templates not found: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out: list[Template] = []
    for raw in data.get("templates", []) or []:
        if not raw.get("id") or not raw.get("pattern"):
            continue
        fe = raw.get("for_each")
        if fe and fe not in FOR_EACH_FIELDS:
            raise ValueError(
                f"template {raw['id']!r}: for_each={fe!r} not in {FOR_EACH_FIELDS}"
            )
        out.append(
            Template(
                id=str(raw["id"]),
                pattern=str(raw["pattern"]),
                weight=float(raw.get("weight", 1)),
                requires_operators=bool(raw.get("requires_operators", False)),
                for_each=fe,
                families=tuple(raw.get("families", []) or []),
            )
        )
    if not out:
        raise ValueError(f"no templates defined in {p}")
    return out


def filter_templates(
    templates: list[Template],
    *,
    supports_operators: bool,
    only_ids: list[str] | None = None,
) -> list[Template]:
    """Operator degradation (§6.2) plus an optional explicit allow-list."""
    out = [t for t in templates if supports_operators or not t.requires_operators]
    if only_ids:
        wanted = set(only_ids)
        out = [t for t in out if t.id in wanted]
    return out


def priority_for(company: Company, role: Role, template: Template, settings: Settings) -> float:
    """`w_tier·(4 − tier) + w_prio·(4 − priority) + template_weight`."""
    return (
        settings.company_tier_weight * (4 - company.tier)
        + settings.role_priority_weight * (4 - role.priority)
        + template.weight
    )


def _render(template: Template, company: Company, role: Role, **extra: str) -> str | None:
    """Fill a pattern. Returns None when a placeholder has no value."""
    role_aliases = role.aliases
    values: dict[str, str] = {
        "company": company.name,
        "company_alias": extra.get("company_alias", ""),
        "role": role.title,
        "role_alias": extra.get("role_alias", ""),
        "role_alias_1": role_aliases[0] if len(role_aliases) > 0 else role.title,
        "role_alias_2": role_aliases[1] if len(role_aliases) > 1 else role.title,
        "seniority": extra.get("seniority", ""),
        "country": company.country or "",
        "domain": company.domain or "",
        "family": role.family,
        "series": company.series or "",
    }
    try:
        rendered = template.pattern.format(**values)
    except KeyError as exc:
        raise ValueError(f"template {template.id!r} uses unknown placeholder {exc}") from exc

    # Any placeholder the template actually used must have had a value.
    for key, val in values.items():
        if "{" + key + "}" in template.pattern and not val.strip():
            return None
    text = " ".join(rendered.split())
    return text or None


def _fan_out(template: Template, company: Company, role: Role) -> list[dict[str, str]]:
    """Expand a template's `for_each` field into concrete substitutions."""
    if template.for_each == "company_alias":
        return [{"company_alias": a} for a in company.aliases]
    if template.for_each == "role_alias":
        return [{"role_alias": a} for a in role.aliases]
    if template.for_each == "seniority":
        return [{"seniority": s} for s in role.seniority]
    return [{}]


def build(
    companies: list[Company],
    roles: list[Role],
    templates: list[Template],
    settings: Settings | None = None,
    *,
    provider: str = "ddgs",
    supports_operators: bool | None = None,
    template_ids: list[str] | None = None,
    max_queries: int | None = None,
) -> list[QuerySpec]:
    """Build the prioritized, deduplicated query list. No network calls."""
    settings = settings or get_settings()
    if supports_operators is None:
        supports_operators = provider != "ddgs"

    active_templates = filter_templates(
        templates, supports_operators=supports_operators, only_ids=template_ids
    )
    budget = max_queries if max_queries is not None else settings.max_queries_per_run

    by_hash: dict[str, QuerySpec] = {}
    for company in companies:
        if not company.active:
            continue
        for role in roles:
            if not role.active:
                continue
            for template in active_templates:
                if not template.applies_to(role):
                    continue
                for extra in _fan_out(template, company, role):
                    text = _render(template, company, role, **extra)
                    if not text:
                        continue
                    qhash = make_query_hash(provider, text)
                    prio = priority_for(company, role, template, settings)
                    existing = by_hash.get(qhash)
                    if existing is not None:
                        # keep the highest priority route to the same string
                        if prio > existing.priority:
                            existing.priority = prio
                            existing.template_id = template.id
                            existing.company_id = company.id
                            existing.role_id = role.id
                            existing.company_name = company.name
                            existing.role_title = role.title
                        continue
                    by_hash[qhash] = QuerySpec(
                        company_id=company.id,
                        role_id=role.id,
                        company_name=company.name,
                        role_title=role.title,
                        template_id=template.id,
                        query_text=text,
                        query_hash=qhash,
                        priority=prio,
                        provider=provider,
                    )

    ordered = sorted(
        by_hash.values(),
        key=lambda q: (-q.priority, q.company_name or "", q.role_title or "", q.query_text),
    )
    return ordered[:budget] if budget and budget > 0 else ordered


def plan_summary(specs: list[Template] | list[QuerySpec], total_before_cap: int | None = None) -> dict[str, Any]:
    """Counts for the dry-run print-out and the Run page preview."""
    specs = list(specs)  # type: ignore[assignment]
    by_template: dict[str, int] = {}
    by_company: dict[str, int] = {}
    for q in specs:  # type: ignore[assignment]
        by_template[q.template_id] = by_template.get(q.template_id, 0) + 1  # type: ignore[union-attr]
        key = q.company_name or "?"  # type: ignore[union-attr]
        by_company[key] = by_company.get(key, 0) + 1
    return {
        "planned": total_before_cap if total_before_cap is not None else len(specs),
        "capped_to": len(specs),
        "by_template": dict(sorted(by_template.items(), key=lambda kv: -kv[1])),
        "by_company": dict(sorted(by_company.items(), key=lambda kv: -kv[1])),
    }


def build_all(
    companies: list[Company],
    roles: list[Role],
    settings: Settings | None = None,
    *,
    provider: str = "ddgs",
    supports_operators: bool | None = None,
    template_ids: list[str] | None = None,
    max_queries: int | None = None,
    templates_path: Path | str | None = None,
) -> tuple[list[QuerySpec], dict[str, Any]]:
    """Convenience wrapper: load templates, build, and summarise (uncapped
    total included so the UI can say "1,284 planned, capped to 300")."""
    settings = settings or get_settings()
    templates = load_templates(templates_path)
    uncapped = build(
        companies, roles, templates, settings,
        provider=provider, supports_operators=supports_operators,
        template_ids=template_ids, max_queries=0,
    )
    budget = max_queries if max_queries is not None else settings.max_queries_per_run
    capped = uncapped[:budget] if budget and budget > 0 else uncapped
    return capped, plan_summary(capped, total_before_cap=len(uncapped))
