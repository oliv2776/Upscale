from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config


def now() -> str:
    return datetime.now(UTC).isoformat()


JOB_COLUMNS = {
    "progress": "REAL",
    "eta_seconds": "INTEGER",
    "settings_json": "TEXT",
    "position": "REAL",
    "priority": "INTEGER NOT NULL DEFAULT 0",
    "kind": "TEXT NOT NULL DEFAULT 'upscale'",
    "warning": "TEXT",
    "segments_done": "INTEGER",
    "segments_total": "INTEGER",
    "resolved_model": "TEXT",
    "trash_path": "TEXT",
    "trashed_at": "TEXT",
}


def migrate_legacy_files() -> None:
    """Ancien nom de la base (versions « Uscale »)."""
    legacy = config.STATE_DIR / "uscale.sqlite3"
    if legacy.exists() and not config.db_path().exists():
        for suffix in ("", "-wal", "-shm"):
            old = Path(str(legacy) + suffix)
            if old.exists():
                old.rename(str(config.db_path()) + suffix)


def init_db() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    migrate_legacy_files()
    config.log_dir().mkdir(parents=True, exist_ok=True)
    config.preview_dir().mkdir(parents=True, exist_ok=True)
    config.thumbnail_dir().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.db_path(), timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
          CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            destination TEXT NOT NULL,
            source_size INTEGER NOT NULL,
            source_mtime_ns INTEGER NOT NULL,
            width INTEGER,
            height INTEGER,
            status TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT '',
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source, source_size, source_mtime_ns)
          )
        """)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        for name, ddl in JOB_COLUMNS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")
        # Ordre de la file : les anciennes tâches gardent leur ordre de création.
        conn.execute("UPDATE jobs SET position=CAST(strftime('%s', created_at) AS REAL) WHERE position IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, priority, position)")
        conn.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("""
          CREATE TABLE IF NOT EXISTS probe_cache (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            width INTEGER,
            height INTEGER,
            updated_at TEXT NOT NULL
          )
        """)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(config.db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def load_settings_payload() -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT payload FROM settings WHERE id=1").fetchone()
    return json.loads(row["payload"]) if row else {}


def store_settings_payload(settings: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(id,payload) VALUES(1,?)", (json.dumps(settings),))


def get_state(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES(?,?)", (key, value))


def is_paused() -> bool:
    return get_state("paused") == "1"


def set_paused(value: bool) -> None:
    set_state("paused", "1" if value else "0")


def set_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    assignments = ",".join(f"{key}=?" for key in fields)
    with connect() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*fields.values(), job_id))


def get_job(job_id: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def log_line(job_id: str, line: str) -> None:
    with (config.log_dir() / f"{job_id}.log").open("a", encoding="utf-8") as stream:
        stream.write(line.rstrip() + "\n")
