# `companies.yaml` — the one thing the machine cannot write

**Written 25 Sep 2026**, updated the same day with the automatic research pass
(§2a). Everything here is checked against the code that reads the file, not
against how it ought to work.

---

## 1. Why this file exists

Five letters were graded in detail on 25 September. The voice scored **4.54 out
of 5** and the rewrite burden **27%**, which clears the bar for sending. One
category scored badly in every single letter:

| | Anthropic | Wayve | Meetdavis | Predium | Helsing |
|---|---|---|---|---|---|
| company motivation /10 | 6.5 | 5.5 | 5.5 | 7.0 | 6.5 |

The reason is mechanical, not stylistic. `companies.yaml` is empty, so the
planner is told *"(none — do not invent one)"* on every run, and the letter
closes with a sentence that would fit any employer:

> "I would welcome the chance to discuss how my rigorous experimentation and
> end-to-end pipeline work align with Meetdavis's needs."

Delete the company name and nothing breaks. That is the test this file exists to
pass.

**v6 already fixed half of this.** A new `role_attraction` field builds the
closing from the responsibilities the posting itself lists, so a v6 letter opens
its last paragraph with the actual work. That needs no research and it now
happens automatically.

What it **cannot** do is know anything that is not in the posting: a paper they
published, a product decision, a talk their team gave. That is this file, and no
model can produce it, because the information is not in front of it.

---

## 2. What good looks like

Your own Alpine letter is the standard. The reason it works, from your notes:

> "The company paragraph cites a specific January announcement and connects it
> to his thesis, which is the opposite of praising their culture."

Two halves, and both are needed:

1. **A specific, checkable fact about them.** Dated if possible.
2. **The honest connection to work you have actually done.**

Compare:

```
✗  "Anthropic's focus on safe, high-performance AI systems matches my
    experience building reliable RL pipelines."
```
True of a hundred applicants, and it was the sentence the reviewer called the
most AI-sounding in the whole letter.

```
✓  "The Code RL posting describes designing environments and reward signals and
    then debugging why a model does or does not improve. That is the loop I
    spent the sepsis project inside: the reward arrived only at episode end, and
    the interesting work was diagnosing why the more complex method lost to a
    linear baseline."
```
Could not have been written about anyone else, and every clause is defensible in
an interview.

---

## 2a. What the machine now does, and what it still cannot

`pitwall apply research <company>` fills this slot automatically when you have
written no entry. It searches, throws away every result that does not name the
employer, refuses to call a model at all unless three on-topic sources survive
across two domains, drafts two or three sentences, and then checks each one
against a single source before keeping it. The result lands in
`data/cache/research/`, **never in this file**, and a hand-written entry here
always wins.

```bash
pitwall apply research Anthropic Helsing     # research and cache
pitwall apply research Meetdavis --dry-run   # what the model would see; no model call
pitwall apply research --show                # what is cached, and what was rejected
```

A batch runs the same pass over its distinct employers before the first letter,
so this is automatic in practice.

**What it produces is a draft, not a replacement for §6.** It reads search
snippets and at most two pages from the employer's own domain, so it finds what a
company says about itself. It does not find the paper you read, the talk you went
to, or the connection to your own work — the `angle` half. What it is for is
turning the fifteen minutes in §6 into five: run it, read the sentences and their
source URLs, and where one is genuinely good, **promote it into this file** with
your own `angle` beside it. From then on the machine never touches that employer
again.

Where it finds nothing it says so and the letter says nothing, which is §7's
first rule enforced rather than trusted.

---

## 3. The format

A YAML list. The file currently contains `[]` and a commented template.

```yaml
- id: co.anthropic
  name: Anthropic
  aliases: []
  notes: >
    What their team actually works on, in your words. From their engineering
    blog, their papers, or the posting itself. Specific enough that it could not
    describe a competitor.
  angle: >
    The honest connection to your own work. Not "I am passionate about their
    mission" — something like "the same class of problem as the sepsis work: a
    sequential decision where the feedback arrives far too late to be useful".
  source_urls:
    - https://www.anthropic.com/research/...
```

| field | required | what it does |
|---|---|---|
| `id` | yes | Your reference. `co.<something>`. Also used for matching. |
| `name` | yes | The employer. Matched against the posting — see §4. |
| `aliases` | no | Other names they post under. Needed less often than you think. |
| `notes` | yes | The specific fact. **Reaches the planner.** |
| `angle` | yes | The connection to your work. **Reaches the planner.** |
| `source_urls` | no | Where you read it. For you in six months, not for the model. |

Only `notes` and `angle` reach the model. They are concatenated with a space and
handed to the planner as `COMPANY NOTES`, which then writes `company_angle`,
which the letter writer uses. Everything else is bookkeeping.

---

## 4. How the name is matched — read this before writing ten entries

Matching is on the **normalised** name, so the legal suffix does not matter:

```
"Predium Technology GmbH"  →  predium
"Predium"                  →  predium     ← these match
```

So `name: Predium` catches a posting that says `Predium Technology GmbH`. Case
is ignored. Accents are folded. `id` is matched too, with `co.` stripped.

**What normalising does NOT catch** is a genuinely different word:

```
"Meta"              →  meta
"Meta Platforms"    →  meta platforms     ← these do NOT match
```

That is what `aliases` is for. If you are unsure, check before writing prose:

```bash
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); from pitwall.apply.loader import load_profile; from pitwall.apply.planner import _company_notes; print(repr(_company_notes(load_profile('profile'), 'Predium Technology GmbH')))"
```

Empty output means the entry is not being found. An entry that never matches is
worse than no entry at all: you did the reading, the letter did not change, and
nothing tells you why.

---

## 5. Which companies to write, in order

From your current shortlist, by how many open postings each has. Doing the top
six covers most of what a batch will ever generate:

| company | postings | worth it? |
|---|---|---|
| OpenAI | 65 | yes — largest single share of the shortlist |
| Anthropic | 37 | yes |
| Waymo | 17 | yes |
| Helsing | 14 | yes |
| Wayve | 12 | yes |
| Celonis | 12 | yes |
| Prior Labs | 4 | if you are genuinely interested |
| Isomorphic Labs | 4 | if you are genuinely interested |
| Meetdavis | 3 | you have already applied |
| Databricks | 3 | later |

**Six entries covers roughly 157 of the postings in your shortlist.** That is
about ninety minutes of reading, once, and it is the difference between a batch
of a hundred letters sharing one weak paragraph and a hundred letters that each
say something true about where they are going.

---

## 6. How to write one, in fifteen minutes

1. **Ten minutes reading.** Their engineering blog, a recent paper, a talk, or
   the posting's own responsibilities section. Look for one thing that is
   concrete and dated.
2. **Write `notes` in your own words** — two or three sentences. If you would be
   embarrassed to have it read back to you in an interview, it is too vague.
3. **Write `angle` last**, because it depends on `notes`. One sentence linking
   their problem to something you actually built. Name the shared *constraint*,
   not the shared subject.
4. **Check it matches** with the command in §4.
5. **Regenerate one letter** for that company and read the last paragraph.

---

## 7. Four rules

**Never invent enthusiasm.** An empty entry is fine and the planner handles it —
`company_angle` returns an empty string and the writer says nothing about the
company at all. Silence beats flattery, and the prompt says so explicitly.

**Never write about culture.** "Collaborative environment", "every voice is
valued", "innovative approach" — your own Red Bull letter is in `style.yaml` as
an `avoid` exemplar for exactly this, and the note on it reads: *"Flattery
instead of evidence."*

**Never claim a domain you do not have.** You have no real-estate experience and
no defence experience. The honest version is stronger: *"I like problems where
messy real-world data has to become a reliable analytical system."* That is true
of Predium and of Helsing and it is defensible in both interviews.

**Keep it checkable.** Everything in `notes` should be something you could point
at. If they ask "where did you read that?", there has to be an answer, which is
what `source_urls` is for.

---

## 8. After you edit

```bash
.venv/bin/python3 profile/validate_profile.py
```

The validator does not currently inspect `companies.yaml` — it is optional and
free-form — but it will catch a YAML syntax error that would otherwise surface
as an empty profile at generation time. The file is read fresh on every run, so
no restart is needed for the CLI. **Streamlit caches imported modules, so the
Studio and the Batch page need a server restart** after any edit under
`profile/`.

---

## 9. The sentence to keep in mind

> The posting tells the model what the job involves. This file tells it what you
> know that the posting does not. If a sentence could have been written without
> doing the reading, it does not belong here.
