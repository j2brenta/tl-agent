"""Application settings.

Loaded fresh on every run. The single source of truth for tokens / URLs is
`<repo-root>/.env` (see `.env.example`); environment variables of the same
name override the file. Every variable is prefixed `TLA_` to keep the global
namespace tidy.

LAYER 1 (markdown config) is loaded by `storage.markdown_loader`, not here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"


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
    prompts_dir: Path = REPO_ROOT / "prompts"
    traces_dir: Path = REPO_ROOT / "traces"
    sqlite_path: Path = REPO_ROOT / "data" / "tl_agent.db"

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


def get_settings() -> Settings:
    """Return a fresh Settings instance.

    Not cached on purpose — each run starts clean so tests can override env.
    """
    return Settings()
