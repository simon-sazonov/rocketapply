# Handoff — PitWall / RocketApply split (29 Sep 2026)

Start a new chat with: "Read HANDOFF_ROCKETAPPLY.md and continue."

## Where things are
- Branch `split` (local, not pushed), 13 commits after tag `pre-split-2026-09-29`.
  Not merged into `job-search` or `main` yet.
- Plan: `~/.claude/plans/i-want-to-rebuild-serene-lamport.md`
- Memory note: `rocketapply-split` (auto-loaded).
- Apps, launched from the repo root:
  - `make pitwall` (:8501) — `apps/pitwall/`, dark, unchanged
  - `make rocketapply` (:8502) — `apps/rocketapply/`, light
  - Preview configs are in `.claude/launch.json`: `rocketapply` (:8504, real DB) and
    `*-verify` (frozen copy in `data/verify/`).
- Backups are in `data/backups/`: `pre-split`, `before-rerank-v10`, and output/run as a tgz.

## Open items / decisions
1. **Merge:** decide how `split` goes back (into `job-search`, then `main`?).
2. **Final phase (needs Simon's OK after he has used RocketApply):**
   - remove PitWall job pages 7–11, `_jobs.py` and the Home job block — **still open, not okayed yet**;
   - ~~filter PitWall's active-run checks by command~~ — **done 14 Oct** (see below);
   - ~~make Setup's "Reset database" networking-only, with a backup first~~ — **done 14 Oct**.
3. ~~**Old servers:**~~ **done** — stopped on 29 Sep (SCORING_WEIGHTS_REPORT §7.3); RocketApply alone answers on :8502. Start PitWall with `make pitwall`.
4. **Two real applications generated during the live test** (Waymo ML Engineer P&P, Lokker DS) are in
   `output/2_needs_changes/`. They are tracked only in the verify DB — offer to record them in the real tracker.
5. **Letter PDFs for older runs:** offer `pitwall apply letters`.
6. **Separate task already running:** pin letter_repair to a second Groq model (today it runs ~95 s/sentence on local Ollama).
7. **SearXNG (Docker) was down.** The people search used DuckDuckGo, and the email domain was typed by hand in the People panel.

## New issues
(add here)
- **Scoring weights:** see `SCORING_WEIGHTS_REPORT.md`. Tuned weights were never applied; the weights in force are the defaults plus `fit` 5. ~~RocketApply's "Measure the score against my ratings" button overstates the score (0.97)…~~ **Fixed 14 Oct** (§7.1): an explicit 👍/👎 always wins, and an application counts as good only once it went out (`tracker.SUBMITTED`). The button should now show about 0.73; not re-measured on the real DB yet.

## Done on 14 Oct 2026 (branch `split`)
- `755bb00` WIP commit of the uncommitted work since 29 Sep (worker, Search audit, outreach templates, these two reports). `config/companies.csv.bak-20261014` left out on purpose.
- `83f3d65` `make lint` checks `apps/`, not the `app/` that moved.
- `ed2df5e` calibration (§7.1): explicit rating wins; only APPLIED, ONLINE_ASSESSMENT, INTERVIEW, FINAL_INTERVIEW, OFFER, REJECTED and WITHDRAWN count as good. SHORTLISTED, CV_READY and IN_PROCESS don't count. Test in `tests/test_job_foundations.py`.
- `a9e851c` run kinds listed once in `db.py` (`NETWORKING_COMMANDS`, `JOB_SEARCH_COMMANDS`). PitWall's Home banner, Find People Start/Stop and People page now ask only about networking runs, so a RocketApply job search no longer shows as a PitWall run, and Find People's Stop can no longer cancel it. `RunRepo.latest(commands=…)`. A test checks that every `tracked()` kind belongs to one app.
- `96702a9` Setup's "Reset database" → `db.reset_networking()`. It takes an online backup into `data/backups/<db>-before-reset-<ts>.db` and checks it, then clears candidates (+ sources, scores, CRM, contact emails), search results, queries and PitWall's runs. It keeps jobs, ratings, applications, the blocklist, companies/roles, the email-domain corpus and the credit ledgers. It refuses while a PitWall run is going. Tested on temp DBs only.

## Still open
- Remove PitWall job pages 7–11, `_jobs.py` and the Home job block (needs Simon's OK).
- Merge plan for `split` → `job-search` → `main` (item 1).
- Record the Waymo / Lokker applications in the real tracker (item 4), and letter PDFs for older runs (item 5). Both need Simon's yes.
- Re-measure calibration on the real DB with the §7.1 fix (`make job-calibrate`).
- An uncommitted `queries.pending(company_ids, role_ids)` change in `db.py`/`pipeline.py` from another session on 14 Oct was left in the working tree for that session to commit.
