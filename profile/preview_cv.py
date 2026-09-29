#!/usr/bin/env python3
"""Show what the frozen frame produces for a given role family.

    python3 preview_cv.py                 # the whole project library
    python3 preview_cv.py quantitative    # a rendered CV block for one role family

This is a PREVIEW, not the renderer. It does the two things that vary — pick 3
projects, pack the skills rows — so you can see the selection actually working
before anyone builds the PDF side.
"""
import sys, yaml, pathlib

H = pathlib.Path(__file__).parent
load = lambda n: yaml.safe_load((H / n).read_text(encoding="utf-8"))

FAMILIES = {
    "quantitative":     {"monte_carlo","risk_modelling","simulation","probability","finance",
                         "time_series","optimization","decision_making","statistics","walk_forward"},
    "machine_learning": {"machine_learning","deep_learning","nlp","transformers","cnn","model_evaluation",
                         "reinforcement_learning","llm","classification","class_imbalance"},
    "data_engineering": {"data_engineering","etl","pyspark","parquet","databases","sql","mongodb",
                         "dimensional_modelling","data_quality","big_data","query_optimization"},
    "research":         {"experimental_design","hypothesis_testing","statistics","ablation",
                         "reinforcement_learning","optimization","probability","benchmarking"},
}

def wrap(t, w=95):
    lines, cur = 0, ""
    for x in t.split():
        c = x if not cur else cur + " " + x
        if len(c) <= w: cur = c
        else: lines += 1; cur = x
    return lines + (1 if cur else 0)

def pack(names, budget):
    out, used = [], 0
    for n in names:
        add = len(n) + (2 if out else 0)
        if used + add <= budget:
            out.append(n); used += add
    return out, used

def library():
    print(f"THE PROJECT LIBRARY — {len(load('projects.yaml'))} projects, 3 print per CV\n")
    for p in load("projects.yaml"):
        label = p.get("link_label") or ""
        print(f"  {p['name']}{('  ' + label) if label else ''}")
        for b in p["bullets"]:
            print(f"    • {b['text']}")
        print()

def render(family):
    tags = FAMILIES[family]
    projects, skills = load("projects.yaml"), load("skills.yaml")["skills"]
    exp, personal = load("experience.yaml"), load("personal.yaml")

    # exclude any project whose work is already printed in the fixed experience block
    blocked = {c for p in projects for c in (p.get("conflicts_with") or [])}
    printed = {b["id"] for e in exp for b in e["bullets"]}
    pool = [p for p in projects if not (blocked & printed & set(p.get("conflicts_with") or []))]

    def score(p):
        t = {x for b in p["bullets"] for x in b["tags"]} | set(p.get("themes") or [])
        s = len(t & tags) * 2 + (3 if family in (p.get("use_for") or []) else 0)
        return s + (2 if p.get("strength") == "strong" else 0)

    chosen = sorted(pool, key=lambda p: (-score(p), p["id"]))[:3]

    rows = load("skills.yaml")["rows"]
    def row_names(rid):
        cand = [s for s in skills if s.get("row") == rid and s.get("printable")]
        def relevance(sk):
            words = {sk["name"].lower()} | {a.lower() for a in (sk.get("aliases") or [])}
            # split multi-word names so "Risk Modelling" meets the tag risk_modelling
            words |= {w.replace(" ", "_") for w in words}
            return len(words & tags)
        cand.sort(key=lambda sk: (not sk.get("pin"),
                                  -relevance(sk),
                                  {"high":0,"medium":1,"low":2,"none":3}[sk["confidence"]]))
        return [s["name"] for s in cand]

    print("=" * 78)
    print(f"  CV PREVIEW — role family: {family}")
    print("=" * 78)
    print(f"\n{'Simon Sazonov':<40}{'Data Scientist':>37}")
    print(f"{personal['email']} · {personal['github']}\n")

    print("SKILLS")
    total = 0
    for rid, budget in [("row.programming", 95), ("row.methods", 195), ("row.tools", 195)]:
        picked, used = pack(row_names(rid), budget)
        total += used
        print(f"  • {', '.join(picked)}")
        print(f"      ({used}/{budget} chars, {len(picked)} of "
              f"{len(row_names(rid))} candidates)")
    print(f"  • {rows['row.spoken']['fixed_text']}")

    print("\nEXPERIENCE")
    lines = 0
    for e in exp:
        print(f"  {e['org']} — {e['role']}")
        for b in e["bullets"]:
            lines += b["rendered_lines"]
            print(f"    • {b['text']}")
    print(f"      [{lines} lines used of a 17-line budget]")

    print("\nPROJECTS")
    cost = 0
    for p in chosen:
        c = 1 + sum(wrap(b["text"]) for b in p["bullets"]); cost += c
        label = p.get("link_label") or ""
        print(f"  {p['name']}{('  ' + label) if label else ''}   [score {score(p)}, {c} lines]")
        for b in p["bullets"]:
            print(f"    • {b['text']}")
    print(f"      [{cost} lines used of an 11-line budget]")
    print("\n  not chosen, next best:",
          ", ".join(p["name"].split("–")[0].strip()
                    for p in sorted(pool, key=lambda p: -score(p))[3:6]))

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg in FAMILIES: render(arg)
    else: library()
