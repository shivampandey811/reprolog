"""Local SQLite store for captured requests."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Optional

from .config import CONFIG

_lock = threading.Lock()
_conns: dict = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    method TEXT,
    path TEXT,
    status INTEGER,
    duration_ms REAL,
    error_type TEXT,
    error_message TEXT,
    replay_of TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_captures_created ON captures(created_at DESC);
"""


def db_path() -> str:
    return os.path.abspath(CONFIG.db_path)


def _connect() -> sqlite3.Connection:
    path = db_path()
    conn = _conns.get(path)
    if conn is not None:
        return conn
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    # Keep captured data out of git: the folder ignores itself.
    gitignore = os.path.join(folder, ".gitignore")
    if not os.path.exists(gitignore):
        with open(gitignore, "w") as fh:
            fh.write("# Created by reprolog: captured requests may contain private data.\n*\n")
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(captures)")}
    if "replay_of" not in cols:
        conn.execute("ALTER TABLE captures ADD COLUMN replay_of TEXT")
    conn.execute("PRAGMA journal_mode=WAL")
    _conns[path] = conn
    return conn


def save(record: dict) -> None:
    error = record.get("error") or {}
    req = record.get("request") or {}
    resp = record.get("response") or {}
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT OR REPLACE INTO captures (id, created_at, method, path, status, duration_ms, error_type, error_message, replay_of, data)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["id"],
                record.get("created_at", time.time()),
                req.get("method"),
                req.get("path"),
                resp.get("status"),
                record.get("duration_ms"),
                error.get("type"),
                (error.get("message") or "")[:500],
                record.get("replay_of"),
                json.dumps(record, ensure_ascii=False, default=str),
            ),
        )
        global _saves
        _saves += 1
        if _saves % 100 == 0:
            conn.execute(
                "DELETE FROM captures WHERE id NOT IN (SELECT id FROM captures ORDER BY created_at DESC LIMIT ?)",
                (CONFIG.max_captures,),
            )
        conn.commit()


_saves = 0

LIST_COLUMNS = "id, created_at, method, path, status, duration_ms, error_type, error_message, replay_of"


def list_since(after_seq: int, limit: int = 200) -> list:
    """Captures saved after `after_seq` (insert order), for the live feed."""
    with _lock:
        rows = _connect().execute(
            f"SELECT rowid AS seq, {LIST_COLUMNS} FROM captures WHERE rowid > ? ORDER BY rowid ASC LIMIT ?",
            (after_seq, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def list_captures(limit: int = 50, offset: int = 0) -> list:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            f"SELECT rowid AS seq, {LIST_COLUMNS} FROM captures ORDER BY rowid DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def get(capture_id: str) -> Optional[dict]:
    """Look up by full id or a unique prefix."""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT data FROM captures WHERE id LIKE ? ORDER BY created_at DESC LIMIT 2", (capture_id + "%",)
        ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise LookupError(f"'{capture_id}' matches more than one capture; use more characters.")
    return json.loads(rows[0]["data"])


def clear() -> int:
    with _lock:
        conn = _connect()
        count = conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
        conn.execute("DELETE FROM captures")
        conn.commit()
    return count


def count() -> int:
    with _lock:
        return _connect().execute("SELECT COUNT(*) FROM captures").fetchone()[0]
