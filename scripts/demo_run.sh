#!/usr/bin/env bash
# scripts/demo_run.sh — one-shot end-to-end demo of the tech-lead loop.
#
# Brings the compose stack up, seeds Jira/GitLab/Mattermost/SQLite, posts a
# realistic bulk-transcript standup into Mattermost for a target date, runs
# the 8-phase pipeline, then prints links to every viewing surface.
#
# Idempotent: safe to re-run. `--reset` first wipes the SQLite state so
# `days_hot` counters / decisions start clean.
#
# Usage:
#   scripts/demo_run.sh                    # demo for $DEMO_DATE (defaults to 2026-05-26)
#   scripts/demo_run.sh 2026-06-01         # demo for an arbitrary date
#   scripts/demo_run.sh --reset            # wipe state first, then demo
#   scripts/demo_run.sh --reset 2026-06-01

set -euo pipefail

# ---------- args ----------
DEMO_DATE="${DEMO_DATE:-2026-05-26}"
DO_RESET=0
for arg in "$@"; do
  case "$arg" in
    --reset) DO_RESET=1 ;;
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) DEMO_DATE="$arg" ;;
    -h|--help)
      sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Colours only when stdout is a TTY.
if [ -t 1 ]; then
  C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_RST=$'\033[0m'
else
  C_BOLD=""; C_DIM=""; C_OK=""; C_WARN=""; C_RST=""
fi
say()  { echo "${C_BOLD}▸${C_RST} $*"; }
ok()   { echo "${C_OK}✓${C_RST} $*"; }
warn() { echo "${C_WARN}!${C_RST} $*"; }

# ---------- 0. prerequisites ----------
say "checking prerequisites"
command -v docker >/dev/null || { echo "docker not found"; exit 1; }
command -v uv     >/dev/null || { echo "uv not found";     exit 1; }

if [ ! -f .env ]; then
  warn ".env missing — copying from .env.example"
  cp .env.example .env
  warn "edit .env and set TLA_ANTHROPIC_API_KEY before re-running"
  exit 1
fi
if ! grep -qE '^TLA_ANTHROPIC_API_KEY=.+' .env && \
   [ -z "${TLA_ROUTER_CONFIG:-}" ] && ! grep -qE '^TLA_ROUTER_CONFIG=.+' .env; then
  warn "TLA_ANTHROPIC_API_KEY is empty in .env and no local router override set"
  warn "either set the key or set TLA_ROUTER_CONFIG=config/router.ollama.yaml"
fi
ok "prerequisites OK"

# ---------- 1. bring compose stack up ----------
say "bringing compose stack up (idempotent; first-run GitLab takes ~90s)"
make up
ok "stack healthy"

# ---------- 2. seed everything ----------
say "seeding Jira mock / GitLab / Mattermost / SQLite baselines"
# Capture admin token from seed stdout so we can write it back to .env.
SEED_OUT="$(mktemp)"
trap 'rm -f "$SEED_OUT"' EXIT
make seed | tee "$SEED_OUT"

ADMIN_TOKEN="$(awk -F': ' '/^admin token:/ { print $2 }' "$SEED_OUT" | tail -n1 | tr -d '[:space:]')"
CHANNEL_ID="$(awk -F': ' '/^standup channel_id:/ { print $2 }' "$SEED_OUT" | tail -n1 | tr -d '[:space:]')"

if [ -z "$ADMIN_TOKEN" ] || [ -z "$CHANNEL_ID" ]; then
  warn "could not parse admin token / channel id from seed output"
  warn "post the demo standup manually and re-run with the .env already set"
  exit 1
fi

# Persist token in .env so subsequent runs work without re-seeding.
if grep -qE '^TLA_MATTERMOST_TOKEN=' .env; then
  # macOS-friendly in-place sed.
  sed -i.bak -E "s|^TLA_MATTERMOST_TOKEN=.*|TLA_MATTERMOST_TOKEN=$ADMIN_TOKEN|" .env && rm -f .env.bak
else
  printf '\nTLA_MATTERMOST_TOKEN=%s\n' "$ADMIN_TOKEN" >> .env
fi
ok "seeded; mattermost admin token written to .env"

# ---------- 3. optional state reset ----------
if [ "$DO_RESET" = 1 ]; then
  say "wiping SQLite state (--reset)"
  # `make reset-state` deletes the db, re-applies schema, then re-seeds —
  # we already seeded, but it's idempotent.
  make reset-state
  ok "state reset"
fi

# ---------- 4. post the demo standup as tl-admin ----------
say "posting bulk standup for $DEMO_DATE as tl-admin"
read -r -d '' STANDUP_BODY <<EOF || true
Standup $DEMO_DATE

John:
Wrapped up ENG-12 yesterday — the publisher retry semantics turned out to be
straightforward once the rate limiter from ENG-1 was in place. Today picking
up ENG-19, the billing dashboard widget. No blockers.

Matt:
Still on ENG-9 (auth events stream). The migration path is bigger than I
expected — touching more services than the ticket scoped for. Blocked on
getting schema review from the platform team; I've pinged twice.

Alicia:
Finished ENG-4 chart refresh and got ENG-22 through review. Starting ENG-7
(admin page filters) today. Risk: still waiting on design feedback for the
filter UX, may have to make a call without it.

Karen:
ENG-5 consumer wiring is done, also did a small PAY-99 cleanup in the
ingestion path while I was in there. Today moving to ENG-10 (slack notifier).
No blockers.
EOF

# JSON-encode the message body via python (handles quoting / newlines safely).
# Trailing `VAR=val` args to `python -c` go to sys.argv, not the environment —
# so we export first.
export CHANNEL_ID STANDUP_BODY
POST_JSON="$(
  uv run python -c '
import json, os
print(json.dumps({"channel_id": os.environ["CHANNEL_ID"], "message": os.environ["STANDUP_BODY"]}))
'
)"

HTTP_CODE="$(
  curl -sS -o /dev/null -w "%{http_code}" \
    -X POST http://localhost:8065/api/v4/posts \
    -H "Authorization: Bearer $ADMIN_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$POST_JSON"
)"
case "$HTTP_CODE" in
  201|200) ok "standup posted (HTTP $HTTP_CODE)" ;;
  *)       warn "standup post returned HTTP $HTTP_CODE — Phase 1 will use seed data only"
esac

# ---------- 5. run the pipeline ----------
say "running 8-phase loop for $DEMO_DATE"
echo "${C_DIM}(this can take a minute — each phase logs ✓ as it finishes)${C_RST}"
uv run python -m tl_agent.cli run --date "$DEMO_DATE"

# ---------- 6. where to look ----------
echo
echo "${C_BOLD}═══════════════════════════════════════════════════════════════════${C_RST}"
echo "${C_BOLD}  Demo complete — $DEMO_DATE${C_RST}"
echo "${C_BOLD}═══════════════════════════════════════════════════════════════════${C_RST}"
echo
echo "  ${C_BOLD}Web UI:${C_RST}"
echo "    Brief        http://localhost:8080/brief?date=$DEMO_DATE"
echo "    Audit log    http://localhost:8080/decisions?date=$DEMO_DATE"
echo "    Sprint view  http://localhost:8080/sprint?date=$DEMO_DATE"
echo
echo "  ${C_BOLD}Traces:${C_RST}"
echo "    Phoenix UI   http://localhost:6006"
echo "    JSONL spans  traces/$DEMO_DATE/spans.jsonl"
echo
echo "  ${C_BOLD}SQLite peek:${C_RST}"
echo "    sqlite3 data/tl_agent.db \\"
echo "      \"select id, hotspot_id, proposed_mode, needs_review, substr(proposed_body,1,80) \\"
echo "       from decisions where date(created_at)='$DEMO_DATE' order by created_at;\""
echo
echo "  ${C_BOLD}Start the web server (separate terminal):${C_RST}  make web"
echo
