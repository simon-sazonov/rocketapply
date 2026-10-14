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
   - remove PitWall job pages 7–11, `_jobs.py` and the Home job block;
   - filter PitWall's active-run checks by command;
   - make Setup's "Reset database" networking-only, with a backup first (today it deletes every job table).
3. **Old servers:** they may still be running `app/Home.py` on :8501/:8502 and are broken since the move — stop them, then use the make targets.
4. **Two real applications generated during the live test** (Waymo ML Engineer P&P, Lokker DS) are in
   `output/2_needs_changes/`. They are tracked only in the verify DB — offer to record them in the real tracker.
5. **Letter PDFs for older runs:** offer `pitwall apply letters`.
6. **Separate task already running:** pin letter_repair to a second Groq model (today it runs ~95 s/sentence on local Ollama).
7. **SearXNG (Docker) was down.** The people search used DuckDuckGo, and the email domain was typed by hand in the People panel.

## New issues
(add here)
- **Scoring weights:** see `SCORING_WEIGHTS_REPORT.md`. Tuned weights were never applied; the weights in force are the defaults plus `fit` 5. RocketApply's "Measure the score against my ratings" button overstates the score (0.97), because `calibrate.examples()` counts CV_READY applications as 👍, even over an explicit 👎. The honest figure is 0.73. The fix is in §7.1 of that report.
