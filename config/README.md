# LAYER 1 — flat markdown config

The files in this directory are loaded fresh into context on every run.
They describe **facts about the team and how the TL wants the agent to behave**
— things that change manually, not from the agent's own state changes.

Edit these files manually, or via an explicit "remember this" action from the
TL. The agent never overwrites them on its own.

## Files

- `team.md` — roster: John, Matt, Alicia, Karen. One section per engineer with
  Jira/GitLab/chat handles, role, on-call rotation, baselines.
- `ownership.md` — module → engineer mapping. Used by Phase 3 to detect when a
  hot spot crosses an ownership boundary.
- `escalation.md` — rules for when ESCALATE is appropriate vs DM. Defaults
  from plan, but the TL can override per-engineer or per-module.
- `tl_preferences.md` — voice/tone/do's-and-don'ts for drafted DMs and standup
  questions. Phase 7 (compose) reads this verbatim.

## Why not in SQLite?

These are slow-moving, human-readable, version-controllable, and want to be
diffable in a PR. SQLite is for things the agent produces and reads back
(flags, predictions, observations) — see `src/tl_agent/storage/schema.sql`.
