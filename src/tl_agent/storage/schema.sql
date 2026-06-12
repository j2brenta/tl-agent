-- tl-agent — SQLite schema (LAYER 2: the "state")
--
-- Conventions:
--   - ISO dates as TEXT (YYYY-MM-DD).
--   - Timestamps as TEXT (RFC3339, UTC).
--   - JSON blobs in `payload` columns where structure is variable.
--   - All tables explicit; no implicit rowid storage for primary data.
--   - FTS5 virtual table for standup_observations.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- ---------- schema metadata ----------
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------- runs ----------
-- One row per orchestrator invocation. Lets us scope counters per run and
-- correlate to traces.
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    run_date    TEXT NOT NULL,         -- YYYY-MM-DD
    started_at  TEXT NOT NULL,         -- RFC3339
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'in_progress',
    trace_id    TEXT,
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS runs_by_date ON runs(run_date);

-- ---------- daily_flags ----------
-- What the agent is watching, carried day-over-day.
CREATE TABLE IF NOT EXISTS daily_flags (
    id             TEXT PRIMARY KEY,
    run_date       TEXT NOT NULL,
    engineer_id    TEXT,                 -- NULL for team-wide
    type           TEXT NOT NULL,
    title          TEXT NOT NULL,
    status         TEXT NOT NULL,        -- TriageStatus
    days_hot       INTEGER NOT NULL CHECK (days_hot >= 1),
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    resolved       INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    resolution_note TEXT,
    related_ticket_ids TEXT NOT NULL DEFAULT '[]'  -- JSON list of strings
);
CREATE INDEX IF NOT EXISTS daily_flags_by_date    ON daily_flags(run_date);
CREATE INDEX IF NOT EXISTS daily_flags_by_eng     ON daily_flags(engineer_id);
CREATE INDEX IF NOT EXISTS daily_flags_unresolved ON daily_flags(resolved, run_date);

-- ---------- predictions ----------
-- Falsifiable claims the agent made; Phase 0 closes them out.
CREATE TABLE IF NOT EXISTS predictions (
    id                 TEXT PRIMARY KEY,
    made_on            TEXT NOT NULL,
    claim              TEXT NOT NULL,
    related_hotspot_id TEXT,
    resolve_after      TEXT NOT NULL,
    outcome            TEXT NOT NULL DEFAULT 'open',
    resolved_on        TEXT,
    resolution_note    TEXT
);
CREATE INDEX IF NOT EXISTS predictions_open ON predictions(outcome, resolve_after);

-- ---------- standup_observations ----------
-- Raw standup text per engineer per day, plus the agent's one-line summary.
CREATE TABLE IF NOT EXISTS standup_observations (
    id             TEXT PRIMARY KEY,
    run_date       TEXT NOT NULL,
    engineer_id    TEXT NOT NULL,
    raw            TEXT NOT NULL,
    summary        TEXT,
    chat_message_id TEXT,
    UNIQUE (run_date, engineer_id)
);
CREATE INDEX IF NOT EXISTS standup_obs_by_date ON standup_observations(run_date);
CREATE INDEX IF NOT EXISTS standup_obs_by_eng  ON standup_observations(engineer_id, run_date);

-- FTS5 mirror for free-text search across raw + summary.
-- Synced via triggers below so the agent never has to write twice.
CREATE VIRTUAL TABLE IF NOT EXISTS standup_observations_fts USING fts5(
    raw,
    summary,
    engineer_id UNINDEXED,
    run_date    UNINDEXED,
    content='standup_observations',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS standup_obs_ai AFTER INSERT ON standup_observations
BEGIN
    INSERT INTO standup_observations_fts(rowid, raw, summary, engineer_id, run_date)
    VALUES (new.rowid, new.raw, new.summary, new.engineer_id, new.run_date);
END;

CREATE TRIGGER IF NOT EXISTS standup_obs_ad AFTER DELETE ON standup_observations
BEGIN
    INSERT INTO standup_observations_fts(standup_observations_fts, rowid, raw, summary, engineer_id, run_date)
    VALUES ('delete', old.rowid, old.raw, old.summary, old.engineer_id, old.run_date);
END;

CREATE TRIGGER IF NOT EXISTS standup_obs_au AFTER UPDATE ON standup_observations
BEGIN
    INSERT INTO standup_observations_fts(standup_observations_fts, rowid, raw, summary, engineer_id, run_date)
    VALUES ('delete', old.rowid, old.raw, old.summary, old.engineer_id, old.run_date);
    INSERT INTO standup_observations_fts(rowid, raw, summary, engineer_id, run_date)
    VALUES (new.rowid, new.raw, new.summary, new.engineer_id, new.run_date);
END;

-- ---------- standup_segments ----------
-- Per-message segments classified as project `update` vs `off_topic`
-- (banter, links, life updates — a team-mood signal). UNIQUE on
-- (chat_message_id, engineer_id, segment_index) is the cache key: once a
-- message has been parsed by the LLM, neither the "Collect Standup" button
-- nor a pipeline run re-parses it.
CREATE TABLE IF NOT EXISTS standup_segments (
    id              TEXT PRIMARY KEY,
    chat_message_id TEXT NOT NULL,
    chat_channel_id TEXT,
    engineer_id     TEXT NOT NULL,
    date_iso        TEXT NOT NULL,
    segment_index   INTEGER NOT NULL,
    text            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    UNIQUE (chat_message_id, engineer_id, segment_index)
);
CREATE INDEX IF NOT EXISTS standup_segments_by_date ON standup_segments(date_iso);
CREATE INDEX IF NOT EXISTS standup_segments_by_eng  ON standup_segments(engineer_id, date_iso);

-- ---------- engineer_baselines ----------
-- Rolling stats per engineer per metric per window. Phase 2 compares today
-- against the baseline.
CREATE TABLE IF NOT EXISTS engineer_baselines (
    engineer_id TEXT NOT NULL,
    window      TEXT NOT NULL,        -- e.g. "7d", "30d"
    metric      TEXT NOT NULL,        -- e.g. "standup_line_count_avg"
    value       REAL NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (engineer_id, window, metric)
);

-- ---------- ticket_snapshots ----------
-- Daily snapshot of every sprint ticket. Lets us compute deltas in Phase 0.
CREATE TABLE IF NOT EXISTS ticket_snapshots (
    run_date    TEXT NOT NULL,
    ticket_id   TEXT NOT NULL,
    status      TEXT NOT NULL,
    assignee    TEXT,
    points      REAL,
    payload     TEXT NOT NULL,        -- JSON: full JiraTicket
    PRIMARY KEY (run_date, ticket_id)
);
CREATE INDEX IF NOT EXISTS ticket_snapshots_by_ticket ON ticket_snapshots(ticket_id, run_date);

-- ---------- decisions ----------
-- Phase 8 audit log: every approval/rejection/edit.
CREATE TABLE IF NOT EXISTS decisions (
    id              TEXT PRIMARY KEY,
    -- Wall-clock UTC time the row was written. For audit.
    created_at      TEXT NOT NULL,
    -- The run this decision belongs to (ctx.run_date.isoformat(),
    -- YYYY-MM-DD). Distinct from created_at because `tl-agent run --date
    -- X` may execute at any wall-clock time, and the brief filter must
    -- bucket by intended run date, not by UTC midnight rollover.
    run_date        TEXT NOT NULL,
    hotspot_id      TEXT NOT NULL,
    proposed_mode   TEXT NOT NULL,
    proposed_body   TEXT NOT NULL,
    tl_action       TEXT,
    tl_acted_at     TEXT,
    final_body      TEXT,
    final_target    TEXT,
    trace_id        TEXT,
    sent_message_id TEXT,
    sent_provider   TEXT,
    needs_review    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS decisions_by_time   ON decisions(created_at);
CREATE INDEX IF NOT EXISTS decisions_by_run    ON decisions(run_date, created_at);
CREATE INDEX IF NOT EXISTS decisions_pending   ON decisions(tl_action, created_at);

-- ---------- idempotency_keys ----------
-- Writer-tool dedup. Hash of (tool_name, normalized_args, run_date) maps to
-- the cached result. TTL enforced at read time by `idempotency.py`.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT PRIMARY KEY,
    tool_name   TEXT NOT NULL,
    run_date    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    result_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idempotency_by_age ON idempotency_keys(created_at);

-- ---------- resolved_config ----------
-- Values the agent *resolved* at runtime rather than the human authoring them
-- in config/ (LAYER 1). Today: a Jira board discovered when team.md omits
-- `board_id`. config/team.md remains the override; this is the learned cache
-- so we don't re-discover (or re-ask) every run. Cleared on DB reset.
CREATE TABLE IF NOT EXISTS resolved_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL          -- RFC3339, when it was last resolved
);

-- ---------- schema_meta seed ----------
INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '1');
