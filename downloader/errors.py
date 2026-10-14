"""Exceptions carrying user-facing, actionable messages."""

from __future__ import annotations


class DownloadError(Exception):
    """Base error whose message is safe and useful to show to the user."""

    code = "download_failed"

    def __init__(self, message: str, *, code: str | None = None, http_status: int | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.http_status = http_status


class InvalidURLError(DownloadError):
    code = "invalid_url"


class BlockedDestinationError(DownloadError):
    code = "blocked_destination"


class CancelledError(DownloadError):
    code = "cancelled"

    def __init__(self, message: str = "The download was cancelled."):
        super().__init__(message)


class DuplicateJobError(Exception):
    def __init__(self, job):
        super().__init__("A download for this URL is already in progress.")
        self.job = job


class TooManyJobsError(Exception):
    pass
