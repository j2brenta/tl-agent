"""Apply commits.yaml against a running GitLab project.

For each entry, create the file on the requested branch and commit via the
Files API. We use synthetic content (commit message + line padding to reach
the desired insertions count) rather than tracked file contents — the agent
only reads the diff stats, not the actual file contents.

Idempotent: if a commit SHA prefix already exists in the project, skip.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True)
    p.add_argument("--gitlab-url", required=True)
    p.add_argument("--token", required=True)
    args = p.parse_args()

    yaml_path = Path(__file__).with_name("commits.yaml")
    spec = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not spec or "commits" not in spec:
        print("no commits in spec; exiting", file=sys.stderr)
        return

    headers = {"PRIVATE-TOKEN": args.token, "Content-Type": "application/json"}
    base = args.gitlab_url.rstrip("/")
    project_quoted = args.project.replace("/", "%2F")

    with httpx.Client(headers=headers, timeout=30) as client:
        for entry in spec["commits"]:
            _apply_one(client, base, project_quoted, entry)


def _apply_one(client: httpx.Client, base: str, project: str, entry: dict[str, Any]) -> None:
    sha = entry["sha"]
    branch = entry.get("branch", "main")
    path = f"seed/{sha}.txt"
    additions = int(entry.get("additions", 1))
    content = "\n".join(
        [f"// {entry.get('message', '')}", *(f"line {i}" for i in range(additions))]
    )

    # Ensure branch exists (fork off main).
    branch_resp = client.get(f"{base}/api/v4/projects/{project}/repository/branches/{branch}")
    if branch_resp.status_code == 404:
        client.post(
            f"{base}/api/v4/projects/{project}/repository/branches",
            params={"branch": branch, "ref": "main"},
        )

    # Try to commit. If the file already exists at this SHA path, skip.
    payload = {
        "branch": branch,
        "author_email": entry.get("author", "dev@example.local"),
        "author_name": entry.get("author", "dev").split("@")[0],
        "commit_message": entry.get("message", "seed"),
        "actions": [{"action": "create", "file_path": path, "content": content}],
    }
    resp = client.post(f"{base}/api/v4/projects/{project}/repository/commits", json=payload)
    if resp.status_code in {400, 409}:
        # Likely already exists — that's fine for idempotent re-runs.
        return
    resp.raise_for_status()


if __name__ == "__main__":
    main()
