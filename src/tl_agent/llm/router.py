"""Model router — maps (phase, tier) → (provider, model).

Single source of truth is `config/router.yaml`. The CLI loads it at boot;
the orchestrator pulls a provider for each phase via `Router.for_phase(...)`.

Why a config file: the central senior signal here is "two providers, one
abstraction" — but that abstraction is only real if you can swap one for the
other without code changes. The router proves it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from tl_agent.llm.anthropic_provider import AnthropicProvider
from tl_agent.llm.base import Provider
from tl_agent.llm.ollama_provider import OllamaProvider
from tl_agent.settings import get_settings

logger = logging.getLogger(__name__)

ProviderName = Literal["anthropic", "ollama"]
Tier = Literal["cheap", "balanced", "deep"]


class ModelRoute(BaseModel):
    """One (phase, tier) → (provider, model) binding."""

    model_config = ConfigDict(extra="forbid")

    provider: ProviderName
    model: str
    max_tokens: int = 1024
    temperature: float = 0.0
    cache_system: bool = False


class RouterConfig(BaseModel):
    """The parsed `config/router.yaml`."""

    model_config = ConfigDict(extra="forbid")

    default_tier: Tier = "balanced"
    routes: dict[str, ModelRoute] = Field(default_factory=dict[str, ModelRoute])

    @classmethod
    def load(cls, path: Path | str) -> RouterConfig:
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


# -------------------- providers + router --------------------


class Router:
    """Holds a provider per name + resolves routes to (provider, model, kwargs).

    Constructed once per process via `build_default()`; the orchestrator owns it.
    """

    def __init__(self, providers: dict[str, Provider], config: RouterConfig) -> None:
        self._providers = providers
        self._config = config
        self._config_path: Path | None = None

    def provider(self, name: str) -> Provider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise RuntimeError(
                f"provider '{name}' not registered; known: {list(self._providers)}"
            ) from exc

    def route(self, key: str) -> ModelRoute:
        """Look up a route key like 'phase2_triage' or 'verifier'.

        If the key isn't present, falls back to the `default_tier` route if any.
        """
        if key in self._config.routes:
            return self._config.routes[key]
        # convention: tier-only routes are named exactly "cheap" / "balanced" / "deep"
        if self._config.default_tier in self._config.routes:
            return self._config.routes[self._config.default_tier]
        raise RuntimeError(
            f"no route for '{key}' and no default-tier route configured; "
            f"available: {list(self._config.routes)}"
        )

    def for_phase(self, phase: str) -> tuple[Provider, ModelRoute]:
        route = self.route(phase)
        return self.provider(route.provider), route

    @property
    def config_path(self) -> Path | None:
        """Path the active config was loaded from (set by build_default)."""
        return self._config_path

    def set_config_path(self, path: Path) -> None:
        self._config_path = path

    @property
    def routes(self) -> dict[str, ModelRoute]:
        """All resolved (route-name → ModelRoute) entries, for introspection."""
        return dict(self._config.routes)


def build_default(*, config_path: Path | str | None = None) -> Router:
    """Construct providers from settings and load router config.

    Anthropic is always wired (no-op if api_key empty — the orchestrator will
    fail fast on first call); Ollama is wired but lazy (httpx client constructed
    only on use).
    """
    settings = get_settings()
    if config_path is not None:
        cfg_path = Path(config_path)
    elif settings.router_config:
        override = Path(settings.router_config)
        cfg_path = override if override.is_absolute() else settings.repo_root / override
    else:
        cfg_path = settings.config_dir / "router.yaml"
    config = RouterConfig.load(cfg_path)

    providers: dict[str, Provider] = {
        "anthropic": AnthropicProvider(api_key=settings.anthropic_api_key),
        "ollama": OllamaProvider(
            base_url=settings.ollama_base_url,
            timeout_seconds=settings.ollama_timeout_seconds,
        ),
    }
    router = Router(providers, config)
    router.set_config_path(cfg_path)
    return router
