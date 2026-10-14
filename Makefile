# RocketApply — the handful of commands worth memorising.
#
# ALWAYS use .venv/bin/python. The system `python3` on this machine is Anaconda
# and its pydantic-settings predates the `NoDecode` import in
# src/pitwall/config.py, so `import pitwall` fails with a misleading ImportError.

PY       := .venv/bin/python
COMPOSE  := docker compose -f docker/searxng/docker-compose.yml
SXNG_ENV := docker/searxng/.env
SXNG_URL := http://localhost:8888

.DEFAULT_GOAL := help
.PHONY: help venv test lint typecheck searxng searxng-env searxng-check \
        searxng-stop searxng-logs searxng-pull groq-check run rocketapply cli \
        job-search job-rerank job-calibrate job-audit job-targets-ats fonts validate-profile calibrate

help:  ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ─────────────────────────────── python ────────────────────────────────

venv:  ## create .venv and install everything
	python3.11 -m venv .venv 2>/dev/null || python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt
	# Editable, and not optional. Without it `pitwall` is not a command, so
	# every `pitwall ...` line in the README is unrunnable and the only working
	# entry point is `python scripts/run_pipeline.py`. pytest passed anyway
	# because pyproject sets `pythonpath = ["src"]`, which is why this went
	# unnoticed.
	$(PY) -m pip install -e .

test:  ## run the test suite (no network needed)
	$(PY) -m pytest

lint:  ## ruff
	$(PY) -m ruff check src apps scripts tests

typecheck:  ## mypy
	$(PY) -m mypy

# ─────────────────────── cv + cover letter ─────────────────────────────

fonts:  ## vendor DM Sans / Arimo / Archivo Black into assets/fonts
	$(PY) scripts/fetch_fonts.py

validate-profile:  ## frame compliance and data integrity for profile/
	$(PY) profile/validate_profile.py

calibrate:  ## re-measure the CV frame with real font metrics; fails on drift
	$(PY) scripts/run_pipeline.py apply calibrate

# ────────────────────────────── searxng ────────────────────────────────

$(SXNG_ENV):
	@printf 'SEARXNG_SECRET=%s\n' "$$($(PY) -c 'import secrets; print(secrets.token_hex(32))')" > $(SXNG_ENV)
	@printf 'SEARXNG_HOST_PORT=8888\n' >> $(SXNG_ENV)
	@echo "wrote $(SXNG_ENV) with a fresh secret"

searxng-env: $(SXNG_ENV)  ## generate docker/searxng/.env if it is missing

searxng: searxng-env  ## start the local SearXNG container
	$(COMPOSE) up -d
	@echo "waiting for $(SXNG_URL)/healthz ..."
	@for i in $$(seq 1 45); do \
	  if curl -fsS "$(SXNG_URL)/healthz" >/dev/null 2>&1; then echo "  up"; exit 0; fi; \
	  sleep 2; \
	done; \
	echo "  never became healthy — check: make searxng-logs"; exit 1

searxng-check: ## prove the JSON API answers and operator queries work
	$(PY) scripts/check_searxng.py

searxng-stop:  ## stop the container
	$(COMPOSE) down

searxng-logs:  ## tail the container logs
	$(COMPOSE) logs -f --tail=100

searxng-pull:  ## update the image
	$(COMPOSE) pull

# ──────────────────────────────── llm ──────────────────────────────────

groq-check:  ## list the Groq models your key can reach and verify LLM_MODEL
	$(PY) scripts/setup_groq.py

# ───────────────────────────── job search ──────────────────────────────

job-search:  ## the daily job inbox: poll every source, filter, rank
	$(PY) scripts/run_pipeline.py job-search

job-rerank:  ## re-rank stored jobs after editing the profile (no network)
	$(PY) scripts/run_pipeline.py job-search --no-poll

job-calibrate:  ## how well the score agrees with your ratings (proposes weights)
	$(PY) scripts/run_pipeline.py job-calibrate

job-audit:  ## is the Search list worth reading? measured against the audit labels
	$(PY) scripts/run_pipeline.py job-audit

job-targets-ats:  ## find the public ATS board of each target company
	$(PY) scripts/discover_ats.py --targets

# ──────────────────────────────── app ──────────────────────────────────
# Launched from the repo root so the engine's relative output/, run/ and data/
# paths resolve; the theme is in apps/rocketapply/.streamlit/config.toml.
# The database is DB_PATH in .env (default data/pitwall.db).

rocketapply:  ## RocketApply — search, generate, apply, connect, on :8502
	$(PY) -m streamlit run apps/rocketapply/app.py --server.port 8502

run: rocketapply  ## same as `make rocketapply`

cli:  ## show the CLI help
	$(PY) scripts/run_pipeline.py --help
