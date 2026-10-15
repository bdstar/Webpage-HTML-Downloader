"""Optional JavaScript-rendered DOM capture using Playwright (Chromium)."""

from __future__ import annotations

import importlib.util
import os
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .config import Settings
from .errors import BlockedDestinationError, CancelledError, DownloadError, InvalidURLError
from .fetcher import format_bytes, http_status_error, translate_exception
from .filenames import build_filename, finalize_file
from .integrity import check_final_url, check_page_number
from .jobs import Job
from .security import resolve_and_check, validate_url

NON_NETWORK_SCHEMES = {"data", "blob", "about"}


def playwright_installed() -> bool:
    return importlib.util.find_spec("playwright") is not None


def rendering_available(settings: Settings) -> bool:
    return settings.enable_browser_rendering and playwright_installed()


class RequestGuard:
    """Checks every URL the browser tries to load against the SSRF policy."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._host_cache: dict[tuple[str, int], str | None] = {}
        self.violations: list[str] = []

    def check(self, url: str) -> str | None:
        """Return ``None`` if allowed, otherwise the reason it is blocked."""
        scheme = urlsplit(url).scheme.lower()
        if scheme in NON_NETWORK_SCHEMES:
            return None
        try:
            normalized = validate_url(url, self.settings)
        except (InvalidURLError, BlockedDestinationError) as exc:
            return exc.message
        parts = urlsplit(normalized)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        key = (parts.hostname or "", port)
        if key not in self._host_cache:
            try:
                resolve_and_check(key[0], port, self.settings)
                self._host_cache[key] = None
            except BlockedDestinationError as exc:
                self._host_cache[key] = exc.message
            except socket.gaierror:
                self._host_cache[key] = None  # the browser will fail to resolve it too
        return self._host_cache[key]

    def handle_route(self, route, request) -> None:
        reason = self.check(request.url)
        if reason is None:
            route.continue_()
        else:
            self.violations.append(f"{request.url}: {reason}")
            route.abort("blockedbyclient")


def run_rendered_download(job: Job, settings: Settings) -> None:
    """Render ``job.url`` in headless Chromium and save the resulting DOM HTML."""
    temp_path: Path | None = None
    try:
        if not rendering_available(settings):
            raise DownloadError(
                "Rendered mode is not enabled. Install Playwright and set "
                "ENABLE_BROWSER_RENDERING=true (see README).", code="render_unavailable")
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright

        settings.download_dir.mkdir(parents=True, exist_ok=True)
        guard = RequestGuard(settings)
        job.set_stage("rendering", "Launching headless browser...")
        timeout_ms = settings.render_timeout * 1000
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    service_workers="block", accept_downloads=False, user_agent=settings.user_agent)
                context.route("**/*", guard.handle_route)
                page = context.new_page()
                job.raise_if_cancelled()
                job.set_stage("connecting", "Loading page in headless browser...")
                try:
                    response = page.goto(job.url, wait_until="domcontentloaded", timeout=timeout_ms)
                except PlaywrightTimeout as exc:
                    raise DownloadError(
                        f"The page did not load within {settings.render_timeout:g} seconds.",
                        code="render_timeout") from exc
                except PlaywrightError as exc:
                    if guard.violations:
                        raise BlockedDestinationError(
                            "The page tried to load a disallowed address: " + guard.violations[0]) from exc
                    raise DownloadError(f"The browser could not load the page: {exc.message}",
                                        code="render_error") from exc
                if response is None:
                    raise DownloadError("The browser did not receive a response for the page.", code="render_error")

                # Re-check every hop of the main document's redirect chain.
                request = response.request
                while request is not None:
                    reason = guard.check(request.url)
                    if reason:
                        raise BlockedDestinationError(
                            f"The page redirected to a disallowed location ({request.url}): {reason}")
                    request = request.redirected_from
                if response.status >= 400:
                    raise http_status_error(response.status, response.status_text or "",
                                            response.headers.get("retry-after"))

                job.set_response_info(page.url, response.headers.get("content-type"), None)
                job.raise_if_cancelled()
                job.set_stage("rendering", "Waiting for the page to finish loading (network idle)...")
                try:
                    page.wait_for_load_state("networkidle", timeout=settings.render_idle_timeout * 1000)
                except PlaywrightTimeout:
                    pass  # long-polling pages never go idle; capture what has rendered
                job.raise_if_cancelled()
                html = page.content()
                final_url = page.url
            finally:
                browser.close()

        data = html.encode("utf-8")
        if len(data) > settings.max_download_bytes:
            raise DownloadError(
                f"The rendered page is {format_bytes(len(data))}, which exceeds the "
                f"{format_bytes(settings.max_download_bytes)} limit.", code="too_large")
        if not html.strip():
            raise DownloadError("The rendered page is empty.", code="empty_response")
        check_final_url(job.url, final_url)
        check_page_number(job.url, html)
        job.set_progress(len(data), None)

        job.set_stage("saving", "Saving rendered HTML...")
        fd, name = tempfile.mkstemp(prefix=".download-", suffix=".part", dir=settings.download_dir)
        temp_path = Path(name)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        job.raise_if_cancelled()
        final_path = finalize_file(temp_path, settings.download_dir, build_filename(final_url, variant="rendered"))
        temp_path = None
        job.complete(final_path.name, str(final_path), final_path.stat().st_size)
    except CancelledError:
        job.mark_cancelled()
    except Exception as exc:  # noqa: BLE001
        if job.cancel_requested:
            job.mark_cancelled()
        else:
            err = translate_exception(exc, settings)
            job.fail(err.message, err.code, err.http_status)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
