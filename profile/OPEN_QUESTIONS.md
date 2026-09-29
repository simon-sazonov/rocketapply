# Open questions — round 2

All ten from round 1 are closed. **The profile validates clean: 0 errors, 0 TODOs.**
What follows is what is genuinely left, in order of how much it matters.

---

## A — One real conflict: when are you available? ⛔

You told me **"available now, full time."**
Your Alpine letter, sent 15 September, says **"I finish my MSc in July 2027, the month
the placement starts."**

Both can be true — you already work at Ophis alongside the degree — but a letter cannot
say both, and the wrong one costs you either a graduate role or a placement. Right now
`personal.yaml` records "available immediately, full-time" and flags
`availability_conflict: UNRESOLVED`.

Pick one:

1. **Full-time now**, MSc alongside — what the file currently says.
2. **From July 2027** — graduate-intake and placement framing, matching the Alpine letter.
3. **Both, conditionally** — give me the rule ("full-time now for remote roles,
   Jul 2027 for anything requiring relocation") and it becomes a field the letter reads.

---

## B — Your work authorisation is recorded narrowly, on purpose 🕐 2 min

You said "work authorization in Portugal", so the file says exactly that:
`"Authorised to work in Portugal"`. Nothing will claim more.

But three of your five relocation targets are outside Portugal, and the Alpine role is in
**Enstone, UK** — a post-Brexit hire that needs its own visa story regardless of your
Portuguese status. If you hold something that carries wider rights — permanent residence,
EU long-term resident status, an EU passport — say which and I will widen the sentence.
If you do not, this is the correct wording and we leave it.

Also worth a look: I inferred `relocation_targets: [Portugal, United Kingdom, Italy, EU,
remote-EU]` from where you have actually applied. Trim it if it is wrong.

---

## C — Read the nine stories aloud 🕐 15 min

This was Q6 and you asked how to do it. **It is a reading job, not a writing job.** I have
rewritten all nine in your register — the method and the three questions are written into
the bottom of `evidence.yaml`, so you do not need this file open to do it.

The short version: read each `story` out loud and ask (1) would I say this sentence,
(2) is anything here not true, (3) is the best part missing. Change what fails, then set
`needs_simon_rewrite: false`. A story you approved is as good as one you wrote.

Two of them — `ev.why_me_graduate` and `ev.working_style` — are the ones I said I would
not draft for you. I still would not have. But you had already written them, in your
Bending Spoons letter and your thesis notes, so I assembled from your sentences rather
than inventing. **Check those two hardest**, precisely because they read so naturally.

If you would rather do it together: name a block, say the story out loud in a message,
and I will format it. That is the fastest route and the output is entirely yours.

---

## D — Q10, which you left blank 🕐 10 min · for the developer, not me

1. **Canva → File → Download → PDF Print**, plus a full-zoom screenshot, so the template
   can match spacing exactly.
2. **Your jobs CSV column names.** Minimum: `title`, `company`, `location`, `description`,
   `url`. `description` must be the **full posting text, not a snippet** — every
   downstream stage reads it, and a truncated one caps the quality of everything after.
   If yours is truncated, fix that before anything else is built.
3. `taxonomy.yaml` is a proposal. Diff it against the developer's `taxonomy.py` when that
   exists and keep one.

---

## E — Two things that will change on their own 🕐 later

- **Thesis supervisor.** `education.yaml` says `not yet confirmed`, and
  `forbidden.yaml` blocks any wording implying the thesis is supervised or approved.
  Update it the day someone signs, and that block loosens automatically.
- **FreedomCode context.** One line on what the company does. Low priority — it never
  prints on the CV, it only gives a letter something concrete if a posting asks about
  stakeholder work.

---

## Noted, not a question

`row.spoken` is **104 of its 105 characters**. One character of slack. It is the only row
with none. If you ever add a language or reword it, the line overflows and the page goes
to two.
