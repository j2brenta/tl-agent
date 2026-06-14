#!/usr/bin/env bash
# Seed the in-compose GitLab with one project + a deterministic commit timeline.
#
# Idempotent: re-runs are safe (skips if the project already exists).
# Uses the root API token printed at first boot — fetch via:
#   docker exec tla-gitlab grep 'Password:' /etc/gitlab/initial_root_password

set -euo pipefail

# Load .env if present (same source of truth as the Python app).
REPO_ROOT_FOR_ENV="$(cd "$(dirname "$0")/../.." && pwd)"
if [ -f "${REPO_ROOT_FOR_ENV}/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "${REPO_ROOT_FOR_ENV}/.env"
    set +a
fi

GITLAB_URL="${TLA_GITLAB_BASE_URL:-${GITLAB_URL:-http://localhost:8929}}"
# Prefer the seed-time admin token; fall back to the runtime token; then to a literal default.
TOKEN="${TLA_GITLAB_ADMIN_TOKEN:-${TLA_GITLAB_TOKEN:-${GITLAB_ADMIN_TOKEN:-dev-token}}}"
PROJECT="${PROJECT:-tl-agent/demo}"
NAMESPACE="${NAMESPACE:-tl-agent}"

curl_api() {
    curl -fsS -H "PRIVATE-TOKEN: ${TOKEN}" "$@"
}

# Ensure TOKEN is a working GitLab PAT.  When .env still has the placeholder
# value (or the token has expired / been revoked), provision a fresh one via
# the Rails runner and persist it to .env so future runs don't need to repeat
# this step.
_ensure_gitlab_token() {
    if curl -fsS -H "PRIVATE-TOKEN: ${TOKEN}" "${GITLAB_URL}/api/v4/version" >/dev/null 2>&1; then
        return 0
    fi
    echo "==> GitLab token invalid — provisioning PAT via rails runner (first-run only)"
    NEW_TOKEN="$(docker exec tla-gitlab gitlab-rails runner \
        "u = User.find_by_username('root'); \
         u.personal_access_tokens.where(name: 'tl-agent-seed').each(&:revoke!); \
         t = u.personal_access_tokens.create!(name: 'tl-agent-seed', scopes: ['api'], expires_at: 1.year.from_now); \
         puts t.token" 2>/dev/null | grep -E '^glpat-' | tail -n1 | tr -d '[:space:]')"
    if [ -z "$NEW_TOKEN" ]; then
        echo "ERROR: could not provision GitLab token — is tla-gitlab healthy?" >&2
        exit 1
    fi
    TOKEN="$NEW_TOKEN"
    ENV_FILE="${REPO_ROOT_FOR_ENV}/.env"
    for var in TLA_GITLAB_ADMIN_TOKEN TLA_GITLAB_TOKEN; do
        if grep -qE "^${var}=" "$ENV_FILE"; then
            sed -i.bak -E "s|^${var}=.*|${var}=${TOKEN}|" "$ENV_FILE" && rm -f "${ENV_FILE}.bak"
        else
            printf '\n%s=%s\n' "$var" "$TOKEN" >> "$ENV_FILE"
        fi
    done
    echo "==> GitLab PAT provisioned and written to .env"
}
_ensure_gitlab_token

# 1. Ensure namespace exists
if ! curl_api "${GITLAB_URL}/api/v4/groups?search=${NAMESPACE}" | grep -q "\"path\":\"${NAMESPACE}\""; then
    echo "==> creating group ${NAMESPACE}"
    curl_api -X POST "${GITLAB_URL}/api/v4/groups" \
        -d "name=${NAMESPACE}&path=${NAMESPACE}&visibility=private"
fi

# 2. Ensure project exists
if ! curl_api "${GITLAB_URL}/api/v4/projects?search=demo" | grep -q "\"path_with_namespace\":\"${PROJECT}\""; then
    echo "==> creating project ${PROJECT}"
    GROUP_ID=$(curl_api "${GITLAB_URL}/api/v4/groups?search=${NAMESPACE}" | python3 -c "import json,sys;print(json.load(sys.stdin)[0]['id'])")
    curl_api -X POST "${GITLAB_URL}/api/v4/projects" \
        -d "name=demo&namespace_id=${GROUP_ID}&visibility=private"
fi

# 3. Commit timeline driven by commits.yaml.
# Invoke via `uv run` so httpx + pyyaml resolve from the project venv —
# bare `python3` on the host doesn't have either. Compute repo root from
# this script's location (script is at infra/gitlab/seed.sh).
if [ -f "$(dirname "$0")/commits.yaml" ]; then
    echo "==> applying commits.yaml (idempotent)"
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
    # Optional: anchor commits to a specific date so each demo run produces
    # fresh commits whose committed_date = now falls inside the agent's
    # Phase 1 fetch window for that run_date (yesterday 12:00 UTC -> that
    # date's 12:00 UTC) — run this seed before 12:00 UTC on COMMIT_ANCHOR_DATE.
    ANCHOR_ARGS=()
    if [ -n "${COMMIT_ANCHOR_DATE:-}" ]; then
        ANCHOR_ARGS+=(--anchor-date "${COMMIT_ANCHOR_DATE}")
    fi
    # `${arr[@]+...}` guards expansion when the array is empty under `set -u`
    # on macOS bash 3.2.
    (cd "$REPO_ROOT" && uv run python "${SCRIPT_DIR}/apply_commits.py" \
        --project "${PROJECT}" \
        --gitlab-url "${GITLAB_URL}" \
        --token "${TOKEN}" \
        ${ANCHOR_ARGS[@]+"${ANCHOR_ARGS[@]}"})
fi

echo "==> gitlab seed complete"
