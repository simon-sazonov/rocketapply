"""The personal job search — job_search_assistant_engineering_spec.md.

A pipeline, not an agent (§97). Each stage is its own module, and each is pure
where it can be, so the matching can change without touching a collector:

    sources ─► normalize ─► store ─► dedupe ─► hard filters ─► score ─► inbox

* `settings`   the owner's search profile (`config/job_search.yaml`)
* `companies`  the target-employer list and company-name normalisation
* `locations`, `experience`, `visa`, `skills`, `roles`   extraction, pure
* `normalize`  one stored posting -> the fields the rest of the system reads
* `dedupe`     the same vacancy from several sources -> one cluster
* `filters`    deterministic hard eligibility; unknown never rejects
* `scoring`    the explainable score — never a bare number
* `store`      SQL for everything above, and the inbox queries
* `tracker`    applications and their status history
* `run`        the whole thing as one tracked run
* `export`     CSV out, so nothing is locked in here
"""
