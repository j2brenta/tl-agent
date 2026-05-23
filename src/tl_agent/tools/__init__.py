"""Tools layer — registry, base contract, idempotency, readback.

Concrete tool implementations live in `tools.jira`, `tools.gitlab`,
`tools.chat.*`, `tools.memory`. They all register themselves into the
module-level `registry` at import time.
"""

from tl_agent.tools.base import (
    BaseTool,
    IdempotencyLookup,
    RetryPolicy,
    ToolError,
    ToolErrorKind,
    ToolException,
    ToolResult,
    normalize_args,
    stable_hash,
)
from tl_agent.tools.idempotency import SqliteIdempotencyStore, make_key
from tl_agent.tools.readback import ReadbackResult, readback
from tl_agent.tools.registry import RegistryError, registry

__all__ = [
    "BaseTool",
    "IdempotencyLookup",
    "ReadbackResult",
    "RegistryError",
    "RetryPolicy",
    "SqliteIdempotencyStore",
    "ToolError",
    "ToolErrorKind",
    "ToolException",
    "ToolResult",
    "make_key",
    "normalize_args",
    "readback",
    "registry",
    "stable_hash",
]
