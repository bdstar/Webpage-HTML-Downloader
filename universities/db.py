"""SQLite persistence for university import tasks, per-file history and university-to-file mapping."""

from __future__ import annotations

import json

from programmes.db import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    folder TEXT NOT NULL,
    delete_html INTEGER NOT NULL DEFAULT 0,
    policy TEXT NOT NULL DEFAULT 'update',
    status TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'queued',
    file_stage TEXT,
    scanned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    run_seconds REAL NOT NULL DEFAULT 0,
    files_total INTEGER NOT NULL DEFAULT 0,
    eligible_total INTEGER NOT NULL DEFAULT 0,
    files_done INTEGER NOT NULL DEFAULT 0,
    files_failed INTEGER NOT NULL DEFAULT 0,
    files_blocked INTEGER NOT NULL DEFAULT 0,
    files_skipped INTEGER NOT NULL DEFAULT 0,
    with_warnings INTEGER NOT NULL DEFAULT 0,
    universities_created INTEGER NOT NULL DEFAULT 0,
    universities_updated INTEGER NOT NULL DEFAULT 0,
    universities_unchanged INTEGER NOT NULL DEFAULT 0,
    json_files_written INTEGER NOT NULL DEFAULT 0,
    html_deleted INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    current_file TEXT,
    current_university TEXT,
    last_action TEXT,
    last_success TEXT,
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
    status TEXT NOT NULL DEFAULT 'pending',
    outcome TEXT,
    reason TEXT,
    university TEXT,
    university_key TEXT,
    output_path TEXT,
    missing_json TEXT NOT NULL DEFAULT '[]',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    attempts INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    processed_at TEXT,
    deleted INTEGER NOT NULL DEFAULT 0,
    UNIQUE (task_id, path)
);
CREATE INDEX IF NOT EXISTS idx_task_files_status ON task_files(task_id, status);
CREATE TABLE IF NOT EXISTS universities (
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    filename TEXT NOT NULL COLLATE NOCASE,
    university_id TEXT,
    source_url TEXT,
    first_task_id TEXT,
    last_task_id TEXT,
    last_file TEXT,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (category, key),
    UNIQUE (category, filename)
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

COUNTERS = ("html_deleted", "retries")


class UniversityStore(Store):
    SCHEMA = SCHEMA
    COUNTERS = COUNTERS
    MIGRATIONS = (("tasks", "output_dir", "TEXT"),)

    def add_file(self, task_id: str, path: str, name: str, size: int, kind: str) -> None:
        self.execute("INSERT OR IGNORE INTO task_files (task_id, path, name, size, kind) VALUES (?, ?, ?, ?, ?)",
                     (task_id, path, name, size, kind))

    def next_pending_file(self, task_id: str) -> dict | None:
        return self.query_one("SELECT * FROM task_files WHERE task_id = ? AND status = 'pending' "
                              "ORDER BY name, id LIMIT 1", (task_id,))

    def files(self, task_id: str, status: str | None = None) -> list[dict]:
        rows = super().files(task_id, status)
        for row in rows:
            row["missing"] = json.loads(row.pop("missing_json") or "[]")
            row["warnings"] = json.loads(row.pop("warnings_json") or "[]")
        return rows

    def university(self, category: str, key: str) -> dict | None:
        return self.query_one("SELECT * FROM universities WHERE category = ? AND key = ?", (category, key))

    def university_by_filename(self, category: str, filename: str) -> dict | None:
        return self.query_one("SELECT * FROM universities WHERE category = ? AND filename = ?",
                              (category, filename))

    def category_stats(self, category: str) -> dict:
        row = self.query_one("SELECT COUNT(*) AS universities, MAX(imported_at) AS last_import "
                             "FROM universities WHERE category = ?", (category,))
        return {"universities": row["universities"] or 0, "last_import": row["last_import"]}
