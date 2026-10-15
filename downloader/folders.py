"""Download-folder selection: validation and a minimal server-side folder browser."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from .errors import DownloadError

INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                  *(f"LPT{i}" for i in range(1, 10))}
MAX_LISTED_FOLDERS = 2000


class FolderError(DownloadError):
    def __init__(self, message: str):
        super().__init__(message, code="invalid_folder")


def roots() -> list[str]:
    """Drive roots on Windows (C:\\, D:\\ …), ``/`` elsewhere."""
    if hasattr(os, "listdrives"):
        try:
            return sorted(os.listdrives())
        except OSError:
            pass
    return [os.path.abspath(os.sep)]


def _as_folder(raw: str | None) -> Path:
    text = (raw or "").strip().strip('"')
    if not text:
        raise FolderError("Choose a folder.")
    path = Path(os.path.expandvars(text)).expanduser()
    if not path.is_absolute():
        raise FolderError("Enter a full folder path, for example D:\\Downloads\\pages.")
    return path.resolve()


def validate_download_dir(raw: str | None) -> Path:
    """An existing, writable folder; raises FolderError with a user-facing message otherwise."""
    path = _as_folder(raw)
    if not path.exists():
        raise FolderError(f"Folder not found: {path}")
    if not path.is_dir():
        raise FolderError(f"Not a folder: {path}")
    try:
        fd, probe = tempfile.mkstemp(prefix=".write-test-", dir=path)
        os.close(fd)
        os.unlink(probe)
    except OSError:
        raise FolderError(f"The app cannot write to this folder: {path}") from None
    return path


def list_folders(raw: str | None) -> dict:
    """Sub-folders of ``raw`` (or the drive list when ``raw`` is empty)."""
    if not (raw or "").strip():
        return {"path": None, "parent": None, "roots": roots(),
                "folders": [{"name": r, "path": r} for r in roots()]}
    path = _as_folder(raw)
    if not path.is_dir():
        raise FolderError(f"Folder not found: {path}")
    folders = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_dir() and not entry.name.startswith((".", "$")):
                        folders.append({"name": entry.name, "path": entry.path})
                except OSError:
                    continue
    except PermissionError:
        raise FolderError(f"Access denied: {path}") from None
    except OSError as exc:
        raise FolderError(f"Cannot open {path}: {exc.strerror or exc}") from None
    folders.sort(key=lambda f: f["name"].casefold())
    parent = str(path.parent) if path.parent != path else None
    return {"path": str(path), "parent": parent, "roots": roots(),
            "folders": folders[:MAX_LISTED_FOLDERS], "truncated": len(folders) > MAX_LISTED_FOLDERS}


def create_folder(parent_raw: str | None, name: str | None) -> Path:
    parent = validate_download_dir(parent_raw)
    name = (name or "").strip()
    if (not name or name in (".", "..") or INVALID_NAME_CHARS.search(name) or name.endswith((".", " "))
            or name.split(".")[0].upper() in RESERVED_NAMES or len(name) > 120):
        raise FolderError("Folder names cannot be empty, reserved (CON, NUL…), end with a dot or space, "
                          'or contain < > : " / \\ | ? *')
    target = parent / name
    try:
        target.mkdir()
    except FileExistsError:
        raise FolderError(f"A folder named '{name}' already exists here.") from None
    except OSError as exc:
        raise FolderError(f"Could not create the folder: {exc.strerror or exc}") from None
    return target
