-- Loom coordinator schema.
-- Applied by coordinator/db.py on startup and by scripts/gen_api_key.py, so the
-- admin CLI can register devices without the Flask app ever having been run.
-- Every statement must be idempotent (IF NOT EXISTS) -- this file is re-applied
-- on every boot.

CREATE TABLE IF NOT EXISTS devices (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT UNIQUE NOT NULL,
    api_key        TEXT UNIQUE NOT NULL,
    platform       TEXT,
    tags           TEXT    DEFAULT '',
    persistent     INTEGER DEFAULT 0,
    created_at     TEXT NOT NULL,
    last_seen      TEXT,
    hostname       TEXT,
    agent_version  TEXT,
    cpu_percent    REAL,
    ram_percent    REAL,
    ram_used_gb    REAL,
    ram_total_gb   REAL,
    disk_percent   REAL,
    uptime_seconds INTEGER,
    running_jobs   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    command            TEXT NOT NULL,
    target_tag         TEXT,
    target_device_id   INTEGER,
    -- pending -> assigned -> running -> done|failed|cancelled|lost
    status             TEXT NOT NULL DEFAULT 'pending',
    assigned_device_id INTEGER,
    source             TEXT DEFAULT 'dashboard',  -- dashboard | trigger:<device>
    created_at         TEXT NOT NULL,
    assigned_at        TEXT,
    started_at         TEXT,
    finished_at        TEXT,
    heartbeat_at       TEXT,   -- last time the running agent checked in for this job
    exit_code          INTEGER,
    result             TEXT,
    log                TEXT,
    FOREIGN KEY (assigned_device_id) REFERENCES devices(id) ON DELETE SET NULL,
    FOREIGN KEY (target_device_id)   REFERENCES devices(id) ON DELETE CASCADE
);

-- poll_jobs() scans pending work on every agent poll; without this it is a full
-- table scan over all history once the jobs table grows.
CREATE INDEX IF NOT EXISTS idx_jobs_pending  ON jobs(status, id);
CREATE INDEX IF NOT EXISTS idx_jobs_assigned ON jobs(assigned_device_id, status);
CREATE INDEX IF NOT EXISTS idx_devices_key   ON devices(api_key);
