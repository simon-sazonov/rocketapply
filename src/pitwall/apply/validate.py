"""Frame-compliance and data-integrity checks for `profile/`.

Ported from `profile/validate_profile.py` with its behaviour intact, so that the
loader, the CLI and CI all run one implementation rather than three. The script
remains as a thin wrapper.

The CV frame has no slack: one character too many wraps a bullet onto a new line
and pushes the page to two. This module is the thing that says so before an
employer does.

WRAPPING. Line counts come from greedy word wrap, never from dividing a character
count by a constant. The draft spec's divide-by-97 model calls `freedomcode.b3`
(193 chars) a two-line bullet; it renders as three in the real PDF. Greedy wrap
gets it right. `layout.py` refines this further, measuring with real font metrics
where the fonts are installed.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from . import PROFILE_DIR

__all__ = [
    "Report",
    "validate",
    "wrap_lines",
    "load_yaml",
    "ID_RE",
    "NUM_RE",
    "MAX_PROJECT_BULLET",
    "MAX_TITLE_LINE",
    "PROJECT_LINE_BUDGET",
    "ROW_LIMITS",
]

ID_RE = re.compile(r"^[a-z0-9_.]+$")
NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")

MAX_PROJECT_BULLET = 95
MAX_TITLE_LINE = 90
PROJECT_LINE_BUDGET = 11
ROW_LIMITS = {"row.programming": 95, "row.methods": 195, "row.tools": 195, "row.spoken": 105}

#: Experience is fixed: all seven bullets print on every application, so there is
#: no selection here and each one has to carry its weight.
EXPECTED_BULLETS = {"ophis": 3, "freedomcode": 3, "beesurfing": 1}

CONFIDENCE_LEVELS = {"high", "medium", "low", "none"}


def wrap_lines(text: str, width: int) -> int:
    """Greedy word wrap, the way a text renderer does it. Returns a line count."""
    lines, cur = 0, ""
    for word in text.split():
        cand = word if not cur else cur + " " + word
        if len(cand) <= width:
            cur = cand
        else:
            lines += 1
            cur = word
    return lines + (1 if cur else 0)


def load_yaml(directory: pathlib.Path, name: str) -> Any:
    """Read one profile file. Missing or malformed is the caller's problem."""
    return yaml.safe_load((directory / name).read_text(encoding="utf-8"))


@dataclass
class Report:
    """What validation found. `ok` is the only thing most callers need."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    todos: list[str] = field(default_factory=list)
    #: Every number the profile licenses. The fact-checker rejects any figure in
    #: generated prose that is not in here.
    allowed_numbers: set[str] = field(default_factory=set)
    #: row id -> printable candidate names, in declared order.
    row_pools: dict[str, list[str]] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    #: project id -> rendered line cost under the character wrapper.
    project_costs: dict[str, int] = field(default_factory=dict)
    #: row.spoken is fixed text with one character of slack (104 of 105). Carried
    #: so the report can keep saying so — it is the only row with no headroom.
    spoken_chars: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def failed(self, *, strict: bool = False) -> bool:
        return bool(self.errors) or (strict and bool(self.todos))

    def unsupported_numbers(self, text: str) -> list[str]:
        """Numbers in `text` with no backing anywhere in the profile.

        KNOWN LIMIT, by design: small integers (1-20) almost always appear
        somewhere in the profile, so "5 years of professional AWS experience"
        passes THIS gate. It is caught by the other two — `forbidden_claims`
        blocks both AWS and "years of professional experience", and the
        OVERSTATED verdict catches seniority inflation. This gate exists for
        fabricated MEASUREMENTS (0.999 ROC-AUC, "99% faster"), which it catches
        exactly. Do not try to make one gate do all three jobs.
        """
        return sorted(
            {
                n
                for n in NUM_RE.findall(text or "")
                if n not in self.allowed_numbers
                and n.replace(",", "") not in self.allowed_numbers
            }
        )


class _Validator:
    """One pass over `profile/`. Instantiated per run; holds no module state."""

    def __init__(self, directory: pathlib.Path) -> None:
        self.dir = directory
        self.report = Report()
        self.all_ids: set[str] = set()
        self.evidence_targets: set[str] = set()
        self._seen_ids: dict[str, str] = {}
        self.canon_tags: set[str] = set()
        self.tag_aliases: dict[str, str] = {}

    # ── recording ──

    def err(self, message: str) -> None:
        self.report.errors.append(message)

    def warn(self, message: str) -> None:
        self.report.warnings.append(message)

    def load(self, name: str) -> Any:
        path = self.dir / name
        if not path.exists():
            self.err(f"{name}: missing")
            return None
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — the message is the product
            self.err(f"{name}: not valid YAML — {exc}")
            return None

    def claim_id(self, new_id: str | None, where: str) -> None:
        if not new_id:
            return
        if new_id in self._seen_ids:
            self.err(
                f"duplicate id {new_id!r} — defined in {self._seen_ids[new_id]} "
                f"and again in {where}"
            )
        else:
            self._seen_ids[new_id] = where

    def check_tags(self, owner: str, tags: list[str] | None) -> None:
        """A tag outside the taxonomy matches nothing, which looks identical to
        having no experience. So an unknown tag is an error, not a warning."""
        for tag in tags or []:
            if tag in self.tag_aliases:
                self.err(f"{owner}: tag {tag!r} is a rejected alias — use {self.tag_aliases[tag]!r}")
            elif self.canon_tags and tag not in self.canon_tags:
                self.err(f"{owner}: tag {tag!r} is not in taxonomy.yaml")

    # ── the pass ──

    def run(self) -> Report:
        tax = self.load("taxonomy.yaml") or {}
        self.canon_tags = {t for grp in (tax.get("canonical") or {}).values() for t in (grp or [])}
        self.tag_aliases = tax.get("aliases") or {}

        layout = self.load("layout.yaml") or {}
        page = layout.get("page", {})
        w_safe = page.get("wrap_width_safe", 95)
        w_real = page.get("wrap_width_chars", 99)

        self._education()
        exp = self._experience(w_safe, w_real)
        projects = self._projects(w_safe)
        skills_doc = self._skills()
        metrics = self._metrics(exp, projects)
        ev_blocks = self._evidence()
        self._style()
        self.load("forbidden.yaml")
        self._todo_sweep()

        self.report.allowed_numbers = self._number_allowlist(exp, projects, metrics, ev_blocks)
        self._number_drift(metrics, ev_blocks, exp, projects)
        self.report.counts = {
            "projects": len(projects),
            "skills": len(skills_doc.get("skills", []) or []),
            "evidence": len(ev_blocks),
            "metrics": len(metrics),
        }
        return self.report

    def _education(self) -> None:
        for entry in self.load("education.yaml") or []:
            self.all_ids.add(entry.get("id"))
            for degree in entry.get("degrees") or []:
                self.all_ids.add(degree.get("id"))

    def _experience(self, w_safe: int, w_real: int) -> list[dict[str, Any]]:
        exp = self.load("experience.yaml") or []
        seen: dict[str, int] = {}
        for entry in exp:
            eid = entry.get("id")
            self.all_ids.add(eid)
            bullets = entry.get("bullets") or []
            seen[eid] = len(bullets)
            for bullet in bullets:
                self._experience_bullet(bullet, w_safe, w_real)
        for key, count in EXPECTED_BULLETS.items():
            if seen.get(key) != count:
                self.err(
                    f"experience: {key} must have exactly {count} bullets, "
                    f"found {seen.get(key, 0)}"
                )
        return exp

    def _experience_bullet(self, bullet: dict[str, Any], w_safe: int, w_real: int) -> None:
        bid = bullet.get("id", "?")
        self.all_ids.add(bid)
        self.evidence_targets.add(bid)
        text = bullet.get("text", "")
        if not ID_RE.match(bid):
            self.err(f"experience {bid}: id must match ^[a-z0-9_.]+$")
        if "\n" in text:
            self.err(f"experience {bid}: text contains a newline — use a quoted single-line string")
        declared = bullet.get("rendered_lines")
        actual_safe = wrap_lines(text, w_safe)
        actual_real = wrap_lines(text, w_real)
        if declared is None:
            self.warn(f"experience {bid}: no rendered_lines declared")
        elif not actual_real <= declared <= actual_safe:
            self.err(
                f"experience {bid}: declared {declared} lines, wraps to {actual_real} "
                f"at {w_real} / {actual_safe} at {w_safe} chars ({len(text)} chars)"
            )
        if len(bullet.get("tags") or []) < 3:
            self.err(f"experience {bid}: needs at least 3 tags")
        self.check_tags(f"experience {bid}", bullet.get("tags"))
        self.claim_id(bid, "experience.yaml")
        target = declared if declared is not None else actual_safe
        for variant in bullet.get("variants") or []:
            self._variant(variant, target, w_safe, w_real)

    def _variant(self, variant: dict[str, Any], target: int, w_safe: int, w_real: int) -> None:
        """A rephrasing that changes how many lines a bullet occupies breaks the
        frozen frame, so it is rejected here rather than at render time."""
        vid, vtext = variant.get("id", "?"), variant.get("text", "")
        self.all_ids.add(vid)
        lo, hi = wrap_lines(vtext, w_real), wrap_lines(vtext, w_safe)
        if not lo <= target <= hi:
            self.err(
                f"variant {vid}: must render as {target} line(s) like its canonical "
                f"bullet, but wraps to {lo}/{hi} ({len(vtext)} chars) — LINE-NEUTRALITY FAIL"
            )

    def _projects(self, w_safe: int) -> list[dict[str, Any]]:
        projects = self.load("projects.yaml") or []
        if len(projects) < 8:
            self.err(f"projects.yaml: needs at least 8 projects, found {len(projects)}")
        costs: dict[str, int] = {}
        for project in projects:
            pid = project.get("id")
            self.all_ids.add(pid)
            title_line = f"{project.get('name', '')} {project.get('link_label') or ''}".strip()
            if len(title_line) > MAX_TITLE_LINE:
                self.err(
                    f"{pid}: title line {len(title_line)} chars, "
                    f"max {MAX_TITLE_LINE} -> {title_line!r}"
                )
            bullets = project.get("bullets") or []
            if len(bullets) != 2:
                self.err(f"{pid}: must have exactly 2 bullets, found {len(bullets)}")
            cost = 1
            for bullet in bullets:
                cost += self._project_bullet(bullet, w_safe)
            costs[pid] = cost
            if cost != 3:
                self.warn(f"{pid}: costs {cost} lines instead of 3 — removes combinations from the pool")
        self.report.project_costs = costs

        # The worst case has to fit, or some trio the selector may legally pick
        # would overflow the page.
        worst = sorted(costs.values(), reverse=True)[:3]
        if sum(worst) > PROJECT_LINE_BUDGET:
            self.err(
                f"projects: worst-case trio costs {sum(worst)} lines, "
                f"budget is {PROJECT_LINE_BUDGET}"
            )

        for project in projects:
            for other in project.get("conflicts_with") or []:
                if other not in self.all_ids:
                    self.err(f"{project.get('id')}: conflicts_with {other!r} does not exist")
        return projects

    def _project_bullet(self, bullet: dict[str, Any], w_safe: int) -> int:
        bid, text = bullet.get("id", "?"), bullet.get("text", "")
        self.all_ids.add(bid)
        self.evidence_targets.add(bid)
        if not ID_RE.match(bid):
            self.err(f"{bid}: id must match ^[a-z0-9_.]+$")
        if len(text) > MAX_PROJECT_BULLET:
            self.err(
                f"{bid}: {len(text)} chars, max {MAX_PROJECT_BULLET} "
                f"(over by {len(text) - MAX_PROJECT_BULLET})"
            )
        if len(text) < 55:
            self.warn(f"{bid}: only {len(text)} chars — short bullets waste the line")
        if len(bullet.get("tags") or []) < 3:
            self.err(f"{bid}: needs at least 3 tags")
        self.check_tags(bid, bullet.get("tags"))
        self.claim_id(bid, "projects.yaml")
        return wrap_lines(text, w_safe)

    def _skills(self) -> dict[str, Any]:
        doc = self.load("skills.yaml") or {}
        rows = doc.get("rows", {})
        skills = doc.get("skills", []) or []

        spoken = (rows.get("row.spoken") or {}).get("fixed_text", "")
        self.report.spoken_chars = len(spoken)
        if len(spoken) > ROW_LIMITS["row.spoken"]:
            self.err(f"row.spoken: {len(spoken)} chars, max {ROW_LIMITS['row.spoken']}")

        pool: dict[str, list[str]] = {r: [] for r in ROW_LIMITS}
        for skill in skills:
            self._skill(skill, pool)
        self.report.row_pools = pool

        for row, limit in ROW_LIMITS.items():
            if row == "row.spoken":
                continue
            available = ", ".join(pool[row])
            if len(available) <= limit:
                self.warn(
                    f"{row}: master list is only {len(available)} chars against a "
                    f"{limit}-char row — it all fits, so this row will look identical "
                    f"on every application"
                )
            pinned = [s for s in skills if s.get("row") == row and s.get("pin")]
            pin_len = len(", ".join(x.get("name", "") for x in pinned))
            if pin_len > limit:
                self.err(f"{row}: pinned items alone are {pin_len} chars, over the {limit}-char budget")
        return doc

    def _skill(self, skill: dict[str, Any], pool: dict[str, list[str]]) -> None:
        sid, row = skill.get("id", "?"), skill.get("row")
        self.all_ids.add(sid)
        self.claim_id(sid, "skills.yaml")
        if not ID_RE.match(sid):
            self.err(f"{sid}: id must match ^[a-z0-9_.]+$")

        conf = skill.get("confidence")
        if conf not in CONFIDENCE_LEVELS:
            self.err(f"{sid}: confidence must be high/medium/low/none, got {conf!r}")

        evidence = skill.get("evidence") or []
        for ref in evidence:
            if ref not in self.evidence_targets and not ref.startswith("edu."):
                self.err(f"{sid}: evidence {ref!r} does not point at a real bullet id")

        # `evidence_private: true` means the work is real but sits in a repository
        # Simon does not publish. It satisfies the evidence requirement; what it
        # cannot do is supply a project name to cite, so the letter writer must
        # not invent one.
        private = bool(skill.get("evidence_private"))
        if conf == "high" and len(evidence) < 2 and not private:
            self.err(f"{sid}: confidence 'high' requires 2+ evidence ids, has {len(evidence)}")
        if conf == "medium" and len(evidence) < 1 and not private:
            self.err(f"{sid}: confidence 'medium' requires at least 1 evidence id")
        if private and not (skill.get("note") or "").strip():
            self.err(f"{sid}: evidence_private needs a `note` saying where the work sits")
        if conf == "low" and not skill.get("low_phrasing"):
            self.err(f"{sid}: confidence 'low' requires low_phrasing")
        if conf == "none" and evidence:
            self.err(f"{sid}: confidence 'none' must have no evidence")

        if skill.get("printable") and conf in {"low", "none"}:
            if skill.get("user_override"):
                tail = "" if skill.get("letter_policy") else (
                    " — and NO letter_policy set, so a cover letter may also claim it"
                )
                self.warn(
                    f"{sid}: printing with confidence {conf!r} and no evidence, "
                    f"by override ({skill['user_override']}){tail}"
                )
            else:
                self.err(f"{sid}: printable but confidence is {conf}")

        if skill.get("printable") and row in pool:
            pool[row].append(skill.get("name", sid))

    def _metrics(
        self, exp: list[dict[str, Any]], projects: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        metrics = self.load("metrics.yaml") or []
        ids = set()
        for metric in metrics:
            mid = metric.get("id", "?")
            ids.add(mid)
            self.all_ids.add(mid)
            self.claim_id(mid, "metrics.yaml")
            if not metric.get("method"):
                self.err(f"{mid}: every metric needs a `method` — the answer to 'how did you measure that?'")
            if not metric.get("claim"):
                self.err(f"{mid}: every metric needs a `claim` (copied word-for-word into documents)")
        for items in (exp, projects):
            for entry in items:
                for bullet in entry.get("bullets") or []:
                    for ref in bullet.get("metrics") or []:
                        if ref not in ids:
                            self.err(f"{bullet.get('id')}: metric {ref!r} has no record in metrics.yaml")
        return metrics

    def _evidence(self) -> list[dict[str, Any]]:
        blocks = self.load("evidence.yaml") or []
        if len(blocks) < 5:
            self.err(f"evidence.yaml: needs 5-8 blocks, found {len(blocks)}")
        for block in blocks:
            eid = block.get("id", "?")
            self.all_ids.add(eid)
            self.claim_id(eid, "evidence.yaml")
            facts = block.get("facts") or []
            if not 3 <= len(facts) <= 6:
                self.warn(f"{eid}: should have 3-6 facts, has {len(facts)}")
            if not block.get("story"):
                self.err(f"{eid}: missing `story` — this is the field that stops letters sounding like AI")
            for ref in block.get("source_refs") or []:
                if ref not in self.all_ids:
                    self.warn(f"{eid}: source_ref {ref!r} not found")
        return blocks

    def _style(self) -> None:
        style = self.load("style.yaml") or {}
        samples = [s for s in (style.get("writing_samples") or []) if "TODO_SIMON" not in str(s)]
        if not samples:
            self.err(
                "style.yaml: writing_samples contains no real sample. "
                "Two paragraphs you actually wrote do more than any instruction."
            )
        exemplars = [e for e in (style.get("exemplars") or []) if isinstance(e, dict)]
        good = [e for e in exemplars if e.get("verdict") == "good" and e.get("text")]
        if not good:
            self.warn(
                "style.yaml: no exemplar with `verdict: good`. Letters are being built "
                "from fragments, so the model has never seen a finished letter in your "
                "voice — which is why the closing paragraph reads generic."
            )
        if not [e for e in exemplars if e.get("verdict") == "avoid" and e.get("text")]:
            self.warn(
                "style.yaml: no exemplar with `verdict: avoid`. One of your own weaker "
                "letters teaches more than a banned-phrase list, because it isolates what "
                "went wrong rather than which words to dodge."
            )
        for entry in exemplars:
            if entry.get("verdict") not in {"good", "avoid"}:
                self.err(f"style.yaml exemplar {entry.get('id', '?')!r}: "
                         f"verdict must be 'good' or 'avoid'")
            if not str(entry.get("text") or "").strip():
                self.err(f"style.yaml exemplar {entry.get('id', '?')!r}: needs `text`")

    def _todo_sweep(self) -> None:
        for path in sorted(self.dir.glob("*.yaml")):
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "TODO_SIMON" in line and not line.lstrip().startswith("#"):
                    self.report.todos.append(f"{path.name}:{i}")

    def _number_drift(
        self,
        metrics: list[dict[str, Any]],
        blocks: list[dict[str, Any]],
        exp: list[dict[str, Any]],
        projects: list[dict[str, Any]],
    ) -> None:
        """A figure stated in an evidence fact that appears nowhere else.

        `facts` are the only things a letter writer may assert about a block, so
        a number in one WILL end up in prose. When the same measurement is also
        written as a bullet or a metric claim, the two have to agree — and they
        drift, because they were written weeks apart. This caught `ev.pitwall`
        saying "a 0.90 recall floor" while `metrics.yaml` and `proj.pitwall.b2`
        both say 0.94. Whichever is right, a letter must not say one while the CV
        in the same envelope says the other.
        """
        printed: set[str] = set()
        for source in (exp, projects):
            for entry in source:
                for bullet in entry.get("bullets") or []:
                    printed.update(NUM_RE.findall(bullet.get("text", "")))
        for metric in metrics:
            printed.update(NUM_RE.findall(str(metric.get("claim", ""))))
            printed.update(NUM_RE.findall(str(metric.get("method", ""))))
            for key in ("raw_value", "raw_before", "raw_after"):
                if metric.get(key) is not None:
                    printed.add(str(metric[key]))
        # Education too: the thesis's measured findings carry real figures
        # ("110 ms/race", "Python 3.13") and the MSc end date is where "2027"
        # legitimately comes from.
        printed.update(
            NUM_RE.findall(yaml.safe_dump(self.load("education.yaml") or [], allow_unicode=True))
        )
        printed |= {n.replace(",", "") for n in printed}

        for block in blocks:
            for fact in block.get("facts") or []:
                for number in NUM_RE.findall(fact):
                    if number in printed or number.replace(",", "") in printed:
                        continue
                    self.warn(
                        f"{block.get('id', '?')}: the figure {number!r} appears in a fact but "
                        f"nowhere in a bullet or a metric — a letter may print it while the CV "
                        f"says something else. Check it against metrics.yaml."
                    )

    def _number_allowlist(
        self,
        exp: list[dict[str, Any]],
        projects: list[dict[str, Any]],
        metrics: list[dict[str, Any]],
        blocks: list[dict[str, Any]],
    ) -> set[str]:
        """Every figure the profile licenses.

        DERIVED, never hand-maintained. The gate's job is to stop the MODEL
        inventing figures, not to re-verify figures Simon wrote: any number
        already sitting in a canonical bullet, an approved variant, a metric
        claim or an education field has passed the truth layer by definition.
        So a new bullet licenses its own numbers automatically, and metrics.yaml
        keeps its real job — the claims where `method` answers "how did you
        measure that?" — instead of collecting dataset sizes and years.
        """
        corpus: list[str] = []
        for metric in metrics:
            corpus.append(str(metric.get("claim", "")))
            # `method` answers "how did you measure that?" and carries the real
            # figures behind the claim — the 63,000-evaluation budget lives only
            # here. A letter quoting it is quoting Simon, not inventing.
            corpus.append(str(metric.get("method", "")))
            for key in ("raw_value", "raw_before", "raw_after"):
                if metric.get(key) is not None:
                    corpus.append(str(metric[key]))
        # `facts` are the whitelist the letter writer is given, so a number in
        # one is already licensed by definition. Not licensing them here would
        # reject a letter for faithfully quoting the profile.
        for block in blocks:
            corpus.extend(str(f) for f in block.get("facts") or [])
        for entry in exp:
            for bullet in entry.get("bullets") or []:
                corpus.append(bullet.get("text", ""))
                corpus.extend(v.get("text", "") for v in bullet.get("variants") or [])
        for project in projects:
            corpus.append(project.get("name", ""))
            corpus.extend(b.get("text", "") for b in project.get("bullets") or [])
        corpus.append(yaml.safe_dump(self.load("education.yaml") or [], allow_unicode=True))

        allowed: set[str] = set()
        for text in corpus:
            for number in NUM_RE.findall(text):
                allowed.add(number)
                allowed.add(number.replace(",", ""))  # 9,543 also licenses 9543
        return allowed


def validate(directory: pathlib.Path | str | None = None) -> Report:
    """One full pass over `profile/`. Never raises on bad data — it reports."""
    return _Validator(pathlib.Path(directory) if directory else PROFILE_DIR).run()


def format_report(report: Report, *, strict: bool = False) -> str:
    """The console rendering, shared by the CLI and `profile/validate_profile.py`
    so the two can never report the same profile differently."""
    counts = report.counts
    out = [
        "=" * 72,
        f"  profile validation    {counts.get('projects', 0)} projects · "
        f"{counts.get('skills', 0)} skills · {counts.get('evidence', 0)} evidence blocks · "
        f"{counts.get('metrics', 0)} metrics",
        "=" * 72,
    ]
    out += [f"  ERROR    {m}" for m in report.errors]
    out += [f"  warning  {m}" for m in report.warnings]
    if report.todos:
        out.append(f"\n  {len(report.todos)} TODO_SIMON marker(s) awaiting your input:")
        out += [f"      {t}" for t in report.todos]
    out.append(
        f"  number allowlist: {len(report.allowed_numbers)} figures derived from bullets, "
        f"variants, metrics and education\n      (the fact-checker rejects any number in "
        f"generated prose that is not in this set)"
    )
    out.append("  row pools (printable candidates vs row budget):")
    for row, limit in ROW_LIMITS.items():
        names = report.row_pools.get(row, [])
        if row == "row.spoken":
            out.append(f"      {row:<18} fixed text, {report.spoken_chars}/{limit} chars")
            continue
        available = ", ".join(names)
        ratio = len(available) / limit if limit else 0
        verdict = (
            "tailors well" if ratio >= 1.5
            else ("some choice" if ratio > 1.0 else "NO CHOICE — all of it fits")
        )
        out.append(
            f"      {row:<18} {len(names):>3} items, {len(available):>4} chars for a "
            f"{limit}-char row  ({ratio:.1f}x) — {verdict}"
        )
    verdict = "FAIL" if report.failed(strict=strict) else "PASS"
    out.append(
        f"\n  {len(report.errors)} error(s), {len(report.warnings)} warning(s), "
        f"{len(report.todos)} todo(s)  ->  {verdict}"
    )
    return "\n".join(out)
