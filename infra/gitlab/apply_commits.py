"""Apply commits.yaml against a running GitLab project.

For each entry, create the file on the requested branch and commit via the
Files API. We use synthetic content (commit message + line padding to reach
the desired insertions count) rather than tracked file contents — the agent
only reads the diff stats, not the actual file contents.

GitLab's Files API stamps the commit's `committed_date` with wall-clock UTC
at the time of the API call — there's no way to back-date it. So:

- The first run creates commits at the path `seed/<sha>.txt` on each
  feature branch; subsequent runs hit the file-exists check and skip.
- Passing `--anchor-date YYYY-MM-DD` namespaces the path under
  `seed/<anchor>/<sha>.txt` and the branch as `<branch>-<anchor>`. That
  forces fresh commits per anchor — useful for demo runs where the agent's
  Phase 1 commit fetch window is `[run_date - 24h, run_date]`. As long as
  the demo is run within that window (typically today/tomorrow), the new
  commits land in range. Old anchored commits stay around for replay.
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
    payload = {
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
