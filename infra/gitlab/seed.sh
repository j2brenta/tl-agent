#!/usr/bin/env bash
# Seed the in-compose GitLab with one project + a deterministic commit timeline.
#
# Idempotent: re-runs are safe (skips if the project already exists).
# Uses the root API token printed at first boot — fetch via:
#   docker exec tla-gitlab grep 'Password:' /etc/gitlab/initial_root_password

set -euo pipefail

GITLAB_URL="${GITLAB_URL:-http://localhost:8929}"
TOKEN="${GITLAB_ADMIN_TOKEN:-dev-token}"
PROJECT="${PROJECT:-tl-agent/demo}"
NAMESPACE="${NAMESPACE:-tl-agent}"

curl_api() {
    curl -fsS -H "PRIVATE-TOKEN: ${TOKEN}" "$@"
}

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
    (cd "$REPO_ROOT" && uv run python "${SCRIPT_DIR}/apply_commits.py" \
        --project "${PROJECT}" \
        --gitlab-url "${GITLAB_URL}" \
        --token "${TOKEN}")
fi

echo "==> gitlab seed complete"
