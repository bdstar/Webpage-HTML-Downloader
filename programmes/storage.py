"""Windows-safe filenames and atomic, verified JSON output."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path

RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                  *(f"LPT{i}" for i in range(1, 10))}
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_STEM = 120


def safe_filename(name: str, suffix: str = ".json") -> str:
    """``name`` as a valid Windows file name: no reserved names, invalid characters or trailing dots."""
    text = unicodedata.normalize("NFKC", name or "").strip()
    text = INVALID_CHARS.sub("-", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"-{2,}", "-", text).strip(" .-")
    text = text[:MAX_STEM].rstrip(" .-") or "unnamed"
    if text.split(".")[0].upper() in RESERVED_NAMES:
        text = f"_{text}"
    return text + suffix


def write_json_atomic(path: Path, payload, verify_count: int | None = None) -> None:
    """Write via a temp file and ``os.replace``; re-read to confirm the file is complete."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem[:40]}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    if verify_count is not None:
        written = read_json(path)
        programs = written.get("programs") if isinstance(written, dict) else None
        if not isinstance(programs, list) or len(programs) != verify_count:
            raise OSError(f"verification failed for {path.name}: expected {verify_count} records")


def read_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)
