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

.PHONY: env
env: ## scaffold .env from .env.example (idempotent — never overwrites)
	@if [ -f .env ]; then \
		echo ".env already exists — leaving it alone"; \
	else \
		cp .env.example .env && echo "wrote .env — edit it with your tokens"; \
	fi

.PHONY: install-hooks
install-hooks: ## install .claude/hooks/pre_commit.sh as .git/hooks/pre-commit
	@if [ ! -d .git ]; then echo "not in a git repo"; exit 1; fi
	@mkdir -p .git/hooks
	@ln -sf ../../.claude/hooks/pre_commit.sh .git/hooks/pre-commit
	@chmod +x .claude/hooks/pre_commit.sh
	@echo "==> linked .git/hooks/pre-commit → .claude/hooks/pre_commit.sh"
	@echo "    next commit will run \`make check\` first (bypass: git commit --no-verify)"

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

# Which service profiles to boot. Override per-invocation, e.g.
#   make up PROFILES=mattermost          # mock chat only; real Jira+GitLab via .env
#   make up PROFILES=                     # nothing but Phoenix (all real)
# Named presets below wrap the common combinations.
PROFILES ?= mattermost,jira,gitlab

.PHONY: up
up: ## bring compose stack up + wait healthchecks (PROFILES= overrides services)
	COMPOSE_PROFILES="$(PROFILES)" $(COMPOSE) up -d
	@echo "==> waiting for healthchecks (profiles: $(PROFILES))..."
	@COMPOSE_PROFILES="$(PROFILES)" bash infra/wait_healthy.sh

.PHONY: up-bundled
up-bundled: ## preset: everything in containers (mock MM+Jira+GitLab + agent)
	$(MAKE) up PROFILES=mattermost,jira,gitlab,agent

.PHONY: up-mock-chat
up-mock-chat: ## preset: mock Mattermost only; point TLA_JIRA_*/TLA_GITLAB_* at real
	$(MAKE) up PROFILES=mattermost,agent

.PHONY: up-real
up-real: ## preset: only agent + Phoenix; all integrations real (Slack/Jira/GitLab via .env)
	$(MAKE) up PROFILES=agent

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
	$(PYTHON) -m tl_agent.cli init-db
	@# jira_mock loads fixtures at module import; restart so new state takes effect.
	@# Safe no-op when the container isn't running.
	@$(COMPOSE) restart jira_mock 2>/dev/null || true

.PHONY: reset-state
reset-state: ## wipe SQLite state, re-apply schema, then re-seed
	$(PYTHON) -m tl_agent.cli reset --confirm
	$(MAKE) seed

.PHONY: clean-mattermost
clean-mattermost: ## delete all posts from the Mattermost town-square channel
	@docker exec tla-mm-db psql -U mmuser -d mattermost -c \
	  "DELETE FROM posts WHERE channelid = (SELECT id FROM channels WHERE name = 'town-square' LIMIT 1);" \
	  && echo "==> town-square cleared" \
	  || echo "==> tla-mm-db not running — skipped"

.PHONY: snapshot
snapshot: ## pg_dump mattermost-postgres → fixtures/mattermost.dump (eval baseline)
	bash infra/mattermost/snapshot.sh dump

.PHONY: restore
restore: ## restore mattermost-postgres from snapshot (per-eval reset)
	bash infra/mattermost/snapshot.sh restore

# -------------------- run --------------------
.PHONY: status
status: ## show last run signals: commits, standups, tickets (DATE= optional)
	$(PYTHON) -m tl_agent.cli status $(if $(DATE),--date $(DATE),)

.PHONY: import-jira
import-jira: ## import active sprint from Jira into DB (DATE=YYYY-MM-DD overrides today)
	$(PYTHON) -m tl_agent.cli import-jira --date $(or $(DATE),$(shell date +%F))

.PHONY: run
run: import-jira ## run the team lead loop, importing Jira snapshot first (DATE=YYYY-MM-DD overrides today)
	$(PYTHON) -m tl_agent.cli run --date $(or $(DATE),$(shell date +%F))

.PHONY: web
web: ## start Phase 8 review UI on :8080 (host)
	uv run uvicorn tl_agent.web.app:app --reload --host 0.0.0.0 --port 8080

# -------------------- containerised agent --------------------
.PHONY: build-image
build-image: ## build the tl-agent application image
	$(COMPOSE) build agent

.PHONY: run-docker
run-docker: ## run the 8-phase loop in a container (DATE=YYYY-MM-DD overrides today)
	COMPOSE_PROFILES="$(PROFILES),agent" $(COMPOSE) run --rm agent \
		run --date $(or $(DATE),$(shell date +%F))

.PHONY: web-docker
web-docker: ## start the Phase 8 review UI in a container on :8080
	COMPOSE_PROFILES="$(PROFILES),agent" $(COMPOSE) up -d web
	@echo "==> review UI at http://localhost:8080"

.PHONY: cli-docker
cli-docker: ## run an arbitrary CLI subcommand in a container, e.g. `make cli-docker ARGS='status'`
	COMPOSE_PROFILES="$(PROFILES),agent" $(COMPOSE) run --rm agent $(ARGS)

.PHONY: demo
demo: ## end-to-end demo (up + seed + post standup + run); DATE= overrides
	@scripts/demo_run.sh $(if $(DATE),$(DATE),)

.PHONY: demo-fresh
demo-fresh: ## like `make demo` but wipes SQLite state first
	@scripts/demo_run.sh --reset $(if $(DATE),$(DATE),)

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
