-- Prism dashboard SQLite schema.

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project_id TEXT,
    cwd TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_sec INTEGER,
    observations_count INTEGER DEFAULT 0,
    mcp_calls_count INTEGER DEFAULT 0,
    engrams_reinforced_count INTEGER DEFAULT 0,
    has_search INTEGER DEFAULT 0,  -- 1 if any prism_search call
    transcript_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);

CREATE TABLE IF NOT EXISTS mcp_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    timestamp TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    params_json TEXT,
    result_count INTEGER,
    duration_ms INTEGER,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_mcp_calls_session ON mcp_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_mcp_calls_tool ON mcp_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_mcp_calls_timestamp ON mcp_calls(timestamp);

CREATE TABLE IF NOT EXISTS engram_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    engram_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,  -- push | search_hit | relevant_hit | get_hit | record_create | observation_match
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_engram_events_session ON engram_events(session_id);
CREATE INDEX IF NOT EXISTS idx_engram_events_engram ON engram_events(engram_id);
CREATE INDEX IF NOT EXISTS idx_engram_events_type ON engram_events(event_type);
CREATE INDEX IF NOT EXISTS idx_engram_events_timestamp ON engram_events(timestamp);

CREATE TABLE IF NOT EXISTS engrams (
    engram_id TEXT PRIMARY KEY,
    scope TEXT,
    kind TEXT,
    domain TEXT,
    confidence REAL,
    evidence_count INTEGER,
    last_observed TEXT,
    pinned INTEGER DEFAULT 0,
    project_id TEXT,
    path TEXT,
    trigger TEXT,
    snapshot_at TEXT  -- when this row was last refreshed from index.json
);

CREATE INDEX IF NOT EXISTS idx_engrams_confidence ON engrams(confidence DESC);
CREATE INDEX IF NOT EXISTS idx_engrams_scope ON engrams(scope);
CREATE INDEX IF NOT EXISTS idx_engrams_kind ON engrams(kind);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    timestamp TEXT NOT NULL,
    event_type TEXT,
    tool_name TEXT,
    project_id TEXT,
    summary TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_observations_session ON observations(session_id);
CREATE INDEX IF NOT EXISTS idx_observations_timestamp ON observations(timestamp);

CREATE TABLE IF NOT EXISTS collector_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
