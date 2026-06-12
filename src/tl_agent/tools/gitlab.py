"""GitLab tools.

Hits the GitLab REST v4 API. In compose this points at the real
`gitlab/gitlab-ce` container; in tests we'll seed it via the official API.

Tools registered here are READ-ONLY (we don't need to write to GitLab for
the morning loop). If you add a writer later (e.g., `post_mr_comment`),
remember to set `requires_approval=True` and provide `idempotency_key`.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, field_validator

from tl_agent.models import GitCommit
from tl_agent.settings import get_settings
from tl_agent.tools._http import http_client, raise_from_http_error, raise_from_transport_error
from tl_agent.tools.base import BaseTool
from tl_agent.tools.registry import registry


def _encode_path(value: str) -> str:
    # GitLab REST requires namespaced project/group paths to be URL-encoded in
    # the path segment: `tl-agent/demo` must become `tl-agent%2Fdemo`,
    # otherwise GitLab parses `tl-agent` as the id and 404s on sub-resources.
    return quote(value, safe="")


def _validate_project(value: str) -> str:
    # Local import to avoid a circular dependency through settings/storage.
    from tl_agent.storage.markdown_loader import load_allowed_gitlab_projects, load_team

    groups = load_team().gitlab_groups
    if groups:
        if any(value == g or value.startswith(f"{g}/") for g in groups):
            return value
        raise ValueError(
            f"unknown project {value!r}. It is outside the team's GitLab groups "
            f"{list(groups)!r} (config/team.md → Repo scope). Use a project "
            "discovered via list_group_projects, not a guess."
        )

    allowed = load_allowed_gitlab_projects()
    if value not in allowed:
        raise ValueError(
            f"unknown project {value!r}. Allowed projects: {sorted(allowed)}. "
            "Use a project from ownership.md, not a guess."
        )
    return value


# Tickets like ENG-12, PAY-3 — used by `_parse_ticket_keys` over commit msgs.
_TICKET_RE = re.compile(r"\b([A-Z]{2,8}-\d{1,6})\b")


def _client() -> httpx.AsyncClient:
    s = get_settings()
    return http_client(
        base_url=s.gitlab_base_url,
        headers={"PRIVATE-TOKEN": s.gitlab_token, "Accept": "application/json"},
    )


def _parse_ticket_keys(text: str) -> tuple[str, ...]:
    return tuple(sorted(set(_TICKET_RE.findall(text))))


async def _paginate_pages(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any],
    tool_label: str,
    *,
    per_page: int = 100,
) -> list[dict[str, Any]]:
    """Walk a `page`/`per_page` GitLab list endpoint until a short page ends it."""
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        try:
            r = await client.get(path, params={**params, "per_page": per_page, "page": page})
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise_from_http_error(exc, tool_label=tool_label)
        except httpx.HTTPError as exc:
            raise_from_transport_error(exc, tool_label=tool_label)
        batch: list[dict[str, Any]] = r.json()
        items.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return items


# -------------------- list_commits --------------------


class ListCommitsIn(BaseModel):
    project: str = Field(
        min_length=1,
        description="GitLab project path (e.g. 'tl-agent/demo'). The tool URL-encodes it.",
    )
    since: datetime
    until: datetime
    author: str | None = None

    @field_validator("project")
    @classmethod
    def _check_project(cls, v: str) -> str:
        return _validate_project(v)


class ListCommitsOut(BaseModel):
    commits: list[GitCommit] = Field(default_factory=list[GitCommit])


class ListCommitsTool(BaseTool[ListCommitsIn, ListCommitsOut]):
    name: ClassVar[str] = "list_commits"
    description: ClassVar[str] = (
        "List commits to a GitLab project in a time window, optionally "
        "filtered by author. Linked ticket keys are parsed from each commit "
        "message."
    )
    input_model: ClassVar[type[BaseModel]] = ListCommitsIn
    output_model: ClassVar[type[BaseModel]] = ListCommitsOut

    async def _call(self, args: ListCommitsIn) -> ListCommitsOut:
        params = {
            "since": args.since.isoformat(),
            "until": args.until.isoformat(),
            "with_stats": "true",
            "all": "true",
        }
        if args.author:
            params["author"] = args.author
        path = f"/api/v4/projects/{_encode_path(args.project)}/repository/commits"
        async with _client() as client:
            try:
                r = await client.get(path, params=params)
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=self.name)
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=self.name)
        data = r.json()
        commits = [_to_commit(item, project=args.project) for item in data]
        return ListCommitsOut(commits=commits)


def _to_commit(item: dict[str, Any], *, project: str) -> GitCommit:
    stats: dict[str, Any] = item.get("stats") or {}
    message = str(item.get("message", ""))
    return GitCommit(
        sha=str(item["id"])[:40],
        project=project,
        author=str(item.get("author_email") or item.get("author_name") or "unknown"),
        committed_at=datetime.fromisoformat(str(item["committed_date"]).replace("Z", "+00:00")),
        branch=str(item.get("branch") or "") or None,
        message=message,
        files_changed=int(stats.get("files", 0) or 0),
        insertions=int(stats.get("additions", 0) or 0),
        deletions=int(stats.get("deletions", 0) or 0),
        linked_ticket_keys=_parse_ticket_keys(message),
    )


# -------------------- get_commit_diff --------------------


class GetDiffIn(BaseModel):
    project: str = Field(min_length=1)
    sha: str = Field(min_length=7)

    @field_validator("project")
    @classmethod
    def _check_project(cls, v: str) -> str:
        return _validate_project(v)


class FileDiff(BaseModel):
    path: str
    additions: int
    deletions: int
    patch: str = ""


class GetDiffOut(BaseModel):
    sha: str
    files: list[FileDiff] = Field(default_factory=list[FileDiff])
    total_additions: int = 0
    total_deletions: int = 0


class GetCommitDiffTool(BaseTool[GetDiffIn, GetDiffOut]):
    name: ClassVar[str] = "get_commit_diff"
    description: ClassVar[str] = (
        "Return per-file diff stats and patches for one commit. Use this to "
        "judge whether the diff scope matches the ticket estimate."
    )
    input_model: ClassVar[type[BaseModel]] = GetDiffIn
    output_model: ClassVar[type[BaseModel]] = GetDiffOut

    async def _call(self, args: GetDiffIn) -> GetDiffOut:
        path = f"/api/v4/projects/{_encode_path(args.project)}/repository/commits/{args.sha}/diff"
        async with _client() as client:
            try:
                r = await client.get(path)
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=self.name)
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=self.name)
        data = r.json()
        files = [
            FileDiff(
                path=str(item.get("new_path") or item.get("old_path") or ""),
                additions=int(item.get("additions", 0) or 0),
                deletions=int(item.get("deletions", 0) or 0),
                patch=str(item.get("diff", ""))[:4000],  # cap the patch size
            )
            for item in data
        ]
        return GetDiffOut(
            sha=args.sha,
            files=files,
            total_additions=sum(f.additions for f in files),
            total_deletions=sum(f.deletions for f in files),
        )


# -------------------- list_branches --------------------


class ListBranchesIn(BaseModel):
    project: str = Field(min_length=1)

    @field_validator("project")
    @classmethod
    def _check_project(cls, v: str) -> str:
        return _validate_project(v)


class Branch(BaseModel):
    name: str
    last_commit_sha: str
    last_commit_at: datetime
    author: str


class ListBranchesOut(BaseModel):
    branches: list[Branch] = Field(default_factory=list[Branch])


class ListBranchesTool(BaseTool[ListBranchesIn, ListBranchesOut]):
    name: ClassVar[str] = "list_branches"
    description: ClassVar[str] = "List branches in a project with their last commit info."
    input_model: ClassVar[type[BaseModel]] = ListBranchesIn
    output_model: ClassVar[type[BaseModel]] = ListBranchesOut

    async def _call(self, args: ListBranchesIn) -> ListBranchesOut:
        path = f"/api/v4/projects/{_encode_path(args.project)}/repository/branches"
        async with _client() as client:
            try:
                r = await client.get(path)
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=self.name)
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=self.name)
        data = r.json()
        return ListBranchesOut(
            branches=[
                Branch(
                    name=str(b["name"]),
                    last_commit_sha=str(b["commit"]["id"])[:40],
                    last_commit_at=datetime.fromisoformat(
                        str(b["commit"]["committed_date"]).replace("Z", "+00:00")
                    ),
                    author=str(
                        b["commit"].get("author_email") or b["commit"].get("author_name") or ""
                    ),
                )
                for b in data
            ]
        )


# -------------------- list_group_projects --------------------


class ListGroupProjectsIn(BaseModel):
    group: str = Field(
        min_length=1,
        description="GitLab group path or id (e.g. 'tl-agent'). Subgroups are included.",
    )


class ListGroupProjectsOut(BaseModel):
    group: str
    projects: list[str] = Field(default_factory=list[str])


class ListGroupProjectsTool(BaseTool[ListGroupProjectsIn, ListGroupProjectsOut]):
    name: ClassVar[str] = "list_group_projects"
    description: ClassVar[str] = (
        "List every project (path_with_namespace) under a GitLab group, "
        "including subgroups. Use this to discover the team's repos instead "
        "of guessing project paths."
    )
    input_model: ClassVar[type[BaseModel]] = ListGroupProjectsIn
    output_model: ClassVar[type[BaseModel]] = ListGroupProjectsOut

    async def _call(self, args: ListGroupProjectsIn) -> ListGroupProjectsOut:
        path = f"/api/v4/groups/{_encode_path(args.group)}/projects"
        params = {"include_subgroups": "true", "simple": "true"}
        async with _client() as client:
            data = await _paginate_pages(client, path, params, self.name)
        projects = [str(p["path_with_namespace"]) for p in data]
        return ListGroupProjectsOut(group=args.group, projects=projects)


# -------------------- register --------------------


def register_gitlab_tools() -> None:
    """Add the GitLab tools to the registry. Idempotent."""
    import contextlib

    from tl_agent.tools.registry import RegistryError

    for tool_cls in (ListCommitsTool, GetCommitDiffTool, ListBranchesTool, ListGroupProjectsTool):
        with contextlib.suppress(RegistryError):
            registry.register(tool_cls())
