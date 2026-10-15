"""Detect when a website served different content from the URL that was requested.

Some sites (e.g. Mastersportal/Bachelorsportal) redirect automated clients to a
restricted view and drop query parameters such as ``?page=5``. Saving that
response under the requested URL would silently give the user the wrong page,
so the download is failed with an explanation instead.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

from .errors import DownloadError

PAGE_PARAMS = ("page", "p", "pg", "pagenum", "page_number", "start", "offset")
IGNORED_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|msclkid|ref|_ga)$", re.IGNORECASE)
CURRENT_PAGE_MARKER = re.compile(r'data-current-page="(\d+)"')
STUDYPORTALS_HOSTS = ("mastersportal.com", "bachelorsportal.com", "phdportal.com", "shortcoursesportal.com",
                      "distancelearningportal.com", "studyportals.com")

_STUDYPORTALS_HINT = (" Mastersportal and Bachelorsportal show only the first results page to automated tools, "
                      "and this app does not disguise itself to get around that. To get this page, open it in "
                      "your normal browser and save it with Ctrl+S; the Programme importer can read saved pages.")


def _params(url: str) -> dict[str, str]:
    return {k: v for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True) if not IGNORED_PARAMS.match(k)}


def _hint(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return _STUDYPORTALS_HINT if any(host == h or host.endswith("." + h) for h in STUDYPORTALS_HOSTS) else ""


def dropped_params(requested_url: str, final_url: str) -> list[str]:
    """Query parameters of the request that are missing or changed in the final URL."""
    final = _params(final_url)
    return [f"{k}={v}" for k, v in _params(requested_url).items() if final.get(k) != v]


def check_final_url(requested_url: str, final_url: str) -> None:
    """Raise if redirects removed or changed query parameters (``?page=5`` → page 1)."""
    lost = dropped_params(requested_url, final_url)
    if lost and final_url != requested_url:
        raise DownloadError(
            f"The website redirected {requested_url} to {final_url} and removed '{'&'.join(lost)}', so it would "
            f"have returned different content from the page you asked for. Nothing was saved." + _hint(requested_url),
            code="content_mismatch")


def check_page_number(requested_url: str, html: str) -> None:
    """Raise if the page reports a different results page from the one requested."""
    requested = _params(requested_url).get("page")
    if not requested or not requested.isdigit():
        return
    match = CURRENT_PAGE_MARKER.search(html)
    if match and match.group(1) != requested:
        raise DownloadError(
            f"You asked for page {requested}, but the website returned page {match.group(1)}. "
            f"Nothing was saved." + _hint(requested_url), code="content_mismatch")
