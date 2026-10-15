"""Listing-page URL generation and canonical programme URLs."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .sources import Portal, portal_for_host

STUDY_PATH = re.compile(r"^/studies/(\d+)(?:/[^/?#]*)?")
# Query parameters added by the portals or by tracking that do not change the page content.
NON_ESSENTIAL_PARAMS = {"bot", "ref", "fbclid", "gclid", "msclkid", "_ga", "mc_cid", "mc_eid"}


class PaginationError(ValueError):
    pass


def validate_listing_url(url: str, portal: Portal) -> str:
    """Check that ``url`` is an https/http search URL on the portal's own domain."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PaginationError("Enter a full listing URL starting with https://")
    if portal_for_host(parts.hostname) is not portal:
        raise PaginationError(f"This URL is not on {portal.host}; choose the matching programme category.")
    if not parts.path.startswith("/search/"):
        raise PaginationError("The URL must be a search/listing page (its path starts with /search/).")
    return urlunsplit(("https", portal.host, parts.path.rstrip("/") or "/", parts.query, ""))


def page_url(base_url: str, page: int) -> str:
    """URL of listing page ``page``: page 1 has no ``page`` parameter, later pages use ``?page=N``.

    Any existing ``page`` (and the portal-added ``bot``) parameter is replaced; other
    query parameters are preserved in their original order.
    """
    if not isinstance(page, int) or page < 1:
        raise PaginationError("Page numbers start at 1.")
    parts = urlsplit(base_url)
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in ("page", "bot")]
    if page > 1:
        params.append(("page", str(page)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ""))


def page_urls(base_url: str, max_page: int) -> list[str]:
    if not isinstance(max_page, int) or max_page < 1:
        raise PaginationError("Maximum pagination must be a positive whole number.")
    return [page_url(base_url, n) for n in range(1, max_page + 1)]


def page_number_of(url: str | None) -> int | None:
    """Page number encoded in a listing URL (1 when no ``page`` parameter is present)."""
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.path.startswith("/search/"):
        return None
    for key, value in parse_qsl(parts.query):
        if key == "page":
            return int(value) if value.isdigit() and int(value) >= 1 else None
    return 1


def study_id_of(url: str | None) -> str | None:
    if not url:
        return None
    match = STUDY_PATH.match(urlsplit(url).path)
    return match.group(1) if match else None


def canonical_programme_url(url: str | None, portal: Portal | None = None) -> str | None:
    """``https://<portal host>/studies/<id>/<slug>.html`` without query or fragment."""
    if not url:
        return None
    parts = urlsplit(url.strip())
    if not STUDY_PATH.match(parts.path):
        return None
    host_portal = portal_for_host(parts.hostname) or portal
    host = host_portal.host if host_portal else (parts.hostname or "")
    return urlunsplit(("https", host, parts.path, "", ""))


def strip_tracking(url: str | None) -> str | None:
    """Remove utm_* and other known tracking parameters; keep everything else."""
    if not url:
        return None
    parts = urlsplit(url.strip())
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
              if not k.lower().startswith("utm_") and k.lower() not in NON_ESSENTIAL_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))
