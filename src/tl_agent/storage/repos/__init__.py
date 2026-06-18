"""Repository functions — one module per table, exported here as namespaces."""

from tl_agent.storage.repos import (
    baselines,
    collection_state,
    commits,
    decisions,
    flags,
    gitlab_projects,
    observations,
    predictions,
    resolved_config,
    snapshots,
    standup_segments,
)

__all__ = [
    "baselines",
    "collection_state",
    "commits",
    "decisions",
    "flags",
    "gitlab_projects",
    "observations",
    "predictions",
    "resolved_config",
    "snapshots",
    "standup_segments",
]
