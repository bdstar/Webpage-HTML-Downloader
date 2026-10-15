"""Flask web application for the Webpage HTML Downloader.

Run with:  python app.py   (then open http://127.0.0.1:5000)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_file, stream_with_context

from downloader import (
    BlockedDestinationError,
    DuplicateJobError,
    InvalidURLError,
    JobManager,
    Settings,
    TooManyJobsError,
)
from downloader.fetcher import run_http_download
from downloader.folders import FolderError, create_folder, list_folders, validate_download_dir
from downloader.jobs import FINAL_STATUSES, MODE_HTTP, MODE_RENDERED, MODES
from downloader.renderer import playwright_installed, rendering_available, run_rendered_download
from downloader.security import validate_url
from programmes.config import ImportSettings
from programmes.tasks import ImportManager
from programmes.web import bp as programmes_bp
from universities.config import UniversitySettings
from universities.tasks import UniversityImportManager
from universities.web import bp as universities_bp

SSE_KEEPALIVE_SECONDS = 15.0
SSE_MIN_INTERVAL_SECONDS = 0.1
LOCAL_ADDRESSES = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def create_app(settings: Settings | None = None, manager: JobManager | None = None,
               import_manager: ImportManager | None = None,
               university_manager: UniversityImportManager | None = None) -> Flask:
    settings = settings or Settings.from_env()
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    manager = manager or JobManager(
        settings, {MODE_HTTP: run_http_download, MODE_RENDERED: run_rendered_download})

    app = Flask(__name__)
    app.config["SETTINGS"] = settings
    app.config["JOB_MANAGER"] = manager
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
    app.config["IMPORT_MANAGER"] = import_manager
    app.config["IMPORT_MANAGER_FACTORY"] = lambda: ImportManager(ImportSettings.from_env())
    app.config["UNIVERSITY_MANAGER"] = university_manager
    app.config["UNIVERSITY_MANAGER_FACTORY"] = lambda: UniversityImportManager(UniversitySettings.from_env())
    app.register_blueprint(programmes_bp)
    app.register_blueprint(universities_bp)

    def error_response(message: str, status: int, code: str, **extra):
        return jsonify({"error": message, "code": code, **extra}), status

    def folder_selection_allowed() -> bool:
        """Choosing/browsing folders exposes the server's file system, so only the local user may do it."""
        return settings.allow_remote_folder_selection or request.remote_addr in LOCAL_ADDRESSES

    app.config["FOLDER_SELECTION_ALLOWED"] = folder_selection_allowed

    def client_config() -> dict:
        return {
            "render_available": rendering_available(settings),
            "playwright_installed": playwright_installed(),
            "render_enabled": settings.enable_browser_rendering,
            "max_download_bytes": settings.max_download_bytes,
            "download_dir": str(settings.download_dir),
            "folder_selection": folder_selection_allowed(),
            "max_active_jobs": settings.max_active_jobs,
        }

    @app.get("/api/folders")
    def browse_folders():
        if not folder_selection_allowed():
            return error_response("Folder selection is only available on the computer running the app.",
                                  403, "folder_selection_disabled")
        try:
            return jsonify(list_folders(request.args.get("path")))
        except FolderError as exc:
            return error_response(exc.message, 400, exc.code)

    @app.post("/api/folders")
    def make_folder():
        if not folder_selection_allowed():
            return error_response("Folder selection is only available on the computer running the app.",
                                  403, "folder_selection_disabled")
        payload = request.get_json(silent=True) or {}
        try:
            path = create_folder(payload.get("parent"), payload.get("name"))
        except FolderError as exc:
            return error_response(exc.message, 400, exc.code)
        return jsonify({"path": str(path)}), 201

    @app.get("/")
    def index():
        return render_template("index.html", app_config=client_config())

    @app.get("/api/config")
    def get_config():
        return jsonify(client_config())

    @app.post("/api/jobs")
    def create_job():
        payload = request.get_json(silent=True) or {}
        mode = payload.get("mode") or MODE_HTTP
        if mode not in MODES:
            return error_response(f"Unknown mode '{mode}'.", 400, "invalid_mode")
        if mode == MODE_RENDERED and not rendering_available(settings):
            return error_response(
                "Rendered (JavaScript) mode is not available on this server. Install Playwright "
                "and set ENABLE_BROWSER_RENDERING=true.", 400, "render_unavailable")
        try:
            url = validate_url(payload.get("url", ""), settings)
        except (InvalidURLError, BlockedDestinationError) as exc:
            return error_response(exc.message, 400, exc.code)
        download_dir = None
        raw_dir = (payload.get("download_dir") or "").strip()
        if raw_dir:
            if not folder_selection_allowed():
                return error_response("Choosing a download folder is only available on the computer running "
                                      "the app.", 403, "folder_selection_disabled")
            try:
                download_dir = validate_download_dir(raw_dir)
            except FolderError as exc:
                return error_response(exc.message, 400, exc.code)
        try:
            job = manager.start(url, mode, download_dir)
        except DuplicateJobError as exc:
            return error_response(str(exc), 409, "duplicate_job", job=exc.job.snapshot())
        except TooManyJobsError as exc:
            return error_response(str(exc), 429, "too_many_jobs")
        return jsonify({"job": job.snapshot()}), 202

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str):
        snapshot = manager.snapshot(job_id)
        if snapshot is None:
            return error_response("Download job not found. It may have expired.", 404, "job_not_found")
        return jsonify({"job": snapshot})

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id: str):
        job = manager.cancel(job_id)
        if job is None:
            return error_response("Download job not found.", 404, "job_not_found")
        return jsonify({"job": job.snapshot()})

    @app.get("/api/jobs/<job_id>/events")
    def job_events(job_id: str):
        if manager.snapshot(job_id) is None:
            return error_response("Download job not found.", 404, "job_not_found")

        def stream():
            last_version = -1
            yield "retry: 2000\n\n"
            while True:
                snapshot = manager.wait_for_change(job_id, last_version, SSE_KEEPALIVE_SECONDS)
                if snapshot is None:
                    yield "event: gone\ndata: {}\n\n"
                    return
                if snapshot["version"] == last_version:
                    # Keep-alive comment; a failed write here detects a closed browser tab.
                    yield ": keep-alive\n\n"
                    continue
                last_version = snapshot["version"]
                yield f"event: progress\ndata: {json.dumps(snapshot)}\n\n"
                if snapshot["status"] in FINAL_STATUSES:
                    return
                time.sleep(SSE_MIN_INTERVAL_SECONDS)

        return Response(
            stream_with_context(stream()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/jobs/<job_id>/file")
    def download_file(job_id: str):
        job = manager.get(job_id)
        if job is None:
            abort(404)
        if job.status != "completed" or not job.file_path:
            return error_response("The file is not ready yet.", 409, "not_ready")
        path = Path(job.file_path).resolve()
        expected_dir = Path(job.download_dir or settings.download_dir).resolve()
        if path.parent != expected_dir or not path.is_file():
            return error_response("The saved file no longer exists.", 410, "file_missing")
        return send_file(path, mimetype="text/html", as_attachment=True, download_name=job.filename)

    return app


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    create_app().run(host=host, port=port, threaded=True, debug=False)
