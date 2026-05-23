"""Prompt loader — reads versioned prompt files with frontmatter.

Layout:
  prompts/<phase>/v1.md
  prompts/<phase>/v2.md   (later)
  prompts/<phase>/README.md (evolution log; not loaded by code)

Each prompt has YAML frontmatter (model, temperature, created, author,
parent_version, change_reason) followed by the system-prompt body.

Loader picks `vN.md` per phase via config/prompts.yaml — defaults to v1.
A/B over prompts is just "edit prompts.yaml + rerun evals".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import frontmatter
import yaml
from pydantic import BaseModel

from tl_agent.settings import get_settings


@dataclass(frozen=True)
class Prompt:
    """One loaded prompt — frontmatter metadata + body."""

    phase: str
    version: int
    body: str
    model: str
    temperature: float
    created: str
    author: str
    parent_version: int | None
    change_reason: str


class PromptsConfig(BaseModel):
    """The parsed `config/prompts.yaml`. Maps phase → active version."""

    versions: dict[str, int]


def load_prompts_config(path: Path | None = None) -> PromptsConfig:
    target = path or get_settings().config_dir / "prompts.yaml"
    if not target.exists():
        # Default: v1 for every known phase
        return PromptsConfig(
            versions={
                "phase2_triage": 1,
                "phase3_correlate": 1,
                "phase5_deepdive": 1,
                "phase6_response_mode": 1,
                "phase7_compose": 1,
                "verifier": 1,
            }
        )
    return PromptsConfig.model_validate(yaml.safe_load(target.read_text(encoding="utf-8")))


def load_prompt(
    phase: str, *, version: int | None = None, prompts_dir: Path | None = None
) -> Prompt:
    """Load `prompts/<phase>/v<N>.md` and parse frontmatter."""
    base = prompts_dir or get_settings().prompts_dir
    v = version if version is not None else load_prompts_config().versions.get(phase, 1)
    path = base / phase / f"v{v}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt not found: {path}")
    post = frontmatter.load(path)
    meta = post.metadata
    parent_raw = meta.get("parent_version")
    return Prompt(
        phase=phase,
        version=v,
        body=post.content.strip(),
        model=str(meta.get("model") or "claude-sonnet-4-6"),
        temperature=float(str(meta.get("temperature") or 0.0)),
        created=str(meta.get("created") or ""),
        author=str(meta.get("author") or ""),
        parent_version=int(str(parent_raw)) if parent_raw is not None else None,
        change_reason=str(meta.get("change_reason") or ""),
    )
