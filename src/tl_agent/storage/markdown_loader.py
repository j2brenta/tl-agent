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

import yaml

from tl_agent.models import Engineer
from tl_agent.settings import get_settings

# A `## H2` introduces one engineer; bullet lines of the form `- **key:** value`
# below it become the per-engineer attributes.
_ENGINEER_HEADER = re.compile(r"^##\s+(?P<name>.+?)\s*$", re.MULTILINE)
# Accepts both `- **key:** value` and `- **key**: value` forms.
_BULLET = re.compile(r"^-\s+\*\*(?P<key>[^*]+?)\*\*:?\s*(?P<value>.+?)\s*$")
_LIST_KEYS = {"aliases"}


@dataclass(frozen=True)
class TeamConfig:
    """Parsed `config/team.md`."""

    engineers: tuple[Engineer, ...]

    def by_id(self, engineer_id: str) -> Engineer | None:
        for e in self.engineers:
            if e.id == engineer_id:
                return e
        return None


def _parse_engineer_block(name: str, block: str) -> Engineer:
    """Pull out `- **key:** value` bullets following an `## H2`."""
    attrs: dict[str, str | tuple[str, ...]] = {"display_name": name}
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
    # display_name from the H2 may be overridden by an explicit bullet
    return Engineer.model_validate(attrs)


def load_team(config_dir: Path | None = None) -> TeamConfig:
    """Parse `config/team.md` into a `TeamConfig`."""
    root = config_dir or get_settings().config_dir
    path = root / "team.md"
    text = path.read_text(encoding="utf-8")

    headers = list(_ENGINEER_HEADER.finditer(text))
    engineers: list[Engineer] = []
    for i, hdr in enumerate(headers):
        start = hdr.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[start:end]
        engineers.append(_parse_engineer_block(hdr["name"].strip(), block))
    return TeamConfig(engineers=tuple(engineers))


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
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    values = data.get(key) or []
    if not isinstance(values, list):
        raise ValueError(f"{path}: key {key!r} must be a list, got {type(values).__name__}")
    return frozenset(str(v) for v in values)


def load_allowed_chat_channels(config_dir: Path | None = None) -> frozenset[str]:
    root = config_dir or get_settings().config_dir
    return _load_allowlist(root / "chat_channels.yaml", "allowed_channels")


def load_allowed_gitlab_projects(config_dir: Path | None = None) -> frozenset[str]:
    root = config_dir or get_settings().config_dir
    return _load_allowlist(root / "gitlab_projects.yaml", "allowed_projects")


def clear_allowlist_cache() -> None:
    """Reset the YAML allowlist cache. Test-only."""
    _load_allowlist.cache_clear()
