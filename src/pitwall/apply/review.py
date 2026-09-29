"""REVIEW.md — the one page that answers "do I have to check this letter?"

Simon asked for exactly this, in these words: *"I just need to get a cover letter,
and notice like 'please check these sentences' or 'no need to check, everything is
proved'."* So the headline is one of three lines and the rest is evidence for it.

Before this module the only human-readable artefact was a block appended to the
BOTTOM of the letter itself, written only when something was already wrong — so a
letter that was completely clean shipped with no statement that it was, and a
letter that was not had to be edited before it could be sent. The note is now a
separate file, always written, and the letter is always sendable as-is.

WHY THIS IS NOT IN `packager.py`: that module names files, writes provenance and
records the DB row, and this needs `Letter`, `FactCheck`, `Repair` and the company
research. Keeping it separate also means `review_markdown` is a pure function of
plain values, so it can be tested without a pipeline run, a router or a network.
Nothing here imports `ApplicationResult` — `pipeline` imports this, so the reverse
would be a cycle.
"""

from __future__ import annotations

import pathlib
from typing import Any

__all__ = ["review_markdown", "write_review", "REVIEW_FILE", "HEADLINES"]

REVIEW_FILE = "REVIEW.md"

#: state -> (headline, what the subtitle says about checking). The three states
#: are the whole point of the file, so they are declared once and read from here
#: rather than formatted inline in three places.
HEADLINES: dict[str, tuple[str, str]] = {
    "clean": ("Ready to send", "no checking needed"),
    "review": ("Check these sentences", "everything else is proved"),
    "unsent": ("Do not send this letter yet", "one claim could not be repaired"),
    # No letter at all: either none was asked for, or the stage stopped. Its own
    # headline, because the first version defaulted this to "clean" and a folder
    # holding only a CV announced "Ready to send — nothing here needs checking".
    "": ("CV only — there is no letter here", "nothing to check"),
}


def _rule(code: str) -> str:
    """A violation code as something a person would say."""
    return code.replace("_", " ").lower()


def _what_to_do(
    state: str, letter: Any, fact: Any, flagged: int, interrupted: bool
) -> list[str]:
    if letter is None:
        if interrupted:
            return [
                "The CV is finished and correct. The letter stage stopped before it "
                "produced anything — the reason is under *If the run was interrupted* "
                "below. Re-running this job will write the letter into a new folder.",
            ]
        return ["The CV is finished and correct. No letter was asked for on this run."]
    if state == "clean":
        who = ""
        if fact is not None and fact.verdicts:
            auditor = fact.auditor or "a second model"
            who = (
                f" An {'independent' if fact.independent else 'un-independent'} check "
                f"by {auditor} read all {len(fact.verdicts)} sentence(s) and questioned none."
            )
        return [
            "Nothing here needs checking. Every number, name and claim in this letter "
            "traces to a record in your profile, and the deterministic gates found "
            f"nothing to object to.{who}"
        ]
    if state == "review":
        return [
            f"Read the {flagged} item(s) below and decide. Everything else in the letter "
            "is proved against your profile, so this is a judgement call about phrasing "
            "rather than a question of fact."
        ]
    return [
        "This letter claims something your profile does not support, and the automatic "
        "repair did not fix it. The sentence is named below. Delete or rewrite it and the "
        "letter is fine — the rest of it passed every check.",
        "",
        "The file is deliberately named `..._DO_NOT_SEND.txt` and carries a banner at the "
        "top so it cannot be pasted into an application by accident.",
    ]


def _flagged_items(letter: Any, fact: Any) -> list[tuple[str, str, str]]:
    """`(what, where, who objected)` for everything worth a human's attention.

    Merges the three sources that used to be reported in three different places:
    the surviving deterministic violations, the banned phrases (which carry no
    code at all), and the model auditor's doubts.
    """
    out: list[tuple[str, str, str]] = []
    for violation in getattr(letter, "violations", []) or []:
        out.append((
            violation.detail,
            violation.span,
            f"a deterministic gate ({_rule(violation.code)})",
        ))
    for phrase in getattr(letter, "banned", []) or []:
        out.append((
            "this phrase is on your own banned list in `style.yaml`",
            phrase,
            "a deterministic gate (banned phrase)",
        ))
    if fact is not None:
        auditor = fact.auditor or "a model"
        who = f"{auditor}, reading independently" if fact.independent else (
            f"{auditor} — WHICH ALSO WROTE THE LETTER, so this verdict is worth less"
        )
        for verdict in fact.failures:
            out.append((
                f"the auditor called this {verdict.verdict.lower()}",
                verdict.span or verdict.sentence,
                who,
            ))
    return out


def review_markdown(
    *,
    company: str = "",
    title: str = "",
    url: str = "",
    state: str = "clean",
    letter: Any = None,
    fact: Any = None,
    plan: Any = None,
    company_research: dict[str, Any] | None = None,
    repairs: list[Any] | None = None,
    warnings: list[str] | None = None,
    notes: list[str] | None = None,
    cv_pdf: str = "",
    letter_file: str = "",
    tokens: int = 0,
    prompt_version: str = "",
    profile_git_sha: str = "",
    generated_at: str = "",
) -> str:
    """The note, as Markdown. Pure: no I/O, no router, no `ApplicationResult`."""
    research = company_research or {}
    # Falls back to the letter's own record. A caller that forgot to pass these
    # would produce a note claiming the letter was verified while silently not
    # mentioning that three of its lines had been rewritten — the exact failure
    # this module's docstring warns about, so it must not depend on a keyword
    # argument being remembered.
    repairs = list(repairs if repairs is not None else (getattr(letter, "repairs", None) or []))
    items = _flagged_items(letter, fact)
    headline, subtitle_tail = HEADLINES.get(
        state if letter is not None else "", HEADLINES["review"]
    )

    words = getattr(letter, "words", 0) or 0
    subtitle = " · ".join(p for p in (
        company or "unknown employer",
        title,
        f"{words} words" if words else "",
        f"{len(items)} item(s) below" if items and letter is not None else subtitle_tail,
    ) if p)

    out: list[str] = [f"# {headline}", "", subtitle, ""]
    out += _what_to_do(state, letter, fact, len(items), bool(warnings))

    if items:
        out += ["", "## Check these", ""]
        for index, (what, where, who) in enumerate(items, 1):
            out.append(f"{index}. **{what}**")
            if where:
                out.append(f"   - in: `{where}`")
            out.append(f"   - raised by: {who}")
        out.append("")

    # ── what was verified ──
    out += ["", "## What was checked, and by what", ""]
    out.append("| check | by | result |")
    out.append("|---|---|---|")
    out.append(
        "| numbers, forbidden claims, invented locations, overclaims | "
        "deterministic rules over your profile | "
        + ("nothing outstanding" if not getattr(letter, "violations", None)
           else f"{len(getattr(letter, 'violations', []))} item(s) above") + " |"
    )
    if fact is not None and fact.verdicts:
        out.append(
            f"| every sentence that claims something about you | {fact.auditor or 'a model'}"
            f"{'' if fact.independent else ' (not independent)'} | {fact.summary()} |"
        )
    else:
        out.append("| sentence-by-sentence audit | — | not run for this letter |")
    if research:
        verified = research.get("verified")
        out.append(
            f"| the facts about {company or 'the employer'} | "
            f"{research.get('model') or 'a model'}, then a second one | "
            + ("checked against their own pages" if verified else "not verified") + " |"
        )
    out.append("")

    # Labelled and moved out of the table. These come from the CV renderer and the
    # fact checker's own prose, and dropped unlabelled under "what was checked"
    # they read as non-sequiturs — a real note said "'|Contract|' omitted" directly
    # beneath a row about forbidden claims.
    if notes:
        out += ["", "### Worth knowing, but nothing to do about", ""]
        out += [f"- {note}" for note in notes]
        out.append("")

    # ── the company facts ──
    if research:
        source = research.get("source")
        out += ["", f"## What this letter says about {company or 'the employer'}", ""]
        if source == "profile":
            out.append("From your own notes in `profile/companies.yaml` — you wrote these.")
        else:
            out.append(
                "Researched automatically from their own pages. Every sentence had to be "
                "traceable to one of these, and a second model confirmed each one before "
                "it was allowed through."
            )
        angle = getattr(plan, "company_angle", "") or ""
        if angle:
            out += ["", f"> {angle}"]
        urls = [u for u in (research.get("urls") or []) if u]
        if urls:
            out += ["", "Sources:"]
            out += [f"- {u}" for u in dict.fromkeys(urls)]
        if research.get("fetched_at"):
            out += ["", f"Read on {research['fetched_at'][:10]}."]
        out.append("")

    # ── repairs ──
    mechanical = [r for r in repairs if r.tier in {"mechanical", "excision"}]
    surgery = [r for r in repairs if r.tier == "surgery"]
    if mechanical or surgery:
        out += ["", "## Fixed for you, without asking", ""]
    if mechanical:
        out.append("Mechanically, with no model involved:")
        out += [f"- {r.what}" for r in mechanical]
        out.append("")
    if surgery:
        out.append(
            "One sentence rewritten and then re-checked. The rule it broke is named, so "
            "you can see whether you agree with the new version:"
        )
        for r in surgery:
            out += [
                "",
                f"- **{_rule(r.code)}**{f' — rewritten by {r.by}' if r.by else ''}",
                f"  - before: {r.before}",
                f"  - after:  {r.after}",
            ]
        out.append("")

    if warnings:
        out += ["", "## If the run was interrupted", ""]
        out += [f"- {w}" for w in warnings]
        out.append("")

    # ── provenance ──
    attempts = getattr(letter, "attempts", 0) or 0
    made = [
        f"prompt `{prompt_version}`" if prompt_version else "",
        f"{attempts} draft(s)" if attempts else "",
        f"{fact.rounds} check round(s)" if fact is not None else "",
        f"{tokens:,} tokens" if tokens else "",
        f"profile `{profile_git_sha[:8]}`" if profile_git_sha else "",
    ]
    out += ["", "## How this was made", "", "  ".join(p for p in made if p)]
    if generated_at:
        out.append(f"\nGenerated {generated_at}.")
    for label, path in (("CV", cv_pdf), ("letter", letter_file)):
        if path:
            out.append(f"- {label}: `{pathlib.Path(path).name}`")
    if url:
        out.append(f"- posting: {url}")
    out.append("")
    return "\n".join(out).rstrip() + "\n"


def write_review(directory: pathlib.Path, text: str) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / REVIEW_FILE
    path.write_text(text, encoding="utf-8")
    return path
