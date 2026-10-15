"""Flask routes for the university importer."""

from __future__ import annotations

import threading
from typing import Callable

from flask import Blueprint, current_app, jsonify, render_template, request

from programmes.tasks import TaskError, export_folder_info
from programmes.web import check_folder_access, folder_selection_allowed, log_export

from .categories import CATEGORIES, UNIVERSITIES_SUBDIR
from .tasks import UniversityImportManager

bp = Blueprint("universities", __name__)
_lock = threading.Lock()


def get_manager() -> UniversityImportManager:
    with _lock:
        manager = current_app.config.get("UNIVERSITY_MANAGER")
        if manager is None:
            factory: Callable[[], UniversityImportManager] = current_app.config["UNIVERSITY_MANAGER_FACTORY"]
            manager = current_app.config["UNIVERSITY_MANAGER"] = factory()
        return manager


@bp.errorhandler(TaskError)
def _task_error(exc: TaskError):
    return jsonify({"error": exc.message, **({"task": exc.task} if exc.task else {})}), exc.http_status


@bp.get("/universities")
def dashboard():
    return render_template("universities.html", folder_selection=folder_selection_allowed())


@bp.get("/api/universities/overview")
def overview():
    return jsonify({**get_manager().overview(), "folder_selection": folder_selection_allowed()})


@bp.get("/api/universities/folder")
def inspect_folder():
    folder = request.args.get("path", "")
    check_folder_access(folder, get_manager().settings.import_folder)
    return jsonify(get_manager().inspect_folder(folder))


@bp.get("/api/universities/<category>/export-folder")
def export_folder(category: str):
    if category not in CATEGORIES:
        raise TaskError("Unknown category; use 'master' or 'bachelor'.", 404)
    default = get_manager().default_export()
    path = request.args.get("path", "")
    check_folder_access(path, default)
    return jsonify(export_folder_info(path, default, pattern=f"*/{UNIVERSITIES_SUBDIR}/*.json"))


@bp.post("/api/universities/tasks")
def create_task():
    payload = request.get_json(silent=True) or {}
    manager = get_manager()
    category = payload.get("category", "")
    folder = payload.get("folder", "")
    export = payload.get("export_folder") or ""
    check_folder_access(folder, manager.settings.import_folder)
    check_folder_access(export, manager.default_export())
    delete_html = payload.get("delete_html")
    task = manager.create(category, folder,
                          manager.settings.delete_html_default if delete_html is None else bool(delete_html),
                          payload.get("policy") or "update", export)
    return jsonify({"task": task}), 202


@bp.get("/api/universities/tasks")
def list_tasks():
    manager = get_manager()
    limit = min(max(request.args.get("limit", 50, type=int), 1), 500)
    return jsonify({"tasks": [manager.snapshot(t["id"]) for t in manager.store.list_tasks(limit)]})


@bp.get("/api/universities/tasks/<task_id>")
def get_task(task_id: str):
    return jsonify({"task": get_manager().snapshot(task_id)})


@bp.post("/api/universities/tasks/<task_id>/<action>")
def control_task(task_id: str, action: str):
    manager = get_manager()
    handlers = {"pause": manager.pause, "resume": manager.resume, "stop": manager.stop,
                "retry": manager.retry_failed}
    if action not in handlers:
        return jsonify({"error": "Unknown action."}), 404
    return jsonify({"task": handlers[action](task_id)})


@bp.get("/api/universities/tasks/<task_id>/files")
def task_files(task_id: str):
    manager = get_manager()
    manager.snapshot(task_id)
    return jsonify({"files": manager.store.files(task_id, request.args.get("status") or None)})


@bp.get("/api/universities/tasks/<task_id>/events")
def task_events(task_id: str):
    manager = get_manager()
    manager.snapshot(task_id)
    return jsonify({"events": manager.store.events(task_id, request.args.get("after", 0, type=int), 500)})


@bp.get("/api/universities/tasks/<task_id>/log")
def export_log(task_id: str):
    return log_export(get_manager(), task_id, "university-import")
