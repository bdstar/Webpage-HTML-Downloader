"""University import tasks, built on the programme importer's task engine (pause/resume/stop/retry/recovery)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

from programmes.classify import make_soup
from programmes.db import utcnow
from programmes.storage import safe_filename, write_json_atomic
from programmes.tasks import (ACTIVE, HTML_SUFFIXES, RESUMABLE, ImportManager, TaskError, decode_html,
                              is_default_folder, resolve_export_folder)

from .categories import (CATEGORIES, DEFAULT_POLICY, POLICIES, UNIVERSITIES_SUBDIR, UniversityCategory,
                         country_folder)
from .config import UniversitySettings
from .db import UniversityStore
from .parser import UniversityPage, blocked_reason, looks_like_university_page, parse_university
from .records import RecordError, merge, name_key, read_existing, validate_record

KIND_UNIVERSITY = "university"
KIND_OTHER = "other"
MAX_NAME_VARIANTS = 50


class FileProblem(Exception):
    """An expected, user-facing reason why one file could not be imported."""


def discover(folder: Path) -> list[Path]:
    """HTML files in ``folder`` (and sub-folders), ignoring the "<page>_files" folders browsers save."""
    return sorted(
        p for p in folder.rglob("*")
        if p.suffix.lower() in HTML_SUFFIXES and p.is_file() and not p.is_symlink()
        and not any(part.endswith("_files") for part in p.relative_to(folder).parts[:-1]))


class UniversityImportManager(ImportManager):
    def __init__(self, settings: UniversitySettings, store: UniversityStore | None = None):
        self.settings = settings
        self.store = store or UniversityStore(settings.db_path)
        self._cond = threading.Condition()
        self._create_lock = threading.Lock()
        self._controls: dict[str, str | None] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._run_started: dict[str, float] = {}
        settings.import_folder.mkdir(parents=True, exist_ok=True)
        self.recover()

    def default_export(self) -> Path:
        """Default export folder; files go to <export>/<country>/universities/<University>.json."""
        return self.settings.export_root

    def task_output(self, task: dict) -> Path:
        return Path(task["output_dir"]) if task.get("output_dir") else self.default_export()

    def export_summary(self, root: Path | None = None) -> dict:
        root = root or self.default_export()
        countries = []
        if root.is_dir():
            for folder in sorted(p for p in root.iterdir() if (p / UNIVERSITIES_SUBDIR).is_dir()):
                count = sum(1 for _ in (folder / UNIVERSITIES_SUBDIR).glob("*.json"))
                countries.append({"country": folder.name, "json_files": count})
        return {"folder": str(root), "countries": countries,
                "json_files": sum(c["json_files"] for c in countries)}

    # --- folders ----------------------------------------------------------------------------

    def resolve_folder(self, raw: str | None) -> Path:
        text = (raw or "").strip().strip('"')
        if text:
            path = Path(os.path.expandvars(text)).expanduser()
            if not path.is_absolute():
                raise TaskError("Enter a full folder path, for example D:\\pages\\universities.")
        else:
            path = self.settings.import_folder
        path = path.resolve()
        if not path.exists():
            raise TaskError(f"Import folder not found: {path}")
        if not path.is_dir():
            raise TaskError(f"Not a folder: {path}")
        try:
            with os.scandir(path):
                pass
        except OSError as exc:
            raise TaskError(f"The app cannot read this folder: {path} ({exc.strerror or exc})") from None
        return path

    def is_default_folder(self, raw: str | None) -> bool:
        return is_default_folder(raw, self.settings.import_folder)

    def inspect_folder(self, raw: str | None) -> dict:
        folder = self.resolve_folder(raw)
        eligible, other, unreadable = [], [], []
        for path in discover(folder):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                unreadable.append(path.name)
                continue
            (eligible if looks_like_university_page(text) else other).append(path.name)
        return {"folder": str(folder), "is_default": folder == self.settings.import_folder.resolve(),
                "html_files": len(eligible) + len(other) + len(unreadable), "eligible": len(eligible),
                "other": len(other), "unreadable": unreadable, "eligible_files": eligible[:20],
                "other_files": other[:20]}

    # --- lifecycle --------------------------------------------------------------------------

    def create(self, category: str, folder: str, delete_html: bool, policy: str = DEFAULT_POLICY,
               export_folder: str | None = None) -> dict:
        if category not in CATEGORIES:
            raise TaskError("Choose a university category.")
        if policy not in POLICIES:
            raise TaskError("Choose what to do with universities that already have a JSON file.")
        with self._create_lock:
            info = self.inspect_folder(folder)
            if info["html_files"] == 0:
                raise TaskError(f"No .html files found in {info['folder']}.")
            if info["eligible"] == 0:
                raise TaskError(f"None of the {info['html_files']} HTML file(s) in {info['folder']} is a "
                                "university profile page.")
            output = resolve_export_folder(export_folder, self.default_export())
            self._check_conflicts(category, info["folder"])
            task_id = uuid.uuid4().hex[:12]
            self.store.create_task(task_id, category, info["folder"], delete_html)
            self.store.update_task(task_id, policy=policy, output_dir=str(output))
        self.store.add_event(task_id, "info", f"Task created for {CATEGORIES[category].label} university pages in "
                                              f"{info['folder']}; JSON files go to {output}",
                             {"delete_html": delete_html, "policy": policy, "output_dir": str(output)})
        self._start_thread(task_id)
        return self.snapshot(task_id)

    def _check_conflicts(self, category: str, folder: str, ignore: str | None = None) -> None:
        for task in self.store.tasks_with_status(*ACTIVE):
            if task["id"] != ignore:
                raise TaskError(f"A university import task is already {task['status']}. Wait for it to finish, "
                                "or pause/stop it first.", 409, self.snapshot(task["id"]))

    # --- views ------------------------------------------------------------------------------

    def snapshot(self, task_id: str) -> dict:
        task = self._task(task_id)
        counts = self.store.file_status_counts(task_id)
        pending = counts.get("pending", 0) + counts.get("processing", 0)
        processed = sum(counts.get(k, 0) for k in ("done", "failed", "blocked", "skipped"))
        elapsed = task["run_seconds"] + (time.monotonic() - self._run_started[task_id]
                                         if task_id in self._run_started else 0)
        total = task["files_total"]
        task.update({
            "label": CATEGORIES[task["category"]].label,
            "policy_label": POLICIES.get(task["policy"], task["policy"]),
            "output_dir": str(self.task_output(task)),
            "file_counts": counts,
            "files_processed": processed,
            "files_pending": pending,
            "imported_clean": counts.get("done", 0) - task["with_warnings"],
            "percent": round(100 * processed / total, 1) if total else (100.0 if task["scanned"] else 0.0),
            "elapsed_seconds": round(elapsed, 1),
            "eta_seconds": round(elapsed / processed * pending, 1) if processed and pending else None,
            "is_active": task["status"] in ACTIVE,
            "can_pause": task["status"] in ("running", "queued"),
            "can_resume": task["status"] in RESUMABLE,
            "can_stop": task["status"] in ("running", "queued", "pausing", "paused", "interrupted"),
            "can_retry": task["status"] not in ACTIVE and (counts.get("failed", 0) + counts.get("blocked", 0)) > 0,
            "delete_html": bool(task["delete_html"]),
        })
        return task

    def overview(self) -> dict:
        categories = {
            key: {"label": category.label, "output_dir": str(self.default_export()),
                  "count_field": category.count_field, "stats": self.store.category_stats(key)}
            for key, category in CATEGORIES.items()}
        active = [t for t in self.store.list_tasks(20) if t["status"] in ACTIVE + ("interrupted",)]
        return {"categories": categories, "category_order": list(CATEGORIES),
                "export": self.export_summary(),
                "import_folder": str(self.settings.import_folder),
                "delete_html_default": self.settings.delete_html_default,
                "policies": [{"value": key, "label": label} for key, label in POLICIES.items()],
                "default_policy": DEFAULT_POLICY,
                "current_task": self.snapshot(active[0]["id"]) if active else None}

    # --- worker -----------------------------------------------------------------------------

    def _finish(self, task_id: str) -> None:
        self._account_time(task_id)
        counts = self._refresh_counts(task_id)
        task = self.store.get_task(task_id)
        problems = counts.get("failed", 0) + counts.get("blocked", 0)
        status = "completed_with_errors" if problems else "completed"
        message = (f"Finished: {counts.get('done', 0)} universit{'y' if counts.get('done', 0) == 1 else 'ies'} "
                   f"imported ({task['with_warnings']} with missing fields), {counts.get('failed', 0)} failed, "
                   f"{counts.get('blocked', 0)} blocked, {counts.get('skipped', 0)} skipped, "
                   f"{task['json_files_written']} JSON file(s) written, {task['html_deleted']} HTML file(s) deleted.")
        self.store.update_task(task_id, status=status, stage="finished", finished_at=utcnow(), current_file=None,
                               current_university=None, file_stage=None, last_action=message)
        self.store.add_event(task_id, "warning" if problems else "info", message, counts)

    def _refresh_counts(self, task_id: str) -> dict:
        counts = self.store.file_status_counts(task_id)
        row = self.store.query_one(
            "SELECT SUM(CASE WHEN outcome = 'created' THEN 1 ELSE 0 END) AS created, "
            "SUM(CASE WHEN outcome = 'updated' THEN 1 ELSE 0 END) AS updated, "
            "SUM(CASE WHEN outcome = 'unchanged' THEN 1 ELSE 0 END) AS unchanged, "
            "SUM(CASE WHEN missing_json != '[]' THEN 1 ELSE 0 END) AS warned "
            "FROM task_files WHERE task_id = ? AND status = 'done'", (task_id,))
        created, updated = row["created"] or 0, row["updated"] or 0
        self.store.update_task(task_id, files_done=counts.get("done", 0), files_failed=counts.get("failed", 0),
                               files_blocked=counts.get("blocked", 0), files_skipped=counts.get("skipped", 0),
                               with_warnings=row["warned"] or 0, universities_created=created,
                               universities_updated=updated, universities_unchanged=row["unchanged"] or 0,
                               json_files_written=created + updated)
        return counts

    def _scan(self, task: dict) -> None:
        folder = Path(task["folder"])
        if not folder.is_dir():
            raise FileNotFoundError(f"Import folder no longer exists: {folder}")
        paths = discover(folder)
        eligible = 0
        for path in paths:
            self.store.update_task(task["id"], current_file=path.name, last_action=f"Scanning {path.name}")
            try:
                kind = KIND_UNIVERSITY if looks_like_university_page(
                    path.read_text(encoding="utf-8", errors="replace")) else KIND_OTHER
            except OSError:
                kind = None
            eligible += kind == KIND_UNIVERSITY
            self.store.add_file(task["id"], str(path), path.name, path.stat().st_size, kind)
        self.store.update_task(task["id"], scanned=1, files_total=len(paths), eligible_total=eligible,
                               current_file=None,
                               last_action=f"Found {len(paths)} HTML file(s), {eligible} university page(s).")
        self.store.add_event(task["id"], "info", f"Scanned folder: {len(paths)} HTML file(s), {eligible} "
                                                 f"university profile page(s).")

    # --- one file ---------------------------------------------------------------------------

    def _stage(self, task_id: str, stage: str) -> None:
        self.store.update_task(task_id, file_stage=stage)

    def _process(self, task: dict, item: dict) -> None:
        task_id = task["id"]
        self.store.update_file(item["id"], status="processing", attempts=item["attempts"] + 1, outcome=None,
                               started_at=utcnow())
        self.store.update_task(task_id, current_file=item["name"], current_university=None, file_stage="reading",
                               last_action=f"Processing {item['name']}")
        try:
            status, reason = self._import_file(task, item)
        except Exception as exc:
            known = isinstance(exc, (OSError, RecordError, FileProblem))
            reason = str(exc) if known else f"{type(exc).__name__}: {exc}"
            self.store.update_file(item["id"], status="failed", reason=reason, processed_at=utcnow())
            self.store.update_task(task_id, last_error=f"{item['name']}: {reason}", file_stage=None)
            self.store.add_event(task_id, "error", f"Failed: {item['name']}: {reason}. The HTML file was kept.",
                                 {"file": item["path"]})
            self._refresh_counts(task_id)
            return
        if status != "done":
            self.store.update_file(item["id"], status=status, reason=reason, processed_at=utcnow())
            self.store.add_event(task_id, "warning" if status == "blocked" else "info",
                                 f"{status.capitalize()}: {item['name']}: {reason}", {"file": item["path"]})
        self._refresh_counts(task_id)
        if status == "done" and task["delete_html"]:
            self._stage(task_id, "deleting HTML")
            self._delete_source(task_id, item)
        self._stage(task_id, None)

    def _import_file(self, task: dict, item: dict) -> tuple[str, str | None]:
        task_id = task["id"]
        category = CATEGORIES[task["category"]]
        path = Path(item["path"])
        if not path.is_file():
            raise FileProblem(f"file not found (moved or deleted?): {path}")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        self.store.update_file(item["id"], sha256=digest, size=len(data))
        duplicate = self.store.find_file_by_hash(task_id, digest, item["id"])
        if duplicate:
            return "skipped", f"identical content to {duplicate['name']}, already imported"

        self._stage(task_id, "parsing")
        html, valid_utf8 = decode_html(data)
        soup = make_soup(html)
        blocked = blocked_reason(html, soup)
        if blocked:
            return "blocked", blocked
        if soup.select_one("span.OrganisationName") is None:
            return "skipped", "not a university profile page (no university name block found)"
        page = parse_university(html, soup, category)
        self.store.update_file(item["id"], kind=KIND_UNIVERSITY, source_url=page.source_url,
                               portal=page.portal.category if page.portal else None)
        if page.portal and page.portal.category != category.key:
            other = CATEGORIES[page.portal.category]
            return "skipped", (f"this page comes from {page.portal.host}; import it with the {other.label} "
                               "category")
        if page.portal is None:
            page.warnings.append("could not confirm which portal the page was saved from")
        if not valid_utf8:
            page.warnings.append("the file is not valid UTF-8; undecodable bytes were replaced")

        self._stage(task_id, "validating")
        name = page.data["university"]
        if not name:
            raise FileProblem("university name not found on the page (it may not have finished loading "
                              "when it was saved)")
        self.store.update_task(task_id, current_university=name)
        if not page.data["country"]:
            page.warnings.append(f"country not found on the page; saved under '{country_folder(None)}'")
        problems = validate_record(page.data, category.fields)
        if problems:
            raise FileProblem("extracted data is invalid: " + "; ".join(problems))

        output = self.task_output(task)
        key, filename, existing, mapped = self._target(category, page, output)
        target = output / filename
        if existing is not None and task["policy"] == "create_only":
            return "skipped", f"{filename} already exists (policy: create new records only)"
        if task["policy"] == "skip_imported" and existing is not None and mapped \
                and mapped["last_task_id"] != task_id:
            return "skipped", (f"{name} was already imported on {mapped['imported_at']} "
                               "(policy: skip already imported records)")

        record, changed = merge(existing, page.data, category.fields)
        problems = validate_record(record, category.fields)
        if problems:
            raise FileProblem("the merged record is invalid: " + "; ".join(problems))
        outcome = "created" if existing is None else ("updated" if changed else "unchanged")
        if outcome != "unchanged":
            self._stage(task_id, "writing JSON")
            write_json_atomic(target, record)

        self._stage(task_id, "verifying JSON")
        saved = read_existing(target)
        if saved != record or validate_record(saved, category.fields):
            raise FileProblem(f"verification failed: {filename} does not contain the expected data after saving")

        self._stage(task_id, "recording")
        notes = []
        if mapped and mapped["last_task_id"] == task_id:
            notes.append(f"same university as {mapped['last_file']} in this task; records merged")
        if existing is not None and name_key(existing.get("university")) != name_key(name):
            notes.append(f"the file previously named the university '{existing.get('university')}'")
        now = utcnow()
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO universities (category, key, display_name, filename, university_id, source_url, "
                "first_task_id, last_task_id, last_file, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(category, key) DO UPDATE SET display_name = excluded.display_name, "
                "filename = excluded.filename, "
                "university_id = COALESCE(excluded.university_id, universities.university_id), "
                "source_url = COALESCE(excluded.source_url, universities.source_url), "
                "last_task_id = excluded.last_task_id, last_file = excluded.last_file, "
                "imported_at = excluded.imported_at",
                (category.key, key, name, filename, page.university_id, page.source_url, task_id, task_id,
                 item["name"], now))
            conn.execute(
                "UPDATE task_files SET status = 'done', outcome = ?, reason = ?, university = ?, university_key = ?, "
                "output_path = ?, missing_json = ?, warnings_json = ?, processed_at = ? WHERE id = ?",
                (outcome, "; ".join(notes) or None, name, key, str(target), json.dumps(page.missing),
                 json.dumps(page.warnings, ensure_ascii=False), now, item["id"]))
        verb = {"created": "created", "updated": f"updated ({', '.join(changed)})",
                "unchanged": "already up to date"}[outcome]
        message = f"Imported {item['name']}: {name} → {filename} {verb}"
        if page.missing:
            message += f"; missing: {', '.join(page.missing)}"
        if notes:
            message += f"; {'; '.join(notes)}"
        self.store.update_task(task_id, last_action=message, last_success=message)
        self.store.add_event(task_id, "warning" if page.missing else "info", message,
                             {"file": item["path"], "json": str(target), "missing": page.missing,
                              "warnings": page.warnings})
        return "done", None

    def _target(self, category: UniversityCategory, page: UniversityPage,
                output: Path) -> tuple[str, str, dict | None, dict | None]:
        """Stable (key, path relative to ``output``) for this university, plus its current record and mapping row.
        Paths look like "France/universities/ESCP Business School.json"."""
        name = page.data["university"]
        folder = f"{country_folder(page.data['country'])}/{UNIVERSITIES_SUBDIR}"
        id_key = f"id:{page.university_id}" if page.university_id else None
        by_name = f"name:{name_key(name)}"
        mapped = self.store.university(category.key, id_key) if id_key else None
        if mapped is None:
            row = self.store.university(category.key, by_name)
            if row and not (row["university_id"] and page.university_id and row["university_id"] != page.university_id):
                mapped = row
        if mapped:
            filename = mapped["filename"]
            if "/" not in filename:  # recorded before files were grouped by country
                filename = f"{folder}/{filename}"
            return mapped["key"], filename, read_existing(output / filename), mapped
        key = id_key or by_name
        base = safe_filename(name)
        stem = base.removesuffix(".json")
        for n in range(1, MAX_NAME_VARIANTS + 1):
            filename = f"{folder}/{base if n == 1 else f'{stem} ({n}).json'}"
            if self.store.university_by_filename(category.key, filename):
                continue  # this file belongs to another institution
            existing = read_existing(output / filename)
            if existing is None or name_key(existing.get("university")) == name_key(name):
                return key, filename, existing, None
        raise FileProblem(f"could not find a free file name for {name}")

    def _delete_source(self, task_id: str, item: dict) -> None:
        super()._delete_source(task_id, item)
        row = self.store.query_one("SELECT deleted FROM task_files WHERE id = ?", (item["id"],))
        if row and row["deleted"]:
            self.store.add_event(task_id, "info", f"Deleted {item['name']} after its data was saved and verified.",
                                 {"file": item["path"]})
