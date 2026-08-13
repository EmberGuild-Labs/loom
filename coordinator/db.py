"""
Loom coordinator database layer.

Kept separate from app.py so that scripts/gen_api_key.py can create and query
the database without importing Flask or requiring a valid config.yaml -- the
admin CLI has to work *before* the coordinator has ever been started.

SQLite concurrency notes:
  The coordinator is a single Flask process, but agents poll concurrently and
  each poll both reads and writes. WAL mode plus a busy timeout keeps those
  overlapping transactions from raising "database is locked".
"""

import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

DEFAULT_DB_PATH = os.path.join(BASE_DIR, "loom.db")

# Columns added after the initial release. CREATE TABLE IF NOT EXISTS will not
# add columns to a table that already exists, so upgrades need explicit ALTERs.
MIGRATIONS = [
    ("devices", "agent_version", "TEXT"),
    ("devices", "running_jobs", "INTEGER DEFAULT 0"),
    ("jobs", "source", "TEXT DEFAULT 'dashboard'"),
    ("jobs", "assigned_at", "TEXT"),
    ("jobs", "heartbeat_at", "TEXT"),
    ("jobs", "exit_code", "INTEGER"),
]


def db_path():
    return os.environ.get("LOOM_DB", DEFAULT_DB_PATH)


def connect(path=None):
    """Open a connection with the pragmas Loom relies on.

    isolation_level=None puts the connection in autocommit mode so that
    explicit BEGIN IMMEDIATE blocks (used to claim jobs atomically) actually
    control the transaction rather than fighting sqlite3's implicit one.
    """
    conn = sqlite3.connect(path or db_path(), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def _existing_columns(conn, table):
    try:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def init_db(path=None):
    """Create the schema if missing and apply any additive migrations.

    Safe to call on every boot -- every statement is idempotent.
    """
    target = path or db_path()
    parent = os.path.dirname(os.path.abspath(target))
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(SCHEMA_PATH, "r") as f:
        schema = f.read()

    conn = connect(target)
    try:
        conn.executescript(schema)
        for table, column, decl in MIGRATIONS:
            if column not in _existing_columns(conn, table):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    finally:
        conn.close()
    return target
