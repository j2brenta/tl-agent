"""Phase 8 — TL approval + deterministic execution + readback.

This phase does NOT run inline with the orchestrator. It is invoked by the
web UI's approve endpoint per decision. The orchestrator finishes after
Phase 7; Phase 8 is the async "wait for TL → act on each approved decision".

execute_decision(decision):
  1. dispatch to the right writer tool based on response_mode
  2. tool's BaseTool.invoke handles validation + retry + idempotency
  3. readback verifies the write landed
  4. persist the updated Decision (with sent_message_id + tl_action)
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from tl_agent.models import ApprovalAction, Decision, ResponseMode
from tl_agent.obs.spans import phase_span
from tl_agent.storage import transaction
from tl_agent.storage.repos import decisions as decisions_repo
from tl_agent.tools import (
    SqliteIdempotencyStore,
    ToolError,
    ToolErrorKind,
    ToolResult,
    readback,
)
from tl_agent.tools.chat.factory import get_chat_provider
from tl_agent.tools.chat.tools import PostDMTool, PostStandupQuestionTool

logger = logging.getLogger(__name__)


@phase_span("phase8_execute")
async def execute_decision(
    *,
    conn: sqlite3.Connection,
    idempotency: SqliteIdempotencyStore,
    decision_id: str,
    action: ApprovalAction,
    edited_body: str | None = None,
    edited_target: str | None = None,
    run_date_iso: str,
) -> Decision:
    """Apply the TL's action to one decision. Returns the updated row."""
    decision = decisions_repo.get(conn, decision_id)
    if decision is None:
        raise LookupError(f"no such decision: {decision_id}")

    final_body = edited_body if edited_body is not None else decision.proposed_body
    final_target = edited_target

    # NOTE / ESCALATE / REJECT: persist, don't send.
    if action is ApprovalAction.REJECTED or decision.proposed_mode is ResponseMode.NOTE:
        updated = decision.model_copy(
            update={
                "tl_action": action,
                "tl_acted_at": datetime.now(UTC),
                "final_body": final_body,
                "final_target": final_target,
            }
        )
        with transaction(conn):
            decisions_repo.insert(conn, updated)
        return updated

    if action is not ApprovalAction.APPROVED and action is not ApprovalAction.EDITED:
        updated = decision.model_copy(
            update={"tl_action": action, "tl_acted_at": datetime.now(UTC)}
        )
        with transaction(conn):
            decisions_repo.insert(conn, updated)
        return updated

    # Approved + (DM or STANDUP) — send for real.
    provider = get_chat_provider()
    if decision.proposed_mode is ResponseMode.STANDUP:
        target = final_target or _default_standup_channel()
        send_result = await PostStandupQuestionTool().invoke(
            {"channel_id": target, "body": final_body},
            run_date_iso=run_date_iso,
            idempotency_lookup=idempotency,
        )
    else:
        # DM or ESCALATE — both deliver via DM to the configured target
        if decision.proposed_mode is ResponseMode.DM:
            target = final_target or _default_dm_target(decision)
        else:  # ESCALATE
            target = final_target or _default_escalation_target()
        send_result = await PostDMTool().invoke(
            {"user_id": target, "body": final_body},
            run_date_iso=run_date_iso,
            idempotency_lookup=idempotency,
        )

    if isinstance(send_result, ToolError):
        logger.warning(
            "phase8.send_failed decision=%s kind=%s msg=%s",
            decision_id,
            send_result.kind.value,
            send_result.message,
        )
        # Persist the attempt so the UI can show the failure; do NOT mark sent_message_id.
        updated = decision.model_copy(
            update={
                "tl_action": action,
                "tl_acted_at": datetime.now(UTC),
                "final_body": final_body,
                "final_target": target,
            }
        )
        with transaction(conn):
            decisions_repo.insert(conn, updated)
        return updated

    assert isinstance(send_result, ToolResult)
    post = send_result.value

    # Readback: fetch the message we just posted, assert body matches.
    try:
        await readback(
            fetch=lambda: provider.get_message(
                channel_id=post.channel_id, message_id=post.message_id
            ),
            matches=lambda msg: msg.text == final_body,
            label=f"phase8.{decision.proposed_mode.value}",
        )
    except Exception as exc:
        logger.warning(
            "phase8.readback_failed decision=%s err_type=%s err=%s",
            decision_id,
            type(exc).__name__,
            exc,
        )
        # Note: idempotency cache was already written by the writer — a manual
        # retry against the same key will hit the cache, which is the documented
        # behaviour. To force resend, change run_date_iso or the body.
        updated = decision.model_copy(
            update={
                "tl_action": action,
                "tl_acted_at": datetime.now(UTC),
                "final_body": final_body,
                "final_target": target,
                "sent_message_id": post.message_id,
                "sent_provider": post.provider,
            }
        )
        with transaction(conn):
            decisions_repo.insert(conn, updated)
        return updated

    updated = decision.model_copy(
        update={
            "tl_action": action,
            "tl_acted_at": datetime.now(UTC),
            "final_body": final_body,
            "final_target": target,
            "sent_message_id": post.message_id,
            "sent_provider": post.provider,
        }
    )
    with transaction(conn):
        decisions_repo.insert(conn, updated)
    logger.info("phase8.sent", extra={"decision": decision_id, "msg": post.message_id})
    return updated


def _default_dm_target(decision: Decision) -> str:
    # Hotspot's first engineer_id ⇒ DM target. Phase 6's `target` field is
    # the authoritative source; this is the safety net.
    return decision.hotspot_id.split("-")[1] if "-" in decision.hotspot_id else "unknown"


def _default_standup_channel() -> str:
    from tl_agent.settings import get_settings

    return get_settings().mattermost_team + "-standup"


def _default_escalation_target() -> str:
    # `config/escalation.md` documents `eng-manager` as the default manager handle.
    return "eng-manager"


# Re-export a sentinel for tests that want to assert the failure-mode taxonomy.
READBACK_FAILURE = ToolErrorKind.UNKNOWN
