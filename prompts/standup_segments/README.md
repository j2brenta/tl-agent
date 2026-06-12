# Standup segments — prompt evolution

Per-message segmentation + update/off-topic classification, used to (a)
show standup text grouped per person without losing structure when someone
writes several blocks, and (b) feed a future team-mood signal from the
`off_topic` segments. Cheap tier (Haiku) — one short call per standup
message, cached by `(chat_message_id, engineer_id, segment_index)` so most
messages are only ever classified once.

## Versions

- `v1.md` (2026-06-12) — initial. Splits on topic change, classifies each
  segment as `update` or `off_topic`, preserves original wording verbatim.

## Open questions

- `off_topic` segments are currently only surfaced as a badge in the UI.
  A later mood-evaluation phase will read `standup_segments` directly
  (via the repo, not the LLM again) to score team mood over time.
