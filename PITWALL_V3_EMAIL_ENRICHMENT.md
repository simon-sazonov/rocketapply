# PitWall v3 — Contact Enrichment

*Rewritten 1 September 2026. **This file replaces the earlier v3 draft** — Snov.io is out,
and the provider order is inverted. Hand to Claude Code in `networking_lead_finder/`.
Assumes v2 merged (SearXNG + Groq + jobs layer + profiles). Schema is at version 4.*

---

## 0. Before any code

### 0.1 This document reverses a v2 constraint

`PITWALL_V2_IMPLEMENTATION.md` §0 constraint 1 says *"No email or phone discovery. At all."*
The owner has re-decided. Your first commit, on its own, edits that constraint to:

> **Contact enrichment** is permitted per `PITWALL_V3_EMAIL_ENRICHMENT.md`: public-source
> discovery, pattern inference, and the Hunter.io API under the owner's own account.
> Independent SMTP probing, site crawling for addresses, and phone numbers remain prohibited.

Add a pointer in Appendix C to §0.3 here. Do not leave the two documents contradicting each
other — a future session reading only the v2 brief will refuse this work.

### 0.2 The shape of the feature

The owner's flow, exactly:

1. Search for people as today. **Nothing in the existing pipeline changes.**
2. On the Leads tab, select one person or several.
3. Click to find emails. **PitWall's own free logic runs first** — no Hunter, no credits.
4. Each person gets a status: **verified**, **probable**, **guess**, or **not found**.
5. For anyone below `verified`, the owner may *individually* click to escalate to Hunter,
   spending one credit, deliberately, per person.
6. Hunter's real remaining credits are visible in the UI at all times.

Enrichment is **never** part of the search pipeline and **never** runs in bulk over the
whole table. It is a manual tool on a manual selection.

### 0.3 Still prohibited

- **Independent SMTP verification.** No `RCPT TO`, no port-25 connections. §3 explains why
  this is an engineering decision as much as a policy one.
- **Site crawling for addresses.** Reading a page the search index already pointed at is in
  scope; walking a domain harvesting mailto: links is not.
- **Phone numbers.** Hunter returns them. Do not request, store, or display them.
- **Bulk enrichment**, and **sending**. Both unchanged and permanent.

### 0.4 Credentials

`HUNTER_API_KEY` in `.env` (already gitignored). Never in this file, committed code, test
fixtures, logs, or exception messages — redact it in anything the HTTP layer re-raises.

---

## 1. What the commercial tools actually do — and the part you cannot copy

Apollo describes the industry workflow as four steps: **domain resolution → pattern
inference → database match → verification**, with a "waterfall" across multiple vendors
worth 8–18 percentage points of extra coverage.

You can build steps 1, 2 and a real version of 4. **You cannot build step 3.** Apollo's
275M-record contact database *is* the product; Hunter's is 107M. That is the whole moat and
it is not reproducible.

**What replaces the database here:** the owner is already running an unlimited local
SearXNG instance. A search index is a serviceable substitute for a contact database when
the volume is 25 people a month rather than 25,000. That is the core idea of this feature —
**evidence mining, not probing.**

---

## 2. The free discovery engine

Order matters: everything here costs nothing, so it all runs before Hunter is ever offered.

### 2.1 Domain resolution (free)

Never spend a Hunter credit learning a domain. In order:

1. `companies.careers_url` — exists since v2
2. Company website already captured in SERP data
3. A SearXNG query for the company's official site
4. `company_domains.manual_override`

Cache per company. A wrong domain poisons everything downstream, so surface the resolved
domain in the UI and let the owner correct it.

### 2.2 Harvest known addresses at the domain (free, SearXNG)

**This is the highest-value step and the one most people miss.** You do not need the target
person's address to help the target person — you need *any two* addresses at their company,
because two addresses reveal the format.

Queries to run per domain (operators work now — that was the v2 unlock):

```
site:{domain} "@{domain}"
"@{domain}" -site:linkedin.com
"{company}" "@{domain}" contact
```

Extract every `…@{domain}` string from SERP titles and snippets. Where a snippet is
truncated, fetching that one indexed page to confirm is in scope; crawling the domain is not.
Respect `robots.txt`. Store each harvested address in `domain_address_samples`.

### 2.3 Pattern inference (free)

From the harvested samples, infer the format by matching each known address against its
owner's name: `first.last`, `first`, `flast`, `first_last`, `f.last`, `last.first`,
`firstl`, and the rest of the standard set.

Record `pattern`, `pattern_sample_size`, and agreement rate. **Sample size is the confidence
signal** — a pattern from one address is a guess, from three is close to fact. Never present
n=1 and n=5 identically.

Then apply the pattern to the target's name → candidate address.

### 2.4 GitHub commit emails (free, high precision)

For software, simulation and vehicle-dynamics people this is the single best source, because
git makes the author email public by design. Two confirmed methods:

- `https://api.github.com/users/{user}/events/public` — push events carry commit author emails
- `https://github.com/{owner}/{repo}/commit/{sha}.patch` — the `From:` header carries it

Rate limit is 60/hour unauthenticated, 5,000/hour with a free personal token
(`GITHUB_TOKEN`, optional). **Filter out `…@users.noreply.github.com`** — GitHub issues those
precisely so the address is not contactable, and storing one as a finding is a bug.

Only attempt this when a GitHub identity is already associated with the candidate. Do not go
hunting for GitHub accounts by name — the false-match rate is high and a wrong person's
address is worse than none.

### 2.5 Corroboration search (free) — this is your verification

Once you have a candidate address, search for the **exact string**:

```
"{candidate_address}"
"{first} {last}" "{candidate_address}"
```

If the address appears published anywhere public — a press release, a team page, a
conference programme, a paper — that is **stronger evidence than an SMTP probe**, because it
proves the address exists *and* that the person publishes it. This is what earns the
`verified` status without spending a credit.

---

## 3. Verification: what is actually possible

### 3.1 Why SMTP probing is not being built

This is an engineering finding, not squeamishness. Four independent reasons, any one of
which is disqualifying:

1. **Residential ISPs block outbound port 25.** From the owner's MacBook on home or café
   wifi, the connection will very likely never open. Docker does not change this.
2. **Production verifiers rotate IPs.** Mail servers return deliberately ambiguous codes
   (252, 421) after repeated `RCPT TO` from one address. A single laptop IP is exactly the
   pattern they throttle.
3. **The big hosts are unreliable or hostile.** Google/Workspace answers cleanly, but
   Microsoft 365 returns 250 for invalid addresses and rate-limits after a few probes;
   Yahoo/AOL return 250 or 252 regardless of whether the mailbox exists; iCloud throttles
   within a few hundred. Most corporate mail sits behind Google or Microsoft.
4. **Catch-all domains defeat it entirely** — they 250 everything, so the answer is
   "unknown" no matter how clean the probe.

Even done perfectly it yields graded confidence, never a binary. Given that, the credit-based
verifier is both more accurate and less risky.

### 3.2 What to build instead (all free)

| Check | Method | What it proves |
|---|---|---|
| Syntax | RFC-shaped validation | the string is a possible address |
| **MX lookup** | DNS query on the domain | the domain accepts mail at all, and *who hosts it* |
| Disposable/free-provider | static list | it is not a throwaway or a personal gmail |
| Role-address detection | `info@`, `press@`, `hr@` | it is a shared mailbox, not a person |
| **Corroboration** | §2.5 exact-string search | **the address is real and published** |

DNS runs on port 53 and is never blocked, so MX lookup works everywhere. The MX hostnames
also tell you the mail host — useful context to display, and a signal that Hunter
verification will be more or less reliable for that domain.

---

## 4. The status model

Four statuses, each defined by evidence, and each rendered so the owner can never mistake a
guess for a fact.

| Status | Earned by | UI |
|---|---|---|
| **verified** | corroboration search found the exact address published, **or** Hunter returned it as deliverable | green; safe to write to |
| **probable** | pattern from **≥2** samples + MX valid + not role/disposable | amber; "pattern (n=3), unconfirmed" |
| **guess** | pattern from n=1, or a generic permutation, MX valid | grey; "guess, unconfirmed" |
| **not found** | no domain, no samples, nothing published | offer Hunter escalation |

**The label must always carry its evidence.** Never render a bare confidence percentage for
a pattern guess — show `pattern (n=2)` or `published: <source domain>`. A number invites the
owner to trust a hypothesis.

Anything below `verified` shows a **"Check with Hunter (1 credit)"** button on that row.

---

## 5. Hunter — manual escalation only

### 5.1 Live credit display, from Hunter not from a local guess

`GET https://api.hunter.io/v2/account` returns the plan, the reset date, and used/available
counts. Poll it on page load and after every spend, cache for a few minutes, and show in the
Leads sidebar:

```
Hunter · 34 of 50 credits left · resets 1 Oct
```

Free tier is **50 credits/month**, and finding, verifying and domain search all draw from the
same pool. No overage. When credits reach zero, disable the escalation buttons with the reset
date shown, rather than letting a click fail.

### 5.2 Spend credits on domains, not people

Hunter's **Domain Search** returns the company's email `pattern` plus sample addresses.
**One call can unlock free pattern-based addresses for every candidate at that company** —
whereas Email Finder spends a credit per person.

So the escalation UI should offer both, and recommend the domain call when the owner has
≥3 unresolved people at the same company:

- *"Find this person (1 credit)"* → Email Finder
- *"Learn Alpine's pattern (1 credit) — unlocks 7 people"* → Domain Search

**Verify the actual credit cost of each endpoint against Hunter's current docs before
building the preview.** If Domain Search costs more than Email Finder, the recommendation
threshold changes; do not guess it.

Feed any addresses Hunter returns back into `domain_address_samples`, so a paid call
permanently improves the free engine.

---

## 6. Data model — migration to schema version 5

```
contact_emails
  id, candidate_id FK→candidates ON DELETE CASCADE,
  email, status (verified|probable|guess|not_found),
  source (corroborated|pattern|github|hunter_finder|hunter_domain|manual),
  evidence_json      -- pattern used, sample size, corroborating URL, MX host
  confidence         -- provider's own score ONLY; NULL for locally-derived
  mx_valid, is_role_address, is_disposable,
  retrieved_at, raw_json (key redacted)
  UNIQUE (candidate_id, email)

company_domains
  id, company_id FK, domain, source, pattern, pattern_sample_size,
  pattern_agreement, last_harvested_at, manual_override
  UNIQUE (company_id, domain)

domain_address_samples          -- the corpus that makes patterns free
  id, company_domain_id FK, email, person_name, source_url, found_at
  UNIQUE (company_domain_id, email)

enrichment_log
  id, candidate_id, step, provider, credits_spent, outcome, created_at
```

Credit accounting **reuses the existing `quota` table** — add a `hunter` provider row. Do not
build a parallel counter. `enrichment_log` is the audit trail; `quota` is the budget.

### 6.1 Blocklist, retention and export — same commit as the migration

These already exist and currently ignore contact data:

- Blocklisting a candidate **hard-deletes** their `contact_emails`. The blocklist is the
  erasure mechanism; it must erase.
- `purge_stale.py`'s 180-day rule covers `contact_emails` for untouched candidates.
- `export.py` **excludes email columns by default**, behind `--include-contacts` with a
  warning. Silent export is how this data leaks.

---

## 7. Package and UI

### 7.1 `src/pitwall/enrich/`

Mirror the conventions already established in `search/` and `sources/jobs/`:

```
enrich/
  __init__.py     step registry + chain (copy sources/jobs/__init__.py shape)
  base.py         EnrichmentStep protocol, EmailCandidate result model
  domains.py      domain resolution
  harvest.py      SearXNG address harvesting → domain_address_samples
  patterns.py     format inference + application
  github.py       commit-email lookup (optional GITHUB_TOKEN)
  corroborate.py  exact-string search — the free verifier
  checks.py       syntax, MX, disposable, role-address
  hunter.py       Hunter adapter: account, email-finder, domain-search
  cache.py        permanent cache — a spent credit is never spent twice
```

Reuse `ratelimit.py`. Every step is independently failable and non-fatal: log it, record it,
continue to the next.

### 7.2 Leads page

1. Checkbox column on the existing grid.
2. **"Find emails for selected (N)"** — runs the free engine only. No confirmation needed;
   it costs nothing. Show progress; it involves several searches per person.
3. Result rows: address, status chip, evidence string, source.
4. Per-row **"Check with Hunter (1 credit)"** on anything below `verified`.
5. Sidebar: live Hunter credits and reset date.
6. When ≥3 people at one company are unresolved, surface the domain-search suggestion (§5.2).

### 7.3 CLI

`pitwall enrich --candidate-id … [--hunter] [--dry-run]` in `cli.py` (the CLI lives in the
package now, not `scripts/`). Without `--hunter` it never spends. Opens a `runs` row, per v2.

---

## 8. Testing

- **No test makes a real network call** — not Hunter, not GitHub, not SearXNG, not DNS.
  Fixtures in `tests/fixtures/enrich/`, provider key redacted.
- Pattern inference table-test: a set of known name→address pairs per format, asserting the
  right format is inferred and that n=1 is not reported as confident.
- **`…@users.noreply.github.com` is filtered out** — explicit test.
- Role addresses (`info@`, `press@`) are flagged, not returned as a person's address.
- Hunter out-of-credits (402/403) produces a clean disable, not a crash. This *will* happen —
  it is 50 credits a month — so it needs a real test.
- Credit preview matches actual spend.
- Blocklist deletes contact rows; export omits them by default.
- All 581 existing tests stay green.

---

## 9. Acceptance criteria

- [ ] v2 brief edited and committed separately
- [ ] Migration 4 → 5 clean on the live DB; `integrity_check` ok
- [ ] Free engine resolves a domain, harvests ≥2 samples, infers a pattern, and returns a
      `probable` address for a real candidate **without spending a credit**
- [ ] A corroborated address reaches `verified` with its source URL shown
- [ ] Statuses never render a guess as a fact; every label carries its evidence
- [ ] Hunter credits shown live from `/v2/account`; buttons disable at zero with reset date
- [ ] A Hunter domain-search result feeds `domain_address_samples` and makes subsequent
      people at that company free
- [ ] No SMTP code exists anywhere in the tree
- [ ] Blocklist erases; purge covers; export excludes by default
- [ ] 581 + new tests green, no network in the suite

---

## 10. Order of work

1. v2 brief edit + commit
2. Migration + blocklist/retention/export + tests
3. `checks.py`, `domains.py` — free, no external calls beyond DNS
4. `harvest.py` + `patterns.py` — the core of the free engine
5. `corroborate.py` — the free verifier
6. Leads UI + statuses
7. `hunter.py` + credit display + escalation buttons
8. `github.py` — optional, last

Steps 3–6 are the feature. Stop there and evaluate the hit rate before wiring Hunter; if the
free engine resolves most people, the 50 credits stretch a very long way.
