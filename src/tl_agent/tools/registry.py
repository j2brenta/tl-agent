"""Central tool registry.

Responsibilities:
- Validate every registered tool at import time (Pydantic schemas valid,
  required class vars set, name unique).
- Generate the Anthropic-format tool schemas the model router hands to the LLM.
- Provide phase-scoped bindings: Phase 5 (ReACT) can ONLY see read-only
  tools. Writers (`requires_approval=True`) are excluded at bind time, not
  just at call time — defense in depth.

This is the "registry not 40 copy-pasted tool files" senior signal.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from tl_agent.tools.base import BaseTool

logger = logging.getLogger(__name__)


class RegistryError(RuntimeError):
    """A tool definition failed registry validation."""


class _Registry:
    """Singleton store of registered tools, keyed by name."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool[Any, Any]] = {}

    def register(self, tool: BaseTool[Any, Any]) -> None:
        """Validate and register a tool. Idempotent on identical re-registration."""
        _assert_well_formed(tool)
        existing = self._tools.get(tool.name)
        if existing is not None and existing is not tool:
            existing_cls = type(existing).__name__
            raise RegistryError(
                f"tool name collision: '{tool.name}' already registered by {existing_cls}"
            )
        self._tools[tool.name] = tool
        logger.debug(
            "tool.registered",
            extra={"tool": tool.name, "requires_approval": tool.requires_approval},
        )

    def get(self, name: str) -> BaseTool[Any, Any]:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise RegistryError(f"no such tool: '{name}'") from exc

    def all(self) -> list[BaseTool[Any, Any]]:
        return list(self._tools.values())

    def read_only(self) -> list[BaseTool[Any, Any]]:
        """Tools safe to bind to Phase 5 (no side effects, no approval needed)."""
        return [t for t in self._tools.values() if not t.requires_approval]

    def writers(self) -> list[BaseTool[Any, Any]]:
        return [t for t in self._tools.values() if t.requires_approval]

    def for_phase5(self, allowed: Iterable[str] | None = None) -> list[BaseTool[Any, Any]]:
        """Phase 5 ReACT binding.

        Returns ONLY tools where `requires_approval=False`. If `allowed` is
        given, further restricts to that subset (used per-hotspot to narrow
        the toolset based on the hot-spot type). Writers are excluded even
        if listed — the registry will not let them through.
        """
        candidates = self.read_only()
        if allowed is not None:
            allowed_set = set(allowed)
            candidates = [t for t in candidates if t.name in allowed_set]
            # Surface mistakes loudly — if a caller asked for a writer here,
            # they probably misunderstood the contract.
            asked_writers = [w.name for w in self.writers() if w.name in allowed_set]
            if asked_writers:
                raise RegistryError(
                    "phase 5 binding refused: writer tools cannot be exposed to ReACT — "
                    f"{asked_writers}"
                )
        return candidates

    def anthropic_schemas(
        self, tools: Iterable[BaseTool[Any, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """Generate the Anthropic Messages API `tools` parameter from Pydantic.

        Each entry: {"name": str, "description": str, "input_schema": json_schema}.
        """
        candidates = list(tools) if tools is not None else self.all()
        out: list[dict[str, Any]] = []
        for t in candidates:
            schema = t.input_model.model_json_schema()
            # Anthropic expects `input_schema`; Pydantic emits `$defs` etc. — pass through.
            out.append(
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": schema,
                }
            )
        return out

    def reset(self) -> None:
        """For tests."""
        self._tools.clear()


def _assert_well_formed(tool: BaseTool[Any, Any]) -> None:
    cls = type(tool)
    missing = [
        attr
        for attr in ("name", "description", "input_model", "output_model")
        if not getattr(cls, attr, None)
    ]
    if missing:
        raise RegistryError(f"{cls.__name__} missing class vars: {missing}")
    if not tool.name.replace("_", "").isalnum():
        raise RegistryError(
            f"{cls.__name__}.name must be snake_case alphanumeric, got {tool.name!r}"
        )


# module-level singleton
registry = _Registry()
