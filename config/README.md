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

## Local overrides (don't let `git pull` clobber your config)

The files committed here are **defaults** — they double as the demo roster and
the templates you start from. Two kinds live in this dir:

- **Contract config** you want updates to: `router*.yaml`, `prompts.yaml`,
  `escalation.md`. Leave these tracking the repo.
- **Instance config** that's per-deployment: `team.md`, `ownership.md`,
  `tl_preferences.md`, `gitlab_projects.yaml`, `chat_channels.yaml` — including
  your Jira `board_id` and `sprint_name_pattern`.

To customize the instance config without fighting `git pull`, put your copies
in a **local override dir**. Any file there shadows the committed default of the
same name; everything you *don't* override keeps tracking the repo. Resolution
lives in `Settings.resolve_config`.

```bash
# bootstrap the local dir with copies of the instance files, then edit them
uv run python -m tl_agent.cli init-local-config
$EDITOR ~/.config/tl-agent/team.md
```

The dir defaults to `$XDG_CONFIG_HOME/tl-agent` (i.e. `~/.config/tl-agent`).
Override with `TLA_LOCAL_CONFIG_DIR=/path/to/dir` in `.env`. It lives outside
the working tree, so it's never touched by git.
