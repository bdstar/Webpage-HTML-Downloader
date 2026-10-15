"""Flask routes for the programme import dashboard."""

from __future__ import annotations

import csv
import io
import json
import threading
from typing import Callable

from flask import Blueprint, Response, current_app, jsonify, render_template, request

from .pagination import PaginationError, page_url, validate_listing_url
from .sources import PORTALS
from .tasks import ImportManager, TaskError, export_folder_info, is_default_folder

bp = Blueprint("programmes", __name__)
_lock = threading.Lock()
LOCAL_ADDRESSES = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def folder_selection_allowed() -> bool:
    allowed = current_app.config.get("FOLDER_SELECTION_ALLOWED")
    return allowed() if allowed else request.remote_addr in LOCAL_ADDRESSES


def check_folder_access(raw: str | None, default) -> None:
    """Folders other than the default expose the server's disks, so only the local user may choose one."""
    if not is_default_folder(raw, default) and not folder_selection_allowed():
        raise TaskError("Choosing another folder is only available on the computer running the app.", 403)


def get_manager() -> ImportManager:
    with _lock:
        manager = current_app.config.get("IMPORT_MANAGER")
        if manager is None:
            factory: Callable[[], ImportManager] = current_app.config["IMPORT_MANAGER_FACTORY"]
            manager = current_app.config["IMPORT_MANAGER"] = factory()
        return manager


def _error(message: str, status: int, **extra):
    return jsonify({"error": message, **extra}), status


@bp.errorhandler(TaskError)
def _task_error(exc: TaskError):
    return _error(exc.message, exc.http_status, **({"task": exc.task} if exc.task else {}))


def _category(category: str) -> str:
    if category not in PORTALS:
        raise TaskError("Unknown category; use 'master' or 'bachelor'.", 404)
    return category


def _attachment(body: str, filename: str, mimetype: str) -> Response:
    return Response(body, mimetype=mimetype,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@bp.get("/programmes")
def dashboard():
    return render_template("programmes.html", folder_selection=folder_selection_allowed())


@bp.get("/api/programmes/overview")
def overview():
    return jsonify({**get_manager().overview(), "folder_selection": folder_selection_allowed()})


@bp.get("/api/programmes/<category>/export-folder")
def export_folder(category: str):
    default = get_manager().dataset.output_dir(_category(category))
    path = request.args.get("path", "")
    check_folder_access(path, default)
    return jsonify(export_folder_info(path, default))


@bp.post("/api/programmes/tasks")
def create_task():
    payload = request.get_json(silent=True) or {}
    manager = get_manager()
    category = payload.get("category", "")
    export = payload.get("export_folder") or ""
    if category in PORTALS:
        check_folder_access(export, manager.dataset.output_dir(category))
    delete_html = payload.get("delete_html")
    task = manager.create(category, payload.get("folder", ""),
                          manager.settings.delete_html_default if delete_html is None else bool(delete_html),
                          export)
    return jsonify({"task": task}), 202


@bp.get("/api/programmes/tasks")
def list_tasks():
    manager = get_manager()
    limit = min(max(request.args.get("limit", 50, type=int), 1), 500)
    category = request.args.get("category") or None
    tasks = [manager.snapshot(t["id"]) for t in manager.store.list_tasks(limit, category)]
    return jsonify({"tasks": tasks})


@bp.get("/api/programmes/tasks/<task_id>")
def get_task(task_id: str):
    return jsonify({"task": get_manager().snapshot(task_id)})


@bp.post("/api/programmes/tasks/<task_id>/<action>")
def control_task(task_id: str, action: str):
    manager = get_manager()
    handlers = {"pause": manager.pause, "resume": manager.resume, "stop": manager.stop,
                "retry": manager.retry_failed}
    if action not in handlers:
        return _error("Unknown action.", 404)
    return jsonify({"task": handlers[action](task_id)})


@bp.get("/api/programmes/tasks/<task_id>/files")
def task_files(task_id: str):
    manager = get_manager()
    manager.snapshot(task_id)  # 404 if missing
    return jsonify({"files": manager.store.files(task_id, request.args.get("status") or None)})


@bp.get("/api/programmes/tasks/<task_id>/events")
def task_events(task_id: str):
    manager = get_manager()
    manager.snapshot(task_id)
    after = request.args.get("after", 0, type=int)
    return jsonify({"events": manager.store.events(task_id, after, 500)})


def log_export(manager, task_id: str, prefix: str) -> Response:
    """A task's full history as a JSON or CSV download (``?format=csv``)."""
    task = manager.snapshot(task_id)
    events = manager.store.events(task_id, 0, 1_000_000)
    files = manager.store.files(task_id)
    if request.args.get("format") == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["time", "level", "message", "file"])
        for event in events:
            writer.writerow([event["ts"], event["level"], event["message"], (event["data"] or {}).get("file", "")])
        return _attachment(buffer.getvalue(), f"{prefix}-{task_id}-log.csv", "text/csv")
    body = json.dumps({"task": task, "files": files, "events": events}, ensure_ascii=False, indent=2)
    return _attachment(body, f"{prefix}-{task_id}-log.json", "application/json")


@bp.get("/api/programmes/tasks/<task_id>/log")
def export_log(task_id: str):
    return log_export(get_manager(), task_id, "import")


@bp.get("/api/programmes/<category>/missing-details")
def missing_details(category: str):
    manager = get_manager()
    rows = manager.store.missing_details(_category(category))
    if request.args.get("format") == "txt":
        return _attachment("\n".join(r["program_url"] for r in rows) + ("\n" if rows else ""),
                           f"{category}-programme-pages-to-save.txt", "text/plain")
    limit = min(max(request.args.get("limit", 50, type=int), 1), 1000)
    return jsonify({"count": len(rows), "programmes": rows[:limit]})


@bp.get("/api/programmes/<category>/listing-pages")
def listing_pages(category: str):
    """Listing page URLs (``?page=N``) not yet imported, for the user to open and save."""
    manager = get_manager()
    portal = PORTALS[_category(category)]
    stats = manager.store.category_stats(category)
    try:
        base = validate_listing_url(request.args.get("base_url") or portal.base_listing_url, portal)
        max_page = request.args.get("max_page", type=int) or stats["listing_pages_total"] or 1
        if max_page < 1 or max_page > 100_000:
            raise PaginationError("Maximum pagination must be between 1 and 100000.")
    except PaginationError as exc:
        return _error(str(exc), 400)
    imported = manager.store.imported_pages(category)
    missing = [n for n in range(1, max_page + 1) if n not in imported]
    if request.args.get("format") == "txt":
        return _attachment("\n".join(page_url(base, n) for n in missing) + ("\n" if missing else ""),
                           f"{category}-listing-pages-to-save.txt", "text/plain")
    limit = min(max(request.args.get("limit", 20, type=int), 1), 1000)
    return jsonify({"base_url": base, "max_page": max_page, "imported": len(imported & set(range(1, max_page + 1))),
                    "missing_count": len(missing),
                    "pages": [{"page": n, "url": page_url(base, n)} for n in missing[:limit]]})


@bp.post("/api/programmes/<category>/rebuild")
def rebuild(category: str):
    written = get_manager().dataset.rebuild_all(_category(category))
    return jsonify({"json_files_written": written})
