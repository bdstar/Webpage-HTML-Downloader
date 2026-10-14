"""Core download logic for the Webpage HTML Downloader."""

from .config import Settings
from .errors import (
    BlockedDestinationError,
    CancelledError,
    DownloadError,
    DuplicateJobError,
    InvalidURLError,
    TooManyJobsError,
)
from .jobs import Job, JobManager

__all__ = [
    "BlockedDestinationError",
    "CancelledError",
    "DownloadError",
    "DuplicateJobError",
    "InvalidURLError",
    "Job",
    "JobManager",
    "Settings",
    "TooManyJobsError",
]
