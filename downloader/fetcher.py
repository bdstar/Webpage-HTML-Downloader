"""Streaming HTTP download of a page's HTML response."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from urllib3.exceptions import (
    DecodeError,
    NameResolutionError,
    ProtocolError,
    ReadTimeoutError,
)

from .config import Settings
from .errors import BlockedDestinationError, CancelledError, DownloadError, InvalidURLError
from .filenames import build_filename, finalize_file
from .integrity import check_final_url, check_page_number
from .jobs import Job
from .security import build_safe_session, validate_url

HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} bytes"


def http_status_error(status: int, reason: str, retry_after: str | None = None) -> DownloadError:
    if status in (401, 407):
        msg = (f"The page requires authentication (HTTP {status}). This tool does not log in "
               "or bypass access restrictions.")
    elif status == 403:
        msg = ("Access denied (HTTP 403). The website refuses this request, possibly because it "
               "blocks automated downloads. This tool does not bypass access restrictions.")
    elif status == 404:
        msg = "Page not found (HTTP 404). Check that the URL is spelled correctly."
    elif status == 410:
        msg = "The page has been permanently removed (HTTP 410)."
    elif status == 429:
        msg = "The website is rate-limiting requests (HTTP 429). Wait a while before trying again."
        if retry_after:
            msg += f" The server asked to retry after: {retry_after}."
    elif 500 <= status < 600:
        msg = (f"The website reported a server error (HTTP {status} {reason}). "
               "It may be temporarily unavailable; try again later.")
    else:
        msg = f"The website returned HTTP {status} {reason}."
    return DownloadError(msg.strip(), code=f"http_{status}", http_status=status)


def is_bot_challenge(response: requests.Response) -> bool:
    """True if the response is Cloudflare's browser-verification ("Just a moment...") page."""
    return response.headers.get("cf-mitigated", "").strip().lower() == "challenge"


def bot_challenge_error(status: int, settings: Settings) -> DownloadError:
    msg = ("This website is protected by a Cloudflare browser check, which only a real browser "
           "can pass, so Raw HTML mode cannot download it. Try Rendered DOM mode instead.")
    if not settings.enable_browser_rendering:
        msg += (" Rendered DOM mode is currently disabled: install Playwright and restart the app "
                "with ENABLE_BROWSER_RENDERING=true (see README).")
    return DownloadError(msg, code="bot_challenge", http_status=status)


def _parse_content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        length = int(value.strip())
    except ValueError:
        return None
    return length if length >= 0 else None


def _find_cause(exc: BaseException, kind: type) -> BaseException | None:
    seen = set()
    stack = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, kind):
            return current
        stack.extend(a for a in getattr(current, "args", ()) if isinstance(a, BaseException))
        stack.append(getattr(current, "reason", None))
        stack.append(current.__cause__)
        stack.append(current.__context__)
    return None


def translate_exception(exc: BaseException, settings: Settings) -> DownloadError:
    """Map low-level networking / filesystem exceptions to user-facing errors."""
    if isinstance(exc, DownloadError):
        return exc
    blocked = _find_cause(exc, BlockedDestinationError)
    if blocked is not None:
        return blocked  # type: ignore[return-value]
    if isinstance(exc, requests.exceptions.SSLError):
        return DownloadError(
            "A secure (TLS/SSL) connection could not be established. The site's certificate "
            "may be invalid or expired.", code="tls_error")
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return DownloadError(
            f"Connecting to the website timed out after {settings.connect_timeout:g} seconds. "
            "The server may be down or unreachable.", code="connect_timeout")
    if isinstance(exc, (requests.exceptions.ReadTimeout, ReadTimeoutError)):
        return DownloadError(
            f"The website stopped sending data for more than {settings.read_timeout:g} seconds. "
            "Try again later.", code="read_timeout")
    if isinstance(exc, requests.exceptions.ConnectionError):
        if _find_cause(exc, NameResolutionError) is not None:
            return DownloadError(
                "The domain name could not be resolved (DNS lookup failed). Check the spelling "
                "of the URL and your internet connection.", code="dns_error")
        if _find_cause(exc, ProtocolError) is not None and _find_cause(exc, ConnectionResetError):
            return DownloadError("The connection was reset by the website.", code="connection_reset")
        return DownloadError(
            "Could not connect to the website. The server may be down, refusing connections, "
            "or unreachable from this machine.", code="connection_error")
    if isinstance(exc, (ProtocolError, requests.exceptions.ChunkedEncodingError)):
        return DownloadError(
            "The connection was interrupted before the page finished downloading. "
            "No partial file was kept; please try again.", code="interrupted")
    if isinstance(exc, (DecodeError, requests.exceptions.ContentDecodingError)):
        return DownloadError("The website sent compressed data that could not be decoded.",
                             code="decode_error")
    # RequestException subclasses OSError, so it must be handled before file-system errors.
    if isinstance(exc, requests.exceptions.RequestException):
        return DownloadError(f"The request failed: {exc}", code="request_error")
    if isinstance(exc, PermissionError):
        return DownloadError(
            "Permission denied while writing to the downloads folder. Check that the "
            "application can write to it.", code="filesystem_permission")
    if isinstance(exc, OSError):
        return DownloadError(f"A file-system error occurred while saving the page: {exc.strerror or exc}.",
                             code="filesystem_error")
    return DownloadError(f"Unexpected error: {exc}", code="internal_error")


def _open_response(session: requests.Session, job: Job, settings: Settings) -> tuple[requests.Response, str]:
    """Issue the request, following and re-validating each redirect manually."""
    url = job.url
    timeout = (settings.connect_timeout, settings.read_timeout)
    for hop in range(settings.max_redirects + 1):
        job.raise_if_cancelled()
        host_hint = url.split("/")[2]
        job.set_stage("connecting", f"Connecting to {host_hint}..." if hop == 0
                      else f"Following redirect to {host_hint}...")
        response = session.get(url, stream=True, allow_redirects=False, timeout=timeout)
        if not response.is_redirect:
            return response, url
        location = response.headers.get("Location", "")
        response.close()
        target = urljoin(url, location)
        try:
            url = validate_url(target, settings)
        except (InvalidURLError, BlockedDestinationError) as exc:
            raise type(exc)(f"The website redirected to a disallowed location ({target}): {exc.message}") from exc
    raise DownloadError(f"Too many redirects (more than {settings.max_redirects}).", code="too_many_redirects")


def run_http_download(job: Job, settings: Settings) -> None:
    """Download ``job.url`` and save the raw HTML response. Never raises."""
    temp_path: Path | None = None
    session = build_safe_session(settings)
    try:
        settings.download_dir.mkdir(parents=True, exist_ok=True)
        response, final_url = _open_response(session, job, settings)
        with response:
            check_final_url(job.url, final_url)
            if response.status_code >= 400 and is_bot_challenge(response):
                raise bot_challenge_error(response.status_code, settings)
            if response.status_code >= 400:
                raise http_status_error(response.status_code, response.reason or "",
                                        response.headers.get("Retry-After"))
            if response.status_code in (204, 205):
                raise DownloadError("The website returned an empty response (no content).", code="empty_response")

            content_type_header = response.headers.get("Content-Type")
            media_type = (content_type_header or "").split(";", 1)[0].strip().lower() or None
            if media_type and media_type not in HTML_CONTENT_TYPES and not settings.allow_non_html:
                raise DownloadError(
                    f"The URL returned '{media_type}' content, not an HTML page.", code="not_html")

            total = _parse_content_length(response.headers.get("Content-Length"))
            limit = settings.max_download_bytes
            if total is not None and total > limit:
                raise DownloadError(
                    f"The page is {format_bytes(total)}, which exceeds the {format_bytes(limit)} limit.",
                    code="too_large")

            encoding = (response.headers.get("Content-Encoding") or "").strip().lower()
            job.set_response_info(final_url, content_type_header, total,
                                  encoding if encoding not in ("", "identity") else None)
            job.set_stage("downloading", "Downloading HTML..." if total else
                          "Downloading HTML (total size not reported by the server)...")

            fd, name = tempfile.mkstemp(prefix=".download-", suffix=".part", dir=settings.download_dir)
            temp_path = Path(name)
            deadline = time.monotonic() + settings.total_timeout
            written = 0
            raw = response.raw
            with os.fdopen(fd, "wb") as out:
                for chunk in raw.stream(settings.chunk_size, decode_content=True):
                    job.raise_if_cancelled()
                    if chunk:
                        out.write(chunk)
                        written += len(chunk)
                    if written > limit:
                        raise DownloadError(
                            f"The page exceeds the {format_bytes(limit)} size limit; download stopped.",
                            code="too_large")
                    if time.monotonic() > deadline:
                        raise DownloadError(
                            f"The download took longer than {settings.total_timeout:g} seconds and was stopped.",
                            code="total_timeout")
                    # Progress is measured in bytes received on the wire so it
                    # matches Content-Length even for compressed responses.
                    job.set_progress(raw.tell(), total)
                job.raise_if_cancelled()
                out.flush()
                os.fsync(out.fileno())

            received = raw.tell()
            if total is not None and received < total:
                raise DownloadError(
                    f"The connection closed early: received {format_bytes(received)} of "
                    f"{format_bytes(total)}. No partial file was kept.", code="interrupted")
            if written == 0:
                raise DownloadError("The website returned an empty page (0 bytes).", code="empty_response")
        check_page_number(job.url, temp_path.read_text(encoding="utf-8", errors="replace"))

        job.set_stage("saving", "Saving file...")
        filename = build_filename(final_url)
        final_path = finalize_file(temp_path, settings.download_dir, filename)
        temp_path = None
        job.complete(final_path.name, str(final_path), final_path.stat().st_size)
    except CancelledError:
        job.mark_cancelled()
    except BaseException as exc:  # noqa: BLE001 - every failure must be reported to the job
        if job.cancel_requested:
            job.mark_cancelled()
        else:
            err = translate_exception(exc, settings)
            job.fail(err.message, err.code, err.http_status)
        if not isinstance(exc, Exception):
            raise
    finally:
        session.close()
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
