"""SQLite persistence: tasks, imported files, programmes, listing-page coverage and events."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    folder TEXT NOT NULL,
    delete_html INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'queued',
    scanned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    run_seconds REAL NOT NULL DEFAULT 0,
    files_total INTEGER NOT NULL DEFAULT 0,
    listing_total INTEGER NOT NULL DEFAULT 0,
    detail_total INTEGER NOT NULL DEFAULT 0,
    files_done INTEGER NOT NULL DEFAULT 0,
    files_failed INTEGER NOT NULL DEFAULT 0,
    files_blocked INTEGER NOT NULL DEFAULT 0,
    files_skipped INTEGER NOT NULL DEFAULT 0,
    listing_done INTEGER NOT NULL DEFAULT 0,
    detail_done INTEGER NOT NULL DEFAULT 0,
    cards_found INTEGER NOT NULL DEFAULT 0,
    programmes_created INTEGER NOT NULL DEFAULT 0,
    programmes_updated INTEGER NOT NULL DEFAULT 0,
    json_files_written INTEGER NOT NULL DEFAULT 0,
    html_deleted INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    current_file TEXT,
    last_action TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS task_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    size INTEGER,
    sha256 TEXT,
    kind TEXT,
    portal TEXT,
    source_url TEXT,
    page_number INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT,
    cards INTEGER NOT NULL DEFAULT 0,
    programmes INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    processed_at TEXT,
    deleted INTEGER NOT NULL DEFAULT 0,
    UNIQUE (task_id, path)
);
CREATE INDEX IF NOT EXISTS idx_task_files_status ON task_files(task_id, status);
CREATE TABLE IF NOT EXISTS programmes (
    category TEXT NOT NULL,
    programme_key TEXT NOT NULL,
    study_id TEXT,
    program_url TEXT,
    university_key TEXT,
    listing_json TEXT,
    detail_json TEXT,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    first_task_id TEXT,
    last_task_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (category, programme_key)
);
CREATE INDEX IF NOT EXISTS idx_programmes_university ON programmes(category, university_key);
CREATE TABLE IF NOT EXISTS universities (
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    filename TEXT NOT NULL,
    PRIMARY KEY (category, key),
    UNIQUE (category, filename)
);
CREATE TABLE IF NOT EXISTS listing_pages (
    category TEXT NOT NULL,
    page INTEGER NOT NULL,
    total_pages INTEGER,
    cards INTEGER NOT NULL,
    file_name TEXT,
    task_id TEXT,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (category, page)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
"""

TASK_COUNTERS = ("files_done", "files_failed", "files_blocked", "files_skipped", "listing_done", "detail_done",
                 "cards_found", "programmes_created", "programmes_updated", "json_files_written",
                 "html_deleted", "retries")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Thread-safe wrapper around one SQLite connection (WAL mode)."""

    SCHEMA = SCHEMA
    COUNTERS = TASK_COUNTERS
    # Columns added after the first release: (table, column, declaration), applied to existing databases.
    MIGRATIONS: tuple[tuple[str, str, str], ...] = (("tasks", "output_dir", "TEXT"),)

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        # Autocommit mode: single statements commit immediately; multi-statement units use transaction().
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(self.SCHEMA)
            for table, column, declaration in self.MIGRATIONS:
                columns = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- low-level helpers -------------------------------------------------------------------

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def query(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params=()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def transaction(self):
        return _Transaction(self)

    # --- tasks ------------------------------------------------------------------------------

    def create_task(self, task_id: str, category: str, folder: str, delete_html: bool) -> dict:
        now = utcnow()
        self.execute("INSERT INTO tasks (id, category, folder, delete_html, status, stage, created_at, updated_at) "
                     "VALUES (?, ?, ?, ?, 'queued', 'queued', ?, ?)",
                     (task_id, category, folder, int(delete_html), now, now))
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict | None:
        return self.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def update_task(self, task_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = utcnow()
        names = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE tasks SET {names} WHERE id = ?", (*fields.values(), task_id))

    def bump_task(self, task_id: str, **increments) -> None:
        unknown = set(increments) - set(self.COUNTERS)
        if unknown:
            raise ValueError(f"unknown counters: {unknown}")
        sets = ", ".join(f"{k} = {k} + ?" for k in increments)
        self.execute(f"UPDATE tasks SET {sets}, updated_at = ? WHERE id = ?",
                     (*increments.values(), utcnow(), task_id))

    def list_tasks(self, limit: int = 50, category: str | None = None) -> list[dict]:
        if category:
            return self.query("SELECT * FROM tasks WHERE category = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                              (category, limit))
        return self.query("SELECT * FROM tasks ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))

    def tasks_with_status(self, *statuses: str) -> list[dict]:
        marks = ", ".join("?" for _ in statuses)
        return self.query(f"SELECT * FROM tasks WHERE status IN ({marks})", statuses)

    # --- task files -------------------------------------------------------------------------

    def add_file(self, task_id: str, path: str, name: str, size: int, kind: str | None,
                 page_number: int | None) -> None:
        self.execute("INSERT OR IGNORE INTO task_files (task_id, path, name, size, kind, page_number) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (task_id, path, name, size, kind, page_number))

    def update_file(self, file_id: int, **fields) -> None:
        names = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE task_files SET {names} WHERE id = ?", (*fields.values(), file_id))

    def next_pending_file(self, task_id: str) -> dict | None:
        """Listing pages first (in page order), then detail pages, then anything unclassified."""
        return self.query_one(
            "SELECT * FROM task_files WHERE task_id = ? AND status = 'pending' ORDER BY "
            "CASE kind WHEN 'listing' THEN 0 WHEN 'detail' THEN 1 ELSE 2 END, "
            "COALESCE(page_number, 1000000), name, id LIMIT 1", (task_id,))

    def files(self, task_id: str, status: str | None = None) -> list[dict]:
        if status:
            return self.query("SELECT * FROM task_files WHERE task_id = ? AND status = ? ORDER BY id",
                              (task_id, status))
        return self.query("SELECT * FROM task_files WHERE task_id = ? ORDER BY "
                          "CASE status WHEN 'failed' THEN 0 WHEN 'blocked' THEN 1 WHEN 'processing' THEN 2 "
                          "WHEN 'pending' THEN 3 WHEN 'skipped' THEN 4 ELSE 5 END, id", (task_id,))

    def file_status_counts(self, task_id: str) -> dict[str, int]:
        rows = self.query("SELECT status, COUNT(*) AS n FROM task_files WHERE task_id = ? GROUP BY status",
                          (task_id,))
        return {r["status"]: r["n"] for r in rows}

    def find_file_by_hash(self, task_id: str, sha256: str, exclude_id: int) -> dict | None:
        return self.query_one("SELECT * FROM task_files WHERE task_id = ? AND sha256 = ? AND id != ? "
                              "AND status = 'done'", (task_id, sha256, exclude_id))

    # --- events -----------------------------------------------------------------------------

    def add_event(self, task_id: str | None, level: str, message: str, data: dict | None = None) -> None:
        self.execute("INSERT INTO events (task_id, ts, level, message, data_json) VALUES (?, ?, ?, ?, ?)",
                     (task_id, utcnow(), level, message,
                      json.dumps(data, ensure_ascii=False) if data else None))

    def events(self, task_id: str, after_id: int = 0, limit: int = 500) -> list[dict]:
        rows = self.query("SELECT * FROM events WHERE task_id = ? AND id > ? ORDER BY id LIMIT ?",
                          (task_id, after_id, limit))
        for row in rows:
            row["data"] = json.loads(row.pop("data_json")) if row.get("data_json") else None
        return rows

    # --- programmes and universities --------------------------------------------------------

    def get_programme(self, category: str, key: str) -> dict | None:
        return self.query_one("SELECT * FROM programmes WHERE category = ? AND programme_key = ?",
                              (category, key))

    def programmes_for_university(self, category: str, ukey: str) -> list[dict]:
        return self.query("SELECT * FROM programmes WHERE category = ? AND university_key = ? "
                          "ORDER BY programme_key", (category, ukey))

    def programme_keys(self, category: str) -> set[str]:
        rows = self.query("SELECT programme_key, program_url FROM programmes WHERE category = ?", (category,))
        return {r["programme_key"] for r in rows} | {r["program_url"] for r in rows if r["program_url"]}

    def university(self, category: str, key: str) -> dict | None:
        return self.query_one("SELECT * FROM universities WHERE category = ? AND key = ?", (category, key))

    def universities(self, category: str) -> list[dict]:
        return self.query("SELECT * FROM universities WHERE category = ? ORDER BY display_name", (category,))

    def missing_details(self, category: str, limit: int | None = None) -> list[dict]:
        sql = ("SELECT programme_key, study_id, program_url, listing_json FROM programmes WHERE category = ? "
               "AND detail_json IS NULL AND program_url IS NOT NULL ORDER BY created_at, programme_key")
        rows = self.query(sql + (" LIMIT ?" if limit else ""), (category, limit) if limit else (category,))
        for row in rows:
            listing = json.loads(row.pop("listing_json") or "{}")
            row["university_name"] = listing.get("university_name")
            row["program_name"] = listing.get("program_name")
        return rows

    def category_stats(self, category: str) -> dict:
        row = self.query_one(
            "SELECT COUNT(*) AS programmes, "
            "SUM(CASE WHEN listing_json IS NOT NULL AND detail_json IS NOT NULL THEN 1 ELSE 0 END) AS complete, "
            "SUM(CASE WHEN detail_json IS NULL THEN 1 ELSE 0 END) AS listing_only, "
            "SUM(CASE WHEN listing_json IS NULL THEN 1 ELSE 0 END) AS detail_only, "
            "COUNT(DISTINCT university_key) AS universities "
            "FROM programmes WHERE category = ?", (category,))
        pages = self.query_one("SELECT COUNT(*) AS pages, MAX(total_pages) AS total_pages "
                               "FROM listing_pages WHERE category = ?", (category,))
        return {k: (v or 0) for k, v in {**row, "listing_pages_imported": pages["pages"]}.items()} | {
            "listing_pages_total": pages["total_pages"]}

    def imported_pages(self, category: str) -> set[int]:
        return {r["page"] for r in self.query("SELECT page FROM listing_pages WHERE category = ?", (category,))}


class _Transaction:
    """``with store.transaction() as conn:`` — one atomic commit under the store lock."""

    def __init__(self, store: Store):
        self.store = store

    def __enter__(self) -> sqlite3.Connection:
        self.store._lock.acquire()
        self.store._conn.execute("BEGIN IMMEDIATE")
        return self.store._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.store._conn.commit()
            else:
                self.store._conn.rollback()
        finally:
            self.store._lock.release()
