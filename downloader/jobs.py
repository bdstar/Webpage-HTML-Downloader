"""Background download jobs and their observable progress state."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .config import Settings
from .errors import CancelledError, DuplicateJobError, TooManyJobsError

MODE_HTTP = "http"
MODE_RENDERED = "rendered"
MODES = {MODE_HTTP, MODE_RENDERED}

ACTIVE_STATUSES = {"running"}
FINAL_STATUSES = {"completed", "failed", "cancelled"}

STAGE_LABELS = {
    "initializing": "Initializing",
    "connecting": "Connecting",
    "rendering": "Rendering page",
    "downloading": "Downloading HTML",
    "saving": "Saving file",
    "completed": "Completed",
    "failed": "Failed",
    "cancelled": "Cancelled",
}

# Percent shown while bytes are still arriving; 100 is reserved for "saved".
MAX_IN_PROGRESS_PERCENT = 99.0


def calculate_percent(downloaded: int, total: int | None) -> float | None:
    """Return download percentage, or ``None`` when the total size is unknown.

    The value never reaches 100 here: a job only shows 100% once the file has
    been written to its final location.
    """
    if not total or total <= 0:
        return None
    percent = downloaded * 100.0 / total
    return round(max(0.0, min(percent, MAX_IN_PROGRESS_PERCENT)), 1)


class Job:
    def __init__(self, url: str, mode: str, cond: threading.Condition | None = None,
                 download_dir: Path | None = None):
        self.id = uuid.uuid4().hex
        self.url = url
        self.mode = mode
        self.download_dir = download_dir
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.status = "running"
        self.stage = "initializing"
        self.message = "Preparing download..."
        self.bytes_downloaded = 0
        self.total_bytes: int | None = None
        self.percent: float | None = None
        self.final_url: str | None = None
        self.content_type: str | None = None
        self.content_encoding: str | None = None
        self.filename: str | None = None
        self.file_size: int | None = None
        self.file_path: str | None = None
        self.error: str | None = None
        self.error_code: str | None = None
        self.http_status: int | None = None
        self.version = 0
        self._cancel = threading.Event()
        self._cond = cond or threading.Condition(threading.RLock())

    @property
    def key(self) -> tuple[str, str]:
        return (self.url, self.mode)

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def request_cancel(self) -> None:
        self._cancel.set()
        self._update(message="Cancelling...")

    def raise_if_cancelled(self) -> None:
        if self._cancel.is_set():
            raise CancelledError()

    def _update(self, **fields) -> None:
        with self._cond:
            for name, value in fields.items():
                setattr(self, name, value)
            self.version += 1
            self._cond.notify_all()

    def set_stage(self, stage: str, message: str) -> None:
        self._update(stage=stage, message=message)

    def set_response_info(self, final_url: str, content_type: str | None, total: int | None,
                          content_encoding: str | None = None) -> None:
        self._update(
            final_url=final_url,
            content_type=content_type,
            content_encoding=content_encoding,
            total_bytes=total,
            percent=calculate_percent(self.bytes_downloaded, total),
        )

    def set_progress(self, downloaded: int, total: int | None) -> None:
        self._update(
            bytes_downloaded=downloaded,
            total_bytes=total,
            percent=calculate_percent(downloaded, total),
        )

    def complete(self, filename: str, path: str, size: int) -> None:
        self._update(
            filename=filename,
            file_path=path,
            file_size=size,
            status="completed",
            stage="completed",
            percent=100.0,
            message="Download completed and saved.",
            finished_at=time.time(),
        )

    def fail(self, message: str, code: str = "download_failed", http_status: int | None = None) -> None:
        self._update(
            status="failed",
            stage="failed",
            error=message,
            error_code=code,
            http_status=http_status,
            message="Download failed.",
            finished_at=time.time(),
        )

    def mark_cancelled(self) -> None:
        self._update(
            status="cancelled",
            stage="cancelled",
            message="Download cancelled. No file was saved.",
            finished_at=time.time(),
        )

    def snapshot(self) -> dict:
        with self._cond:
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "mode": self.mode,
            "status": self.status,
            "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage, self.stage),
            "message": self.message,
            "bytes_downloaded": self.bytes_downloaded,
            "total_bytes": self.total_bytes,
            "percent": self.percent,
            "final_url": self.final_url,
            "content_type": self.content_type,
            "content_encoding": self.content_encoding,
            "filename": self.filename,
            "file_size": self.file_size,
            "file_path": self.file_path,
            "download_dir": str(self.download_dir) if self.download_dir else None,
            "error": self.error,
            "error_code": self.error_code,
            "http_status": self.http_status,
            "cancel_requested": self.cancel_requested,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "version": self.version,
        }


Runner = Callable[[Job, Settings], None]


class JobManager:
    """Thread-safe registry of jobs with change notification for SSE streams."""

    def __init__(self, settings: Settings, runners: dict[str, Runner]):
        self.settings = settings
        self.runners = runners
        self._jobs: dict[str, Job] = {}
        self._cond = threading.Condition(threading.RLock())

    def start(self, url: str, mode: str, download_dir: Path | None = None) -> Job:
        with self._cond:
            for job in self._jobs.values():
                if job.is_active and job.key == (url, mode):
                    raise DuplicateJobError(job)
            active = sum(1 for j in self._jobs.values() if j.is_active)
            if active >= self.settings.max_active_jobs:
                raise TooManyJobsError(
                    f"Too many downloads are running ({active}). Wait for one to finish and try again."
                )
            job = Job(url, mode, self._cond, download_dir or self.settings.download_dir)
            self._jobs[job.id] = job
            self._prune()
        thread = threading.Thread(target=self._run, args=(job,), name=f"job-{job.id[:8]}", daemon=True)
        thread.start()
        return job

    def _run(self, job: Job) -> None:
        runner = self.runners[job.mode]
        settings = self.settings
        if job.download_dir and job.download_dir != settings.download_dir:
            settings = replace(settings, download_dir=job.download_dir)
        try:
            runner(job, settings)
        except Exception as exc:  # runners report expected errors themselves
            if job.is_active:
                job.fail(f"Unexpected error: {exc}", "internal_error")

    def _prune(self) -> None:
        finished = [j for j in self._jobs.values() if not j.is_active]
        excess = len(self._jobs) - self.settings.max_retained_jobs
        for job in sorted(finished, key=lambda j: j.created_at)[: max(0, excess)]:
            del self._jobs[job.id]

    def get(self, job_id: str) -> Job | None:
        with self._cond:
            return self._jobs.get(job_id)

    def snapshot(self, job_id: str) -> dict | None:
        with self._cond:
            job = self._jobs.get(job_id)
            return job.snapshot() if job else None

    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job and job.is_active:
            job.request_cancel()
        return job

    def wait_for_change(self, job_id: str, last_version: int, timeout: float) -> dict | None:
        """Block until the job's version differs from ``last_version`` or timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                job = self._jobs.get(job_id)
                if job is None:
                    return None
                if job.version != last_version:
                    return job.snapshot()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return job.snapshot()
                self._cond.wait(remaining)
