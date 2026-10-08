"""SQLite persistence: settings, sync runs, per-activity results, synced IDs."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import DB_PATH, ensure_data_dir

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger       TEXT NOT NULL,              -- manual | scheduled
    status        TEXT NOT NULL,              -- running | success | partial | failed
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    window_start  TEXT,
    found         INTEGER NOT NULL DEFAULT 0, -- activities returned by Garmin
    uploaded      INTEGER NOT NULL DEFAULT 0,
    already       INTEGER NOT NULL DEFAULT 0, -- previously synced, ignored
    skipped       INTEGER NOT NULL DEFAULT 0,
    failed        INTEGER NOT NULL DEFAULT 0,
    message       TEXT
);

CREATE TABLE IF NOT EXISTS sync_items (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    garmin_activity_id TEXT NOT NULL,
    name               TEXT,
    activity_type      TEXT,
    start_time         TEXT,
    distance_m         REAL,
    duration_s         REAL,
    status             TEXT NOT NULL,         -- uploaded | pending | skipped | failed
    detail             TEXT,
    coros_import_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_run ON sync_items(run_id);

CREATE TABLE IF NOT EXISTS synced_activities (
    garmin_activity_id TEXT PRIMARY KEY,
    status             TEXT NOT NULL,         -- uploaded | pending
    run_id             INTEGER,
    coros_import_id    TEXT,
    synced_at          TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db() -> None:
    ensure_data_dir()
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Any run left "running" was interrupted by a restart.
        conn.execute(
            "UPDATE sync_runs SET status='failed', finished_at=?, message='Interrupted (app restarted)' "
            "WHERE status='running'",
            (now_iso(),),
        )
    os.chmod(DB_PATH, 0o600)


# ----------------------------------------------------------------- settings
def get_setting(key: str, default: Any = None) -> Any:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(key: str, value: Any) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def delete_setting(key: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))


# --------------------------------------------------------------------- runs
def create_run(trigger: str, window_start: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO sync_runs(trigger, status, started_at, window_start) VALUES(?, 'running', ?, ?)",
            (trigger, now_iso(), window_start),
        )
        return int(cur.lastrowid)


def update_run(run_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE sync_runs SET {cols} WHERE id=?", (*fields.values(), run_id))


def add_item(run_id: int, **fields: Any) -> None:
    fields["run_id"] = run_id
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with connect() as conn:
        conn.execute(f"INSERT INTO sync_items({cols}) VALUES({marks})", tuple(fields.values()))


def delete_run(run_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sync_items WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM sync_runs WHERE id=?", (run_id,))


def list_runs(limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sync_runs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0]
    return [dict(r) for r in rows], total


def get_run(run_id: int) -> dict | None:
    with connect() as conn:
        run = conn.execute("SELECT * FROM sync_runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            return None
        items = conn.execute(
            "SELECT * FROM sync_items WHERE run_id=? ORDER BY start_time DESC, id DESC", (run_id,)
        ).fetchall()
    return {**dict(run), "items": [dict(i) for i in items]}


def last_run() -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def last_successful_run() -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM sync_runs WHERE status IN ('success','partial') ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def clear_history() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sync_items")
        conn.execute("DELETE FROM sync_runs WHERE status != 'running'")


def totals() -> dict:
    with connect() as conn:
        synced = conn.execute("SELECT COUNT(*) FROM synced_activities").fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0]
    return {"synced_activities": synced, "runs": runs}


# ----------------------------------------------------------- synced set
def synced_ids(ids: list[str]) -> set[str]:
    if not ids:
        return set()
    marks = ", ".join("?" for _ in ids)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT garmin_activity_id FROM synced_activities WHERE garmin_activity_id IN ({marks})",
            ids,
        ).fetchall()
    return {r[0] for r in rows}


def mark_synced(activity_id: str, status: str, run_id: int, import_id: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO synced_activities(garmin_activity_id, status, run_id, coros_import_id, synced_at) "
            "VALUES(?, ?, ?, ?, ?) ON CONFLICT(garmin_activity_id) DO UPDATE SET "
            "status=excluded.status, run_id=excluded.run_id, coros_import_id=excluded.coros_import_id, "
            "synced_at=excluded.synced_at",
            (activity_id, status, run_id, import_id, now_iso()),
        )


def unmark_synced(activity_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM synced_activities WHERE garmin_activity_id=?", (activity_id,))
