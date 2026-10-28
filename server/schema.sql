-- Hosted reprolog: Cloudflare D1 database "reprolog" (id f8ae861e-0daf-4963-8948-6291942a521c, APAC).
-- Applied 2026-10-28. Logs are kept 10 days; full payloads live gzip-compressed in capture_data.

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT,
    provider TEXT,                -- github, google, email
    created_at INTEGER NOT NULL
);

-- Created automatically the first time a token sends REPROLOG_PROJECT=<name>.
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (user_id, name)
);

-- Only the sha256 hash is stored. project_id NULL = token works for every project of the account.
CREATE TABLE IF NOT EXISTS tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('read', 'write')),
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    prefix TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER,
    revoked_at INTEGER
);

-- One small searchable row per log; the dashboard list reads only this.
CREATE TABLE IF NOT EXISTS captures (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    method TEXT,
    path TEXT,
    status INTEGER,
    duration_ms REAL,
    error_type TEXT,
    error_message TEXT,
    replay_of TEXT,
    span_count INTEGER,
    log_count INTEGER,
    size_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS idx_captures_project_time ON captures(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_captures_time ON captures(created_at);   -- for the 10-day cleanup

-- Full capture JSON, gzip-compressed (D1 row limit is 2 MB; bodies are capped before this).
CREATE TABLE IF NOT EXISTS capture_data (
    capture_id TEXT PRIMARY KEY REFERENCES captures(id) ON DELETE CASCADE,
    data BLOB NOT NULL
);

-- Per-user daily counter so one account can't use up the shared 100k writes/day.
CREATE TABLE IF NOT EXISTS usage_daily (
    user_id TEXT NOT NULL,
    day TEXT NOT NULL,            -- YYYY-MM-DD (UTC)
    captures INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
