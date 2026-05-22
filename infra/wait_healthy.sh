#!/usr/bin/env bash
# Wait until all compose services with healthchecks report healthy.
# Polls `docker compose ps` rather than relying on `--wait` so we can surface
# a per-service status line.

set -euo pipefail

COMPOSE="docker compose -f infra/docker-compose.yml"
TIMEOUT_SECONDS=${TIMEOUT_SECONDS:-300}

services=$($COMPOSE config --services)

deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))

while true; do
    all_healthy=true
    for svc in $services; do
        # health is one of: starting | healthy | unhealthy | (empty = no healthcheck)
        status=$($COMPOSE ps --format '{{.Health}}' "$svc" 2>/dev/null | head -1 || true)
        case "$status" in
            healthy|"") ;;  # healthy, or no healthcheck defined
            *) all_healthy=false; echo "  $svc: $status" ;;
        esac
    done

    if $all_healthy; then
        echo "==> all services healthy"
        exit 0
    fi

    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "==> healthcheck timeout after ${TIMEOUT_SECONDS}s" >&2
        $COMPOSE ps
        exit 1
    fi

    sleep 3
done
