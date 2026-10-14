import dataclasses
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import pytest

from conftest import PAGE
from downloader.config import Settings
from downloader.fetcher import run_http_download
from downloader.filenames import build_filename, finalize_file, slugify_url
from downloader.jobs import Job


class RecordingJob(Job):
    """Job that records every progress update for assertions."""

    def __init__(self, url, cancel_after_bytes=None):
        super().__init__(url, "http")
        self.history = []
        self.cancel_after_bytes = cancel_after_bytes

    def set_progress(self, downloaded, total):
        super().set_progress(downloaded, total)
        self.history.append((downloaded, total, self.percent, self.stage))
        if self.cancel_after_bytes is not None and downloaded >= self.cancel_after_bytes:
            self.request_cancel()


def download(url, settings, **kwargs):
    job = RecordingJob(url, **kwargs)
    run_http_download(job, settings)
    return job


def saved_files(settings):
    return sorted(p.name for p in settings.download_dir.iterdir()) if settings.download_dir.exists() else []


def assert_no_files(settings):
    assert saved_files(settings) == [], "failed or cancelled downloads must not leave files behind"


# --- successful downloads ------------------------------------------------------------------

def test_downloads_and_saves_html(server, settings):
    job = download(server + "/page", settings)
    assert job.status == "completed", job.error
    path = Path(job.file_path)
    assert path.parent == settings.download_dir
    assert path.read_bytes() == PAGE
    assert job.file_size == len(PAGE)
    assert job.percent == 100.0
    assert job.filename.startswith("127.0.0.1_page_") and job.filename.endswith(".html")
    assert saved_files(settings) == [job.filename]


def test_progress_uses_content_length(server, settings):
    job = download(server + "/page", settings)
    assert job.history, "progress should be reported while downloading"
    totals = {total for _, total, _, _ in job.history}
    assert totals == {len(PAGE)}
    downloaded = [d for d, _, _, _ in job.history]
    assert downloaded == sorted(downloaded)
    assert downloaded[-1] == len(PAGE)
    percents = [p for _, _, p, _ in job.history]
    assert all(0 <= p <= 99 for p in percents), "100% is reserved for after the file is saved"
    assert len(job.history) > 5


def test_unknown_length_reports_no_percentage(server, settings):
    job = download(server + "/unknown-length", settings)
    assert job.status == "completed", job.error
    assert job.history
    assert all(total is None and percent is None for _, total, percent, _ in job.history)
    assert Path(job.file_path).read_bytes() == PAGE
    assert job.percent == 100.0


def test_gzip_response_is_decoded_and_progress_tracks_wire_bytes(server, settings):
    job = download(server + "/gzip", settings)
    assert job.status == "completed", job.error
    assert Path(job.file_path).read_bytes() == PAGE
    last_downloaded, total, _, _ = job.history[-1]
    assert last_downloaded == total < len(PAGE)


def test_follows_safe_redirect(server, settings):
    job = download(server + "/redirect-ok", settings)
    assert job.status == "completed", job.error
    assert job.final_url == server + "/page"


# --- error handling ----------------------------------------------------------------------

@pytest.mark.parametrize("path, code, fragment", [
    ("/404", "http_404", "not found"),
    ("/403", "http_403", "Access denied"),
    ("/429", "http_429", "rate-limiting"),
    ("/500", "http_500", "server error"),
])
def test_http_errors(server, settings, path, code, fragment):
    job = download(server + path, settings)
    assert job.status == "failed"
    assert job.error_code == code
    assert fragment.lower() in job.error.lower()
    assert_no_files(settings)


def test_429_includes_retry_after(server, settings):
    job = download(server + "/429", settings)
    assert "120" in job.error


@pytest.mark.parametrize("path", ["/empty", "/no-content"])
def test_empty_response(server, settings, path):
    job = download(server + path, settings)
    assert job.status == "failed"
    assert job.error_code == "empty_response"
    assert_no_files(settings)


def test_non_html_rejected(server, settings):
    job = download(server + "/pdf", settings)
    assert job.status == "failed" and job.error_code == "not_html"
    assert "application/pdf" in job.error


def test_size_limit_from_content_length(server, settings):
    job = download(server + "/big", settings)
    assert job.status == "failed" and job.error_code == "too_large"
    assert job.history == [], "should fail before streaming when Content-Length is too big"
    assert_no_files(settings)


def test_size_limit_while_streaming_unknown_length(server, settings):
    job = download(server + "/big-unknown", settings)
    assert job.status == "failed" and job.error_code == "too_large"
    assert_no_files(settings)


def test_truncated_download_is_not_saved(server, settings):
    job = download(server + "/truncated", settings)
    assert job.status == "failed"
    assert job.error_code == "interrupted"
    assert_no_files(settings)


def test_read_timeout(server, settings):
    job = download(server + "/hang", settings)
    assert job.status == "failed"
    assert job.error_code == "read_timeout"
    assert_no_files(settings)


def test_connection_refused(settings):
    # Windows retries refused connections for ~2s, so allow more than that.
    settings = dataclasses.replace(settings, connect_timeout=6)
    with socket_closed_port() as port:
        job = download(f"http://127.0.0.1:{port}/", settings)
    assert job.status == "failed"
    assert job.error_code == "connection_error"


def test_dns_failure(settings, monkeypatch):
    import socket

    def fail(*args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    job = download("http://does-not-exist.invalid/", settings)
    assert job.status == "failed"
    assert job.error_code == "dns_error"


@pytest.mark.parametrize("path, fragment", [
    ("/redirect-private", "10.0.0.5"),
    ("/redirect-metadata", "169.254.169.254"),
    ("/redirect-file", "scheme"),
])
def test_redirect_to_prohibited_destination_blocked(server, settings, path, fragment):
    job = download(server + path, settings)
    assert job.status == "failed"
    assert job.error_code in {"blocked_destination", "invalid_url"}
    assert fragment in job.error
    assert_no_files(settings)


def test_redirect_loop(server, settings):
    job = download(server + "/redirect-loop", settings)
    assert job.status == "failed" and job.error_code == "too_many_redirects"


def test_loopback_blocked_without_allowlist(server, tmp_path):
    job = download(server + "/page", Settings(download_dir=tmp_path / "d", allowed_ports=None))
    assert job.status == "failed"
    assert job.error_code == "blocked_destination"


def test_permission_error_reported(server, settings, monkeypatch):
    def deny(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(tempfile, "mkstemp", deny)
    job = download(server + "/page", settings)
    assert job.status == "failed"
    assert job.error_code == "filesystem_permission"
    assert "Permission denied" in job.error


def test_cancellation_mid_download(server, settings):
    job = download(server + "/slow-chunks", settings, cancel_after_bytes=8192)
    assert job.status == "cancelled"
    assert job.filename is None
    assert_no_files(settings)


def test_cancel_before_start(server, settings):
    job = RecordingJob(server + "/page")
    job.request_cancel()
    run_http_download(job, settings)
    assert job.status == "cancelled"
    assert_no_files(settings)


# --- filenames ---------------------------------------------------------------------------

def test_filename_is_safe_and_meaningful():
    name = build_filename("https://www.Example.com/blog/My Post!.html?x=1",
                          now=datetime(2026, 10, 14, 17, 45, 0), token="abc123")
    assert name == "example.com_blog_My-Post_20261014-174500_abc123.html"


@pytest.mark.parametrize("url", [
    "https://example.com/../../etc/passwd",
    "https://example.com/%2e%2e%2f%2e%2e%2fwindows",
    "https://example.com/a\\b:c*d?e",
    "https://example.com/" + "a" * 500,
])
def test_filename_never_contains_path_components(url):
    slug = slugify_url(url)
    assert "/" not in slug and "\\" not in slug and ".." not in slug and ":" not in slug
    assert len(slug) <= 80


def test_unique_suffix_differs_between_calls():
    assert build_filename("https://example.com/") != build_filename("https://example.com/")


def test_finalize_does_not_overwrite(tmp_path):
    (tmp_path / "page.html").write_text("original")
    for content in ("second", "third"):
        tmp = tmp_path / f".{content}.part"
        tmp.write_text(content)
        finalize_file(tmp, tmp_path, "page.html")
    assert (tmp_path / "page.html").read_text() == "original"
    assert (tmp_path / "page-1.html").read_text() == "second"
    assert (tmp_path / "page-2.html").read_text() == "third"
    assert not list(tmp_path.glob("*.part"))


def test_concurrent_finalize_produces_distinct_files(tmp_path):
    temps = []
    for i in range(10):
        p = tmp_path / f".{i}.part"
        p.write_text(str(i))
        temps.append(p)
    threads = [threading.Thread(target=finalize_file, args=(p, tmp_path, "same.html")) for p in temps]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    finals = sorted(tmp_path.glob("same*.html"))
    assert len(finals) == 10
    assert sorted(f.read_text() for f in finals) == sorted(str(i) for i in range(10))


class socket_closed_port:
    """Context manager yielding a localhost port with nothing listening on it."""

    def __enter__(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        return self.port

    def __exit__(self, *exc):
        return False
