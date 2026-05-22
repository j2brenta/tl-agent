# tl-agent — Makefile
# Conventions: every dev workflow has a make target; CI runs `make check`.

.DEFAULT_GOAL := help
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c

PYTHON := uv run python
PYTEST := uv run pytest

.PHONY: help
help: ## Show available targets
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# -------------------- env --------------------
.PHONY: install
install: ## uv sync (dev + mcp extras)
	uv sync --all-extras

.PHONY: lock
lock: ## refresh uv.lock
	uv lock

# -------------------- quality gates --------------------
.PHONY: fmt
fmt: ## ruff format
	uv run ruff format .

.PHONY: lint
lint: ## ruff lint + format check
	uv run ruff format --check .
	uv run ruff check .

.PHONY: type
type: ## mypy strict + pyright
	uv run mypy
	uv run pyright

.PHONY: unit
unit: ## unit tests (no integration)
	$(PYTEST) -m "not integration and not eval" -q

.PHONY: check
check: lint type unit ## full pre-commit gate — must pass before commit

.PHONY: test
test: ## all tests including integration (needs `make up`)
	$(PYTEST) -q

# -------------------- compose --------------------
COMPOSE := docker compose -f infra/docker-compose.yml

.PHONY: up
up: ## bring compose stack up + wait healthchecks
	$(COMPOSE) up -d
	@echo "==> waiting for healthchecks..."
	@bash infra/wait_healthy.sh

.PHONY: down
down: ## tear down compose stack (keeps volumes)
	$(COMPOSE) down

.PHONY: nuke
nuke: ## tear down compose stack AND volumes
	$(COMPOSE) down -v

.PHONY: logs
logs: ## tail compose logs
	$(COMPOSE) logs -f --tail=100

# -------------------- seed / snapshot / restore --------------------
.PHONY: seed
seed: ## seed GitLab + Jira mock + Mattermost + SQLite baselines
	$(PYTHON) -m services.jira_mock.seed
	$(PYTHON) -m services.mattermost_seed.seed
	bash infra/gitlab/seed.sh
	$(PYTHON) -m tl_agent.storage.db --init

.PHONY: snapshot
snapshot: ## pg_dump mattermost-postgres → fixtures/mattermost.dump (eval baseline)
	bash infra/mattermost/snapshot.sh dump

.PHONY: restore
restore: ## restore mattermost-postgres from snapshot (per-eval reset)
	bash infra/mattermost/snapshot.sh restore

# -------------------- run --------------------
.PHONY: run
run: ## run today's tech-lead loop
	$(PYTHON) -m tl_agent.cli run --date $$(date +%F)

.PHONY: web
web: ## start Phase 8 review UI on :8080
	uv run uvicorn tl_agent.web.app:app --reload --host 0.0.0.0 --port 8080

.PHONY: trace
trace: ## open Phoenix UI in browser
	@open http://localhost:6006 || xdg-open http://localhost:6006

# -------------------- evals --------------------
.PHONY: eval-quick
eval-quick: ## 5 quickest cases, <60s — gates commits
	$(PYTHON) -m evals.runner --suite quick

.PHONY: eval-full
eval-full: ## all 20+ cases
	$(PYTHON) -m evals.runner --suite full

.PHONY: eval-report
eval-report: ## render last eval run as markdown
	$(PYTHON) -m evals.runner --report

# -------------------- mcp --------------------
.PHONY: mcp
mcp: ## run the local MCP server (stdio)
	$(PYTHON) -m mcp.server
