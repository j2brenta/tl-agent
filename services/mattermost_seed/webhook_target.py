"""Outgoing-webhook receiver for the TL_review channel.

Mattermost POSTs events here when a configured trigger fires. We verify the
signature, dedupe via the event id, and (in a more developed setup) would
route the event back into the agent for follow-up processing.

For the demo, this exists to prove:
  - We verify the HMAC signature (rejects tampered or unsigned posts).
  - We dedupe on event id (Mattermost retries — idempotent receiver).

Run standalone:  uvicorn services.mattermost_seed.webhook_target:app --port 9101
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import UTC, datetime

from fastapi import FastAPI, Header, HTTPException, Request

from tl_agent.tools.chat.mattermost import MattermostProvider

logger = logging.getLogger(__name__)
app = FastAPI(title="mattermost-webhook-target")

_provider = MattermostProvider()
_seen: OrderedDict[str, datetime] = OrderedDict()
_MAX_SEEN = 1024


@app.post("/webhook/mattermost")
async def receive(
    request: Request,
    x_mm_signature: str | None = Header(default=None),
    x_mm_timestamp: str | None = Header(default=None),
    x_mm_event_id: str | None = Header(default=None),
) -> dict[str, str]:
    body = await request.body()
    if not x_mm_signature or not _provider.verify_webhook_signature(
        body=body, signature=x_mm_signature, timestamp=x_mm_timestamp
    ):
        raise HTTPException(status_code=401, detail="bad signature")

    event_id = x_mm_event_id or ""
    if event_id and event_id in _seen:
        return {"status": "duplicate", "event_id": event_id}
    if event_id:
        _seen[event_id] = datetime.now(UTC)
        if len(_seen) > _MAX_SEEN:
            _seen.popitem(last=False)

    payload = await request.json()
    logger.info("webhook.received", extra={"event_id": event_id, "type": payload.get("event")})
    return {"status": "ok", "event_id": event_id}
