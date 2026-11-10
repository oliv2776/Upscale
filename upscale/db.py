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
        conn.execute("""
          CREATE TABLE IF NOT EXISTS library_items (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL UNIQUE,
            relative TEXT NOT NULL,
            destination TEXT NOT NULL,
            ambiguous INTEGER NOT NULL DEFAULT 0,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            scanned_at TEXT NOT NULL
          )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS library_source ON library_items(source)")
        version = conn.execute("SELECT value FROM app_state WHERE key='probe_cache_version'").fetchone()
        if not version or version[0] != str(config.PROBE_CACHE_VERSION):
            # Une ancienne méthode a pu enregistrer des dimensions de vignette/cover.
            conn.execute("DELETE FROM probe_cache")
            conn.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES('probe_cache_version',?)",
                         (str(config.PROBE_CACHE_VERSION),))
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


def replace_library_items(items: list[dict[str, Any]]) -> None:
    """Remplace le catalogue par le résultat complet d'une analyse."""
    with connect() as conn:
        conn.execute("DELETE FROM library_items")
        conn.executemany("""
          INSERT INTO library_items(id,source,relative,destination,ambiguous,width,height,size,mtime_ns,scanned_at)
          VALUES(?,?,?,?,?,?,?,?,?,?)
        """, [(item["id"], item["source"], item["relative"], item["destination"],
               int(bool(item.get("ambiguous"))), item["width"], item["height"], item["size"],
               item["mtime_ns"], now()) for item in items])


def library_item(item_id: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM library_items WHERE id=?", (item_id,)).fetchone()


def library_items_by_ids(ids: list[str]) -> list[sqlite3.Row]:
    if not ids:
        return []
    rows: list[sqlite3.Row] = []
    with connect() as conn:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            placeholders = ",".join("?" * len(chunk))
            rows.extend(conn.execute(f"SELECT * FROM library_items WHERE id IN ({placeholders})",
                                     chunk).fetchall())
    return rows


def library_page(page: int, limit: int, query: str = "", status: str = "all") -> dict[str, Any]:
    """Page du catalogue avec le dernier statut de tâche pour chaque vidéo."""
    join = """FROM library_items l
              LEFT JOIN jobs j ON j.id = (
                SELECT j2.id FROM jobs j2
                WHERE j2.source=l.source AND j2.source_size=l.size AND j2.source_mtime_ns=l.mtime_ns
                  AND j2.kind='upscale'
                ORDER BY j2.updated_at DESC LIMIT 1
              )"""
    where, params = [], []
    if query:
        where.append("(l.source LIKE ? OR l.relative LIKE ?)")
        params += [f"%{query}%", f"%{query}%"]
    if status == "new":
        where.append("(j.id IS NULL OR j.status IN ('failed','cancelled'))")
    elif status == "queued":
        where.append("j.status IN ('queued','running')")
    elif status == "done":
        where.append("j.status='completed'")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with connect() as conn:
        counts = conn.execute(f"""SELECT COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN j.id IS NULL OR j.status IN ('failed','cancelled') THEN 1 ELSE 0 END),0) AS new_count,
            COALESCE(SUM(CASE WHEN j.status IN ('queued','running') THEN 1 ELSE 0 END),0) AS queued_count,
            COALESCE(SUM(CASE WHEN j.status='completed' THEN 1 ELSE 0 END),0) AS done_count
            {join}""").fetchone()
        count = conn.execute(f"SELECT COUNT(*) AS n {join}{clause}", params).fetchone()["n"]
        rows = conn.execute(f"""SELECT l.*, j.status AS job_status, j.id AS job_id
            {join}{clause} ORDER BY l.source COLLATE NOCASE LIMIT ? OFFSET ?""",
                            [*params, limit, (page - 1) * limit]).fetchall()
    return {"items": rows, "total": count, "page": page, "limit": limit,
            "counts": {"total": counts["total"], "new": counts["new_count"],
                       "queued": counts["queued_count"], "done": counts["done_count"]}}


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
