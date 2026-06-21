"""Application settings.

Loaded fresh on every run. The single source of truth for tokens / URLs is
`<repo-root>/.env` (see `.env.example`); environment variables of the same
name override the file. Every variable is prefixed `TLA_` to keep the global
namespace tidy.

LAYER 1 (markdown config) is loaded by `storage.markdown_loader`, not here.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"


def _default_local_config_dir() -> Path:
    """Per-deployment config override dir, honouring `XDG_CONFIG_HOME`."""
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "tl-agent"


class Settings(BaseSettings):
    """Runtime configuration for tl-agent."""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        env_prefix="TLA_",
        extra="ignore",
    )

    # paths
    repo_root: Path = REPO_ROOT
    config_dir: Path = REPO_ROOT / "config"
    # Per-deployment overrides that must survive a `git pull`. Any config file
    # dropped here shadows the committed default of the same name (see
    # `resolve_config`). Lives outside the working tree, gitignored.
    local_config_dir: Path = Field(default_factory=_default_local_config_dir)
    prompts_dir: Path = REPO_ROOT / "prompts"
    traces_dir: Path = REPO_ROOT / "traces"
    sqlite_path: Path = REPO_ROOT / "data" / "tl_agent.db"
    # WAL is ideal for local dev, but its shared-memory (`-shm`) file is
    # memory-mapped, which Docker Desktop's bind-mount file sharing doesn't
    # support — a DB on a bind mount then reports "disk image is malformed".
    # The container overrides this to TRUNCATE (see infra/agent.defaults.env).
    sqlite_journal_mode: Literal["WAL", "TRUNCATE", "DELETE"] = "WAL"

    # llm
    anthropic_api_key: str = Field(default="", description="Anthropic API key")
    ollama_base_url: str = Field(default="http://localhost:11434/v1")
    ollama_model: str = Field(
        default="qwen3:8b",
        description="Default model tag used by the Ollama provider when a route omits one",
    )
    ollama_timeout_seconds: float = Field(
        default=300.0,
        description="Per-request HTTP timeout for Ollama calls (slow on cold loads + 8B models)",
    )
    default_provider: Literal["anthropic", "ollama"] = "anthropic"
    router_config: str = Field(
        default="",
        description=(
            "Override path for the router YAML (relative to repo root or absolute). "
            "Empty → config/router.yaml. Set to config/router.ollama.yaml to run "
            "the whole pipeline on a local model."
        ),
    )

    # observability
    otlp_endpoint: str = Field(default="http://localhost:6006/v1/traces")
    otlp_timeout_seconds: int = Field(
        default=10,
        description="Per-batch OTLP export timeout; 2s is too tight when the host is busy",
    )
    enable_jsonl_sink: bool = True
    # When true, openinference-instrumentation-anthropic emits LLM I/O spans
    # (request messages, system prompt, response text, tool_uses) so Phoenix
    # can render the Conversation tab. Off by default because payloads can
    # contain ticket bodies / names; opt in for debugging.
    log_llm_payloads: bool = False

    # chat provider
    chat_provider: Literal["mattermost", "slack"] = "mattermost"
    mattermost_url: str = Field(default="http://localhost:8065")
    mattermost_token: str = Field(default="")
    mattermost_team: str = Field(default="engineering")
    mattermost_webhook_secret: str = Field(default="dev-shared-secret-change-me")

    slack_bot_token: str = Field(default="")
    slack_signing_secret: str = Field(default="")

    # jira / gitlab
    jira_base_url: str = Field(default="http://localhost:9100")
    jira_token: str = Field(default="dev-token")
    # "3" = Jira Cloud (ADF comment bodies); "2" = Server/Data Center (plain
    # text). Only the core REST prefix + comment serialization differ; the
    # Agile API (/rest/agile/1.0) is version-independent.
    jira_api_version: Literal["2", "3"] = "3"
    # Story points have no fixed field in Jira — each instance exposes them via
    # a custom field whose id varies. Override per instance.
    jira_points_field: str = "customfield_10016"
    # Optional custom field holding the issue's sprint (also instance-specific).
    jira_sprint_field: str | None = None
    # Maps a Jira status *name* (lower-cased) to one of our coarse buckets.
    # Falls back to status.statusCategory when a name isn't listed.
    jira_status_map: dict[str, str] = Field(
        default_factory=lambda: {
            "to do": "todo",
            "open": "todo",
            "backlog": "todo",
            "in progress": "in_progress",
            "in review": "in_review",
            "code review": "in_review",
            "review": "in_review",
            "blocked": "blocked",
            "done": "done",
            "closed": "done",
            "resolved": "done",
        }
    )
    gitlab_base_url: str = Field(default="http://localhost:8929")
    gitlab_token: str = Field(default="dev-token")
    # Seed-time admin token (Personal Access Token with `api` scope). Used
    # by infra/gitlab/seed.sh. If unset, falls back to gitlab_token.
    gitlab_admin_token: str = Field(default="")

    # Seed-time Mattermost admin (used by services/mattermost_seed/seed.py
    # on first boot to create the admin user + bot accounts).
    mattermost_admin_user: str = Field(default="tl-admin")
    mattermost_admin_email: str = Field(default="tl-admin@example.local")
    mattermost_admin_pw: str = Field(default="Tl-AdminPw1!")

    # phase 5 ReACT budgets
    react_max_steps: int = 12
    react_wall_clock_seconds: int = 60
    react_token_budget: int = 30_000

    # run-level budgets
    run_token_budget: int = 500_000

    def resolve_config(self, name: str) -> Path:
        """Resolve a config file by name, preferring the local override dir.

        A file in `local_config_dir` shadows the committed default of the same
        name in `config_dir`, so a `git pull` that updates the repo never
        clobbers per-deployment config (team roster, allowlists, board id).
        Opt-in per file: only what you drop into the local dir is overridden;
        everything else keeps tracking the committed (contract) version.
        """
        override = self.local_config_dir / name
        return override if override.exists() else self.config_dir / name


def get_settings() -> Settings:
    """Return a fresh Settings instance.

    Not cached on purpose — each run starts clean so tests can override env.
    """
    return Settings()
