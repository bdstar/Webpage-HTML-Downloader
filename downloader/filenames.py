"""Safe filename generation and atomic file finalisation."""

from __future__ import annotations

import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

MAX_SLUG_LENGTH = 80
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_REPEATS = re.compile(r"[-_.]{2,}")
_STRIP_EXTENSIONS = re.compile(r"\.(html?|php|aspx?|jsp|cgi)$", re.IGNORECASE)
_finalize_lock = threading.Lock()


def slugify_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "page").lower()
    if host.startswith("www."):
        host = host[4:]
    path = _STRIP_EXTENSIONS.sub("", unquote(parts.path).strip("/"))
    raw = host if not path else f"{host}_{path.replace('/', '_')}"
    slug = _UNSAFE.sub("-", raw)
    slug = _REPEATS.sub("-", slug).strip("-_.")
    slug = slug[:MAX_SLUG_LENGTH].rstrip("-_.")
    return slug or "page"


def build_filename(url: str, now: datetime | None = None, token: str | None = None,
                   variant: str | None = None) -> str:
    """Return e.g. ``example.com_blog_post_20261014-174500_a1b2c3.html``."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    token = token or secrets.token_hex(3)
    middle = f"_{variant}" if variant else ""
    return f"{slugify_url(url)}{middle}_{stamp}_{token}.html"


def finalize_file(temp_path: Path, directory: Path, filename: str) -> Path:
    """Move a completed temp file to ``directory/filename`` without overwriting.

    If the name is taken, ``-1``, ``-2`` ... is appended before the extension.
    """
    stem, ext = os.path.splitext(filename)
    with _finalize_lock:
        candidate = directory / filename
        counter = 1
        while candidate.exists():
            candidate = directory / f"{stem}-{counter}{ext}"
            counter += 1
        os.replace(temp_path, candidate)
    return candidate
