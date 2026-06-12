"""Repository functions — one module per table, exported here as namespaces."""

from tl_agent.storage.repos import (
    baselines,
    decisions,
    flags,
    observations,
    predictions,
    resolved_config,
    snapshots,
    standup_segments,
)

__all__ = [
    "baselines",
    "decisions",
    "flags",
    "observations",
    "predictions",
    "resolved_config",
    "snapshots",
    "standup_segments",
]
