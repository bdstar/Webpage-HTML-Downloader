"""Import tasks: scan a folder of saved pages, process them one by one, persist everything."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from downloader.config import BASE_DIR
from downloader.folders import FolderError, validate_download_dir

from .classify import KIND_BLOCKED, KIND_DETAIL, KIND_LISTING, classify, make_soup
from .config import ImportSettings
from .dataset import Dataset
from .db import Store, utcnow
from .detail import parse_detail
from .listing import parse_listing
from .sources import PORTALS

ACTIVE = ("queued", "running", "pausing", "paused", "stopping")
RESUMABLE = ("paused", "interrupted", "stopped")
HTML_SUFFIXES = (".html", ".htm")
SNIFF_PAGE = re.compile(r'data-current-page="(\d+)"')


class TaskError(Exception):
    def __init__(self, message: str, http_status: int = 400, task: dict | None = None):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.task = task


def sniff(text: str) -> tuple[str | None, int | None]:
    """Cheap kind/page guess used only to order the queue (full classification happens later)."""
    if 'ProgrammeCard' in text:
        match = SNIFF_PAGE.search(text)
        return KIND_LISTING, int(match.group(1)) if match else None
    if 'id="StudyKeyFacts"' in text:
        return KIND_DETAIL, None
    return None, None


def is_default_folder(raw: str | None, default: Path) -> bool:
    text = (raw or "").strip().strip('"')
    if not text:
        return True
    try:
        return Path(os.path.expandvars(text)).expanduser().resolve() == default.resolve()
    except (OSError, ValueError):
        return False


def export_folder_info(raw: str | None, default: Path, pattern: str = "*.json") -> dict:
    """Validate an export folder without creating anything (the default folder may not exist yet).
    ``json_files`` counts the files matching ``pattern`` inside it."""
    if is_default_folder(raw, default):
        path = default.resolve()
        exists = path.is_dir()
    else:
        try:
            path = validate_download_dir(raw)
        except FolderError as exc:
            raise TaskError(exc.message) from None
        exists = True
    return {"folder": str(path), "is_default": path == default.resolve(), "exists": exists,
            "json_files": sum(1 for _ in path.glob(pattern)) if exists else 0}


def resolve_export_folder(raw: str | None, default: Path) -> Path:
    """The folder JSON files are written to: the category default (created if needed) or an existing,
    writable folder chosen by the user."""
    if is_default_folder(raw, default):
        default.mkdir(parents=True, exist_ok=True)
        raw = str(default)
    try:
        return validate_download_dir(raw)
    except FolderError as exc:
        raise TaskError(f"Export folder: {exc.message}") from None


def decode_html(data: bytes) -> tuple[str, bool]:
    try:
        return data.decode("utf-8-sig"), True
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), False


class ImportManager:
    def __init__(self, settings: ImportSettings, store: Store | None = None):
        self.settings = settings
        self.store = store or Store(settings.db_path)
        self.dataset = Dataset(self.store, settings.output_root)
        self._cond = threading.Condition()
        self._controls: dict[str, str | None] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._run_started: dict[str, float] = {}
        for category in PORTALS:
            settings.import_folder(category).mkdir(parents=True, exist_ok=True)
        self.recover()

    # --- lifecycle --------------------------------------------------------------------------

    def recover(self) -> None:
        """Tasks that were running when the app stopped become 'interrupted' and can be resumed."""
        for task in self.store.tasks_with_status("queued", "running", "pausing", "stopping", "paused"):
            if task["id"] in self._threads:
                continue
            self.store.execute("UPDATE task_files SET status = 'pending' WHERE task_id = ? AND status = 'processing'",
                               (task["id"],))
            if task["status"] == "paused":
                continue
            self.store.update_task(task["id"], status="interrupted", stage="interrupted", current_file=None,
                                   last_action="The app stopped while this task was running; press Resume to continue.")
            self.store.add_event(task["id"], "warning", "Task interrupted by an application restart. "
                                                        "Completed files are kept; press Resume to continue.")

    def create(self, category: str, folder: str, delete_html: bool, export_folder: str | None = None) -> dict:
        if category not in PORTALS:
            raise TaskError("Choose Master's or Bachelor's.")
        raw = (folder or "").strip().strip('"')
        path = Path(raw).expanduser() if raw else self.settings.import_folder(category)
        if not path.is_absolute():
            path = BASE_DIR / path
        path = path.resolve()
        if not path.is_dir():
            raise TaskError(f"Import folder not found: {path}")
        output = resolve_export_folder(export_folder, self.dataset.output_dir(category))
        self._check_conflicts(category, str(path))
        task_id = uuid.uuid4().hex[:12]
        self.store.create_task(task_id, category, str(path), delete_html)
        self.store.update_task(task_id, output_dir=str(output))
        self.store.add_event(task_id, "info", f"Task created for {PORTALS[category].label} pages in {path}; "
                                              f"JSON files go to {output}",
                             {"delete_html": delete_html, "output_dir": str(output)})
        self._start_thread(task_id)
        return self.snapshot(task_id)

    def task_output(self, task: dict) -> Path:
        """Export folder of a task (tasks created before the setting existed use the category default)."""
        return Path(task["output_dir"]) if task.get("output_dir") else self.dataset.output_dir(task["category"])

    def _check_conflicts(self, category: str, folder: str, ignore: str | None = None) -> None:
        for task in self.store.tasks_with_status(*ACTIVE):
            if task["id"] == ignore:
                continue
            if task["category"] == category:
                raise TaskError(f"A {PORTALS[category].label} import task is already {task['status']}.",
                                409, self.snapshot(task["id"]))
            if Path(task["folder"]) == Path(folder):
                raise TaskError("Another task is already using this folder.", 409, self.snapshot(task["id"]))

    def _start_thread(self, task_id: str) -> None:
        with self._cond:
            self._controls[task_id] = None
            thread = threading.Thread(target=self._run, args=(task_id,), name=f"import-{task_id}", daemon=True)
            self._threads[task_id] = thread
        thread.start()

    def pause(self, task_id: str) -> dict:
        task = self._task(task_id)
        if task["status"] not in ("running", "queued"):
            raise TaskError(f"Only a running task can be paused (this one is {task['status']}).", 409)
        with self._cond:
            self._controls[task_id] = "pause"
            self._cond.notify_all()
        self.store.update_task(task_id, status="pausing", last_action="Pausing after the current file…")
        self.store.add_event(task_id, "info", "Pause requested; the current file will finish first.")
        return self.snapshot(task_id)

    def resume(self, task_id: str) -> dict:
        task = self._task(task_id)
        if task["status"] not in RESUMABLE and task["status"] != "pausing":
            raise TaskError(f"This task cannot be resumed (it is {task['status']}).", 409)
        self._check_conflicts(task["category"], task["folder"], ignore=task_id)
        self.store.add_event(task_id, "info", "Task resumed.")
        with self._cond:
            alive = task_id in self._threads and self._threads[task_id].is_alive()
            self._controls[task_id] = None
            self._cond.notify_all()
        if alive:
            self.store.update_task(task_id, status="running")
        else:
            self.store.update_task(task_id, status="queued", stage="queued", finished_at=None)
            self._start_thread(task_id)
        return self.snapshot(task_id)

    def stop(self, task_id: str) -> dict:
        task = self._task(task_id)
        if task["status"] in ("running", "queued", "pausing", "paused") and self._alive(task_id):
            with self._cond:
                self._controls[task_id] = "stop"
                self._cond.notify_all()
            self.store.update_task(task_id, status="stopping", last_action="Stopping after the current file…")
            self.store.add_event(task_id, "info", "Stop requested; the current file will finish first.")
        elif task["status"] in ("paused", "interrupted", "queued"):
            self.store.update_task(task_id, status="stopped", stage="stopped", finished_at=utcnow(),
                                   current_file=None, last_action="Stopped.")
            self.store.add_event(task_id, "info", "Task stopped.")
        else:
            raise TaskError(f"This task is not running (it is {task['status']}).", 409)
        return self.snapshot(task_id)

    def retry_failed(self, task_id: str) -> dict:
        task = self._task(task_id)
        if task["status"] in ACTIVE:
            raise TaskError("Wait for the task to finish, pause or stop before retrying.", 409)
        cursor = self.store.execute("UPDATE task_files SET status = 'pending', reason = NULL WHERE task_id = ? "
                                    "AND status IN ('failed', 'blocked')", (task_id,))
        if cursor.rowcount == 0:
            raise TaskError("There are no failed or blocked files to retry.", 409)
        self._check_conflicts(task["category"], task["folder"], ignore=task_id)
        self.store.bump_task(task_id, retries=cursor.rowcount)
        self.store.add_event(task_id, "info", f"Retrying {cursor.rowcount} failed/blocked file(s).")
        self.store.update_task(task_id, status="queued", stage="queued", finished_at=None, last_error=None)
        self._start_thread(task_id)
        return self.snapshot(task_id)

    def wait(self, task_id: str, timeout: float = 30) -> None:
        """Block until the worker thread for ``task_id`` ends (used by tests and shutdown)."""
        thread = self._threads.get(task_id)
        if thread:
            thread.join(timeout)

    # --- views ------------------------------------------------------------------------------

    def _task(self, task_id: str) -> dict:
        task = self.store.get_task(task_id)
        if task is None:
            raise TaskError("Task not found.", 404)
        return task

    def _alive(self, task_id: str) -> bool:
        thread = self._threads.get(task_id)
        return bool(thread and thread.is_alive())

    def snapshot(self, task_id: str) -> dict:
        task = self._task(task_id)
        counts = self.store.file_status_counts(task_id)
        pending = counts.get("pending", 0) + counts.get("processing", 0)
        finished = sum(counts.get(k, 0) for k in ("done", "failed", "blocked", "skipped"))
        elapsed = task["run_seconds"] + (time.monotonic() - self._run_started[task_id]
                                         if task_id in self._run_started else 0)
        total = task["files_total"]
        task.update({
            "label": PORTALS[task["category"]].label,
            "output_dir": str(self.task_output(task)),
            "file_counts": counts,
            "files_pending": pending,
            "percent": round(100 * finished / total, 1) if total else (100.0 if task["scanned"] else 0.0),
            "elapsed_seconds": round(elapsed, 1),
            "eta_seconds": round(elapsed / finished * pending, 1) if finished and pending else None,
            "is_active": task["status"] in ACTIVE,
            "can_pause": task["status"] in ("running", "queued"),
            "can_resume": task["status"] in RESUMABLE,
            "can_stop": task["status"] in ("running", "queued", "pausing", "paused", "interrupted"),
            "can_retry": task["status"] not in ACTIVE and (counts.get("failed", 0) + counts.get("blocked", 0)) > 0,
            "delete_html": bool(task["delete_html"]),
        })
        return task

    def overview(self) -> dict:
        categories = {}
        for category, portal in PORTALS.items():
            active = [t for t in self.store.list_tasks(20, category) if t["status"] in ACTIVE + ("interrupted",)]
            categories[category] = {
                "label": portal.label,
                "base_listing_url": portal.base_listing_url,
                "import_folder": str(self.settings.import_folder(category)),
                "output_dir": str(self.dataset.output_dir(category)),
                "stats": self.store.category_stats(category),
                "current_task": self.snapshot(active[0]["id"]) if active else None,
            }
        return {"categories": categories, "delete_html_default": self.settings.delete_html_default}

    # --- worker -----------------------------------------------------------------------------

    def _checkpoint(self, task_id: str) -> str | None:
        """Honour pause/stop between files. Returns "stop" when the task must end now."""
        with self._cond:
            if self._controls.get(task_id) == "pause":
                self._account_time(task_id)
                self.store.update_task(task_id, status="paused", stage="paused", current_file=None,
                                       last_action="Paused. Press Resume to continue.")
                self.store.add_event(task_id, "info", "Task paused.")
                while self._controls.get(task_id) == "pause":
                    self._cond.wait(timeout=1.0)
                self._run_started[task_id] = time.monotonic()
                if self._controls.get(task_id) != "stop":
                    self.store.update_task(task_id, status="running", stage="processing")
            return "stop" if self._controls.get(task_id) == "stop" else None

    def _account_time(self, task_id: str) -> None:
        started = self._run_started.pop(task_id, None)
        if started is not None:
            task = self.store.get_task(task_id)
            self.store.update_task(task_id, run_seconds=task["run_seconds"] + time.monotonic() - started)

    def _run(self, task_id: str) -> None:
        self._run_started[task_id] = time.monotonic()
        try:
            task = self.store.get_task(task_id)
            self.store.update_task(task_id, status="running", stage="scanning" if not task["scanned"] else "processing",
                                   started_at=task["started_at"] or utcnow(), last_error=None)
            if not task["scanned"]:
                self._scan(task)
                task = self.store.get_task(task_id)
            if task["files_total"] == 0:
                self._account_time(task_id)
                self.store.update_task(task_id, status="empty", stage="finished", finished_at=utcnow(),
                                       last_action="No .html files were found in the import folder.")
                self.store.add_event(task_id, "warning", "No .html files found; nothing was imported.")
                return
            self.store.update_task(task_id, stage="processing")
            while True:
                if self._checkpoint(task_id) == "stop":
                    self._account_time(task_id)
                    self.store.update_task(task_id, status="stopped", stage="stopped", finished_at=utcnow(),
                                           current_file=None, last_action="Stopped. Completed files are saved.")
                    self.store.add_event(task_id, "info", "Task stopped by the user.")
                    return
                item = self.store.next_pending_file(task_id)
                if item is None:
                    break
                self._process(task, item)
            self._finish(task_id)
        except Exception as exc:  # unexpected: record it rather than dying silently
            self._account_time(task_id)
            self.store.update_task(task_id, status="failed", stage="failed", finished_at=utcnow(),
                                   current_file=None, last_error=f"{type(exc).__name__}: {exc}")
            self.store.add_event(task_id, "error", f"Task failed: {type(exc).__name__}: {exc}")
        finally:
            self._run_started.pop(task_id, None)
            with self._cond:
                if self._threads.get(task_id) is threading.current_thread():
                    self._threads.pop(task_id, None)
                self._controls.pop(task_id, None)

    def _finish(self, task_id: str) -> None:
        self._account_time(task_id)
        counts = self._refresh_counts(task_id)
        problems = counts.get("failed", 0) + counts.get("blocked", 0)
        status = "completed_with_errors" if problems else "completed"
        message = (f"Finished: {counts.get('done', 0)} file(s) imported, {counts.get('failed', 0)} failed, "
                   f"{counts.get('blocked', 0)} blocked, {counts.get('skipped', 0)} skipped.")
        self.store.update_task(task_id, status=status, stage="finished", finished_at=utcnow(), current_file=None,
                               last_action=message)
        self.store.add_event(task_id, "warning" if problems else "info", message, counts)

    def _refresh_counts(self, task_id: str) -> dict:
        counts = self.store.file_status_counts(task_id)
        kinds = {(r["kind"], r["status"]): r["n"] for r in self.store.query(
            "SELECT kind, status, COUNT(*) AS n FROM task_files WHERE task_id = ? GROUP BY kind, status", (task_id,))}
        self.store.update_task(task_id, files_done=counts.get("done", 0), files_failed=counts.get("failed", 0),
                               files_blocked=counts.get("blocked", 0), files_skipped=counts.get("skipped", 0),
                               listing_done=kinds.get((KIND_LISTING, "done"), 0),
                               detail_done=kinds.get((KIND_DETAIL, "done"), 0))
        return counts

    def _scan(self, task: dict) -> None:
        folder = Path(task["folder"])
        if not folder.is_dir():
            raise FileNotFoundError(f"Import folder no longer exists: {folder}")
        paths = sorted(
            p for p in folder.rglob("*")
            if p.suffix.lower() in HTML_SUFFIXES and p.is_file() and not p.is_symlink()
            and not any(part.endswith("_files") for part in p.relative_to(folder).parts[:-1]))
        listing = detail = 0
        for path in paths:
            self.store.update_task(task["id"], current_file=path.name, last_action=f"Scanning {path.name}")
            try:
                kind, page = sniff(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                kind, page = None, None
            listing += kind == KIND_LISTING
            detail += kind == KIND_DETAIL
            self.store.add_file(task["id"], str(path), path.name, path.stat().st_size, kind, page)
        self.store.update_task(task["id"], scanned=1, files_total=len(paths), listing_total=listing,
                               detail_total=detail, current_file=None,
                               last_action=f"Found {len(paths)} HTML file(s): {listing} listing, {detail} detail.")
        self.store.add_event(task["id"], "info", f"Scanned folder: {len(paths)} HTML file(s), "
                                                 f"{listing} listing page(s), {detail} programme page(s).")

    # --- one file ---------------------------------------------------------------------------

    def _process(self, task: dict, item: dict) -> None:
        task_id = task["id"]
        self.store.update_file(item["id"], status="processing", attempts=item["attempts"] + 1)
        self.store.update_task(task_id, current_file=item["name"], last_action=f"Processing {item['name']}")
        try:
            outcome = self._import_file(task, item)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}" if not isinstance(exc, OSError) else str(exc)
            self.store.update_file(item["id"], status="failed", reason=reason, processed_at=utcnow())
            self.store.update_task(task_id, last_error=f"{item['name']}: {reason}")
            self.store.add_event(task_id, "error", f"Failed: {item['name']}: {reason}", {"file": item["path"]})
            self._refresh_counts(task_id)
            return
        status, reason = outcome
        if status != "done":
            self.store.update_file(item["id"], status=status, reason=reason, processed_at=utcnow())
            level = "warning" if status == "blocked" else "info"
            self.store.add_event(task_id, level, f"{status.capitalize()}: {item['name']}: {reason}",
                                 {"file": item["path"]})
        self._refresh_counts(task_id)
        if status == "done" and task["delete_html"]:
            self._delete_source(task_id, item)

    def _import_file(self, task: dict, item: dict) -> tuple[str, str | None]:
        task_id, category = task["id"], task["category"]
        path = Path(item["path"])
        if not path.is_file():
            raise FileNotFoundError(f"File no longer exists (moved or deleted?): {path}")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        self.store.update_file(item["id"], sha256=digest, size=len(data))
        duplicate = self.store.find_file_by_hash(task_id, digest, item["id"])
        if duplicate:
            return "skipped", f"identical content to {duplicate['name']}, already imported"

        html, valid_utf8 = decode_html(data)
        soup = make_soup(html)
        info = classify(html, soup)
        self.store.update_file(item["id"], kind=info.kind, portal=info.portal.category if info.portal else None,
                               source_url=info.source_url, page_number=info.page_number)
        if info.kind == KIND_BLOCKED:
            return "blocked", info.reason
        if info.kind not in (KIND_LISTING, KIND_DETAIL):
            return "skipped", info.reason
        if info.portal is None:
            return "skipped", "could not tell whether this is a Mastersportal or Bachelorsportal page"
        if info.portal.category != category:
            return "skipped", (f"this is a {info.portal.label} page ({info.portal.host}); import it with the "
                               f"{info.portal.label} category")
        if not valid_utf8:
            self.store.add_event(task_id, "warning", f"{item['name']} is not valid UTF-8; "
                                                     "undecodable bytes were replaced.")

        source = {"url": info.source_url, "file": item["name"]}
        created = updated = cards = 0
        affected: set[str] = set()
        if info.kind == KIND_LISTING:
            result = parse_listing(soup, info.portal)
            if not result.cards:
                raise ValueError("listing page contains no readable programme cards")
            source["page"] = info.page_number
            with self.store.transaction() as conn:
                for card in result.cards:
                    new, keys = self.dataset.upsert_listing(conn, category, card, source, task_id)
                    created += new
                    updated += not new
                    affected |= keys
                if info.page_number:
                    conn.execute("INSERT OR REPLACE INTO listing_pages (category, page, total_pages, cards, "
                                 "file_name, task_id, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (category, info.page_number, info.total_pages, len(result.cards), item["name"],
                                  task_id, utcnow()))
            cards = result.cards_found
            notes = []
            if result.skipped_positions:
                notes.append(f"{len(result.skipped_positions)} empty placeholder card(s) ignored")
            if result.duplicate_ids:
                notes.append(f"{len(result.duplicate_ids)} programme(s) listed twice on the page")
            without_link = sum(1 for c in result.cards if not c.study_id)
            if without_link:
                notes.append(f"{without_link} card(s) without a programme link (saved without URL)")
            detail_text = f"page {info.page_number}" if info.page_number else "listing page"
            message = (f"Imported {item['name']} ({detail_text}): {len(result.cards)} programme card(s), "
                       f"{created} new, {updated} updated" + (f"; {'; '.join(notes)}" if notes else ""))
        else:
            result = parse_detail(soup, info.portal, info.source_url)
            if not result.study_id:
                raise ValueError("could not determine which programme this page belongs to (no canonical URL)")
            with self.store.transaction() as conn:
                new, affected = self.dataset.upsert_detail(conn, category, result, source, task_id)
            created, updated = int(new), int(not new)
            message = (f"Imported {item['name']}: {result.data.get('program_name')} at "
                       f"{result.data.get('university_name')}" + (" (new programme)" if new else "")
                       + (f"; warnings: {'; '.join(result.warnings)}" if result.warnings else ""))

        written = self.dataset.write_universities(category, affected, self.task_output(task))
        self.store.update_file(item["id"], status="done", reason=None, cards=cards, programmes=created + updated,
                               processed_at=utcnow())
        self.store.bump_task(task_id, cards_found=cards, programmes_created=created, programmes_updated=updated,
                             json_files_written=written)
        self.store.update_task(task_id, last_action=message)
        self.store.add_event(task_id, "info", message, {"file": item["path"], "json_files": written})
        return "done", None

    def _delete_source(self, task_id: str, item: dict) -> None:
        """Remove an imported page (and its browser "<name>_files" folder) after its data is saved."""
        path = Path(item["path"])
        try:
            path.unlink()
            companion = path.with_name(f"{path.stem}_files")
            if companion.is_dir() and not companion.is_symlink():
                shutil.rmtree(companion)
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.store.add_event(task_id, "warning", f"Imported but could not delete {item['name']}: {exc}")
            return
        self.store.update_file(item["id"], deleted=1)
        self.store.bump_task(task_id, html_deleted=1)
