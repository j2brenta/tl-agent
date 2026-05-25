"""Apply commits.yaml against a running GitLab project.

For each entry, create the file on the requested branch and commit via the
Commits API. We use synthetic content (commit message + line padding to reach
the desired insertions count) rather than tracked file contents — the agent
only reads the diff stats, not the actual file contents.

- The first run creates commits at the path `seed/<sha>.txt` on each
  feature branch; subsequent runs hit the 400/409 check and skip.
- Passing `--anchor-date YYYY-MM-DD` namespaces the path under
  `seed/<anchor>/<sha>.txt` and the branch as `<branch>-<anchor>`, forcing
  fresh commits per anchor. GitLab's Commits API always stamps committed_date
  with wall-clock time (author_date/commit_date fields are ignored), so Phase 1
  uses a midnight-to-midnight window over run_date to capture seed commits
  regardless of when the seed runs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 4
_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True)
    p.add_argument("--gitlab-url", required=True)
    p.add_argument("--token", required=True)
    p.add_argument(
        "--anchor-date",
        default="",
        help="If set (YYYY-MM-DD), namespace file paths + branches so this "
        "run creates fresh commits even when prior seeds exist.",
    )
    args = p.parse_args()

    yaml_path = Path(__file__).with_name("commits.yaml")
    spec = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not spec or "commits" not in spec:
        print("no commits in spec; exiting", file=sys.stderr)
        return

    headers = {"PRIVATE-TOKEN": args.token, "Content-Type": "application/json"}
    base = args.gitlab_url.rstrip("/")
    project_quoted = args.project.replace("/", "%2F")

    if args.anchor_date:
        print(f"==> anchoring commits at {args.anchor_date} (fresh per-run)", file=sys.stderr)

    with httpx.Client(headers=headers, timeout=30) as client:
        for entry in spec["commits"]:
            _apply_one(client, base, project_quoted, entry, anchor=args.anchor_date)


def _apply_one(
    client: httpx.Client,
    base: str,
    project: str,
    entry: dict[str, Any],
    *,
    anchor: str = "",
) -> None:
    sha = entry["sha"]
    branch = entry.get("branch", "main")
    if anchor:
        # Namespacing both branch and file path keeps prior seeds intact
        # while forcing fresh commits whose committed_date = now.
        branch = f"{branch}-{anchor}"
        path = f"seed/{anchor}/{sha}.txt"
    else:
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

    # Try to commit. If the file already exists at this path, skip.
    payload: dict[str, Any] = {
        "branch": branch,
        "author_email": entry.get("author", "dev@example.local"),
        "author_name": entry.get("author", "dev").split("@")[0],
        "commit_message": entry.get("message", "seed"),
        "actions": [{"action": "create", "file_path": path, "content": content}],
    }
    resp = client.post(f"{base}/api/v4/projects/{project}/repository/commits", json=payload)
    if resp.status_code in {400, 409}:
        return  # idempotent: already exists
    resp.raise_for_status()


if __name__ == "__main__":
    main()
