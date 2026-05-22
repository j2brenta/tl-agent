"""Application settings.

Loaded fresh on every run; reads `.env` if present plus environment overrides.
LAYER 1 (markdown config) is loaded by `storage.markdown_loader`, not here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration for tl-agent."""

    model_config = SettingsConfigDict(
        env_file=".env",
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
    default_provider: Literal["anthropic", "ollama"] = "anthropic"

    # observability
    otlp_endpoint: str = Field(default="http://localhost:6006/v1/traces")
    enable_jsonl_sink: bool = True

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
