# PitWall — the handful of commands worth memorising.
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
        searxng-stop searxng-logs searxng-pull groq-check app cli

help:  ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ─────────────────────────────── python ────────────────────────────────

venv:  ## create .venv and install everything
	python3.11 -m venv .venv 2>/dev/null || python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

test:  ## run the test suite (no network needed)
	$(PY) -m pytest

lint:  ## ruff
	$(PY) -m ruff check src app scripts tests

typecheck:  ## mypy
	$(PY) -m mypy

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

# ──────────────────────────────── app ──────────────────────────────────

app:  ## launch the Streamlit dashboard
	$(PY) -m streamlit run app/Home.py

cli:  ## show the CLI help
	$(PY) scripts/run_pipeline.py --help
