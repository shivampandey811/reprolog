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

-- One-time sign-in codes sent by email. Only a hash of the code is stored.
CREATE TABLE IF NOT EXISTS login_codes (
    email TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_login_codes_email ON login_codes(email, created_at);

-- Paid plans. No row = free. Set by hand for now (no payment provider yet):
--   INSERT OR REPLACE INTO subscriptions (user_id, plan, updated_at) VALUES ('<user id>', 'pro', unixepoch());
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    plan TEXT NOT NULL CHECK (plan IN ('free', 'pro')),
    updated_at INTEGER NOT NULL
);

-- Uptime checks (Pro). Checked every minute by the cron trigger.
CREATE TABLE IF NOT EXISTS monitors (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending, up, down
    fail_streak INTEGER NOT NULL DEFAULT 0,   -- down after 2 failed checks in a row
    last_checked_at INTEGER,
    last_code INTEGER,
    last_ms INTEGER,
    last_error TEXT,
    changed_at INTEGER                        -- when status last flipped
);
CREATE INDEX IF NOT EXISTS idx_monitors_user ON monitors(user_id);

-- One row per monitor per hour (one upsert per check keeps D1 writes low).
CREATE TABLE IF NOT EXISTS monitor_hours (
    monitor_id TEXT NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    hour INTEGER NOT NULL,                    -- unix time / 3600
    checks INTEGER NOT NULL DEFAULT 0,
    fails INTEGER NOT NULL DEFAULT 0,
    total_ms INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (monitor_id, hour)
);

CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    monitor_id TEXT NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    cause TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_monitor ON incidents(monitor_id, started_at DESC);

-- Request metrics sent by the library once a minute (every request, not just failures).
-- One row per process per route per minute; queries sum them. buckets = JSON latency histogram.
CREATE TABLE IF NOT EXISTS metric_minutes (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    minute INTEGER NOT NULL,                  -- unix time / 60
    method TEXT NOT NULL,
    route TEXT NOT NULL,
    count INTEGER NOT NULL,
    errors INTEGER NOT NULL,
    total_ms REAL NOT NULL,
    buckets TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metric_minutes ON metric_minutes(project_id, minute);

-- Distinct errors per project, so alerts fire once per new kind of error, not per failure.
CREATE TABLE IF NOT EXISTS error_groups (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    error_type TEXT,
    message TEXT,
    method TEXT,
    path TEXT,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    sample_id TEXT,
    PRIMARY KEY (project_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_error_groups_seen ON error_groups(project_id, last_seen DESC);

-- Where alerts go (Pro). Email goes to the account's address.
CREATE TABLE IF NOT EXISTS alert_settings (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    email_on_error INTEGER NOT NULL DEFAULT 1,
    email_on_uptime INTEGER NOT NULL DEFAULT 1,
    slack_webhook TEXT,
    updated_at INTEGER NOT NULL
);
