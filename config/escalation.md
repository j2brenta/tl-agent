# Escalation rules

When the agent recommends a response mode (Phase 6), these rules override the
default heuristic. The default heuristic is in `phases/phase6_response_mode.py`;
this file is the human-readable override layer.

## Defaults (from plan)

- `days_hot < 2` → NOTE
- `days_hot 2-4` individual → DM
- `days_hot 2-4` team-wide → STANDUP
- `days_hot >= 5` OR sprint-critical → ESCALATE
- off-sprint work without justification → DM

## Overrides

### Per-engineer

- **karen**: bump DM → STANDUP for `days_hot >= 3`. Reason: still ramping;
  team-visible coaching is healthier than private DMs while she's new in role.
- **john**: never ESCALATE without first sending a DM. Reason: senior; he
  wants the chance to respond before it reaches management.

### Per-module

- `services/billing/`: ESCALATE threshold drops to `days_hot >= 3` —
  revenue-critical; surface earlier.

### Per-time

- During sprint week 1 (`sprint_day <= 5`): suppress ESCALATE unless
  sprint-critical. Reason: early-sprint variance is noise.

## Escalation target

Manager handle: `eng-manager` (chat user id). Phase 6 sets
`ResponseDraft.target = "eng-manager"` for ESCALATE mode.
