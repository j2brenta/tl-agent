"""LAYER 1 loader.

Parses the markdown files under `config/` into typed Python objects. Loaded
fresh on every run — these files are the "constants" the agent operates by,
hand-edited or via explicit "remember this" actions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import yaml

from tl_agent.models import Engineer, Role
from tl_agent.settings import get_settings

# A `## H2` introduces one engineer; bullet lines of the form `- **key:** value`
# below it become the per-engineer attributes.
_ENGINEER_HEADER = re.compile(r"^##\s+(?P<name>.+?)\s*$", re.MULTILINE)
# Accepts both `- **key:** value` and `- **key**: value` forms.
_BULLET = re.compile(r"^-\s+\*\*(?P<key>[^*]+?)\*\*:?\s*(?P<value>.+?)\s*$")
_LIST_KEYS = {"aliases"}
# H2 sections that are config, not people — parsed into TeamConfig fields and
# kept out of `members` so they never hit `Engineer.model_validate`.
_RESERVED_SECTIONS = {"sprint scope"}


@dataclass(frozen=True)
class TeamConfig:
    """Parsed `config/team.md` — the full roster (engineers + leadership).

    `members` holds everyone; `engineers` filters to the individual
    contributors the workflow operates over, so leadership entries never leak
    into the per-engineer triage/standup loops.
    """

    members: tuple[Engineer, ...]
    board_id: str | None = None
    sprint_name_pattern: str | None = None

    @property
    def engineers(self) -> tuple[Engineer, ...]:
        return tuple(m for m in self.members if m.role_kind == Role.ENGINEER)

    @property
    def team_lead(self) -> Engineer | None:
        return next((m for m in self.members if m.role_kind == Role.TEAM_LEAD), None)

    @property
    def product_manager(self) -> Engineer | None:
        return next((m for m in self.members if m.role_kind == Role.PRODUCT_MANAGER), None)

    def by_id(self, member_id: str) -> Engineer | None:
        return next((m for m in self.members if m.id == member_id), None)

    def resolve(self, handle: str | None) -> str | None:
        """Map a cross-system handle to a member `id`, or None if unknown.

        `handle` is whatever an upstream system reports — a Jira `displayName`
        or `accountId`, a GitLab username, a chat id. Resolution goes through
        `Engineer.matches()`, so it honours `jira_account_id`, `display_name`,
        `aliases`, and the bare `id`. Used to fold a ticket's raw assignee back
        onto the team roster (see `phases.phase1_collect`).
        """
        if not handle:
            return None
        return next((m.id for m in self.members if m.matches(handle)), None)


def _parse_bullets(block: str) -> dict[str, str | tuple[str, ...]]:
    """Pull `- **key:** value` bullets from an `## H2` body into a dict."""
    attrs: dict[str, str | tuple[str, ...]] = {}
    for line in block.splitlines():
        match = _BULLET.match(line.strip())
        if not match:
            continue
        key = match["key"].strip().rstrip(":").lower().replace(" ", "_")
        value = match["value"].strip()
        if key in _LIST_KEYS:
            attrs[key] = tuple(p.strip() for p in value.split(",") if p.strip())
        else:
            attrs[key] = value
    return attrs


def _parse_engineer_block(name: str, block: str) -> Engineer:
    """Pull out `- **key:** value` bullets following an `## H2`."""
    attrs: dict[str, str | tuple[str, ...]] = {"display_name": name}
    # display_name from the H2 may be overridden by an explicit bullet
    attrs.update(_parse_bullets(block))
    return Engineer.model_validate(attrs)


def load_team(config_dir: Path | None = None) -> TeamConfig:
    """Parse `config/team.md` into a `TeamConfig`."""
    root = config_dir or get_settings().config_dir
    path = root / "team.md"
    text = path.read_text(encoding="utf-8")

    headers = list(_ENGINEER_HEADER.finditer(text))
    members: list[Engineer] = []
    board_id: str | None = None
    sprint_name_pattern: str | None = None
    for i, hdr in enumerate(headers):
        name = hdr["name"].strip()
        start = hdr.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[start:end]
        if name.lower() in _RESERVED_SECTIONS:
            attrs = _parse_bullets(block)
            board_id = _as_str(attrs.get("board_id")) or board_id
            sprint_name_pattern = _as_str(attrs.get("sprint_name_pattern")) or sprint_name_pattern
            continue
        members.append(_parse_engineer_block(name, block))
    return TeamConfig(
        members=tuple(members),
        board_id=board_id,
        sprint_name_pattern=sprint_name_pattern,
    )


def _as_str(value: str | tuple[str, ...] | None) -> str | None:
    return value if isinstance(value, str) else None


def load_markdown(name: str, config_dir: Path | None = None) -> str:
    """Verbatim read of one config file — for prompts that want it inline.

    Use this for `ownership.md`, `escalation.md`, `tl_preferences.md` where
    we hand the whole file to the LLM as context. The LLM is good at reading
    markdown; we don't need to re-parse it.
    """
    root = config_dir or get_settings().config_dir
    return (root / name).read_text(encoding="utf-8")


# -------------------- allowlists (chat channels, gitlab projects) --------------------
#
# These are tool-layer guards against the LLM inventing identifiers. The
# loaders are cached because they are hit on every tool invocation; tests
# reset the cache via `_load_allowlist.cache_clear()`.


@lru_cache(maxsize=8)
def _load_allowlist(path: Path, key: str) -> frozenset[str]:
    data = cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    values: Any = data.get(key) or []
    if not isinstance(values, list):
        raise ValueError(f"{path}: key {key!r} must be a list, got {type(values).__name__}")
    return frozenset(str(v) for v in cast(list[Any], values))  # type: ignore[redundant-cast]


def load_allowed_chat_channels(config_dir: Path | None = None) -> frozenset[str]:
    root = config_dir or get_settings().config_dir
    return _load_allowlist(root / "chat_channels.yaml", "allowed_channels")


def load_allowed_gitlab_projects(config_dir: Path | None = None) -> frozenset[str]:
    root = config_dir or get_settings().config_dir
    return _load_allowlist(root / "gitlab_projects.yaml", "allowed_projects")


def clear_allowlist_cache() -> None:
    """Reset the YAML allowlist cache. Test-only."""
    _load_allowlist.cache_clear()
