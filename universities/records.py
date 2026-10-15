"""University JSON records: schema validation, safe merging and identity checks."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from programmes.storage import read_json


class RecordError(ValueError):
    """A record (new or already on disk) that must not be written over or trusted."""


def name_key(name: str | None) -> str:
    text = unicodedata.normalize("NFKC", name or "").casefold()
    text = re.sub(r"[^\w]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def validate_record(record, fields: tuple[str, ...]) -> list[str]:
    """Problems with ``record``: every schema field present, each a string or null (never "null")."""
    if not isinstance(record, dict):
        return ["the JSON file does not contain an object"]
    problems = []
    for name in fields:
        if name not in record:
            problems.append(f"field '{name}' is missing")
            continue
        value = record[name]
        if value is not None and not isinstance(value, str):
            problems.append(f"field '{name}' must be a string or null, not {type(value).__name__}")
        elif isinstance(value, str) and (not value.strip() or value.strip().lower() == "null"):
            problems.append(f"field '{name}' must be null instead of '{value}'")
    if not record.get("university"):
        problems.append("field 'university' is empty")
    return problems


def read_existing(path: Path) -> dict | None:
    """The record stored at ``path``; None if there is no file. Raises RecordError if it is unusable."""
    if not path.exists():
        return None
    try:
        record = read_json(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecordError(f"the existing file {path.name} is not valid JSON ({exc}); it was left untouched. "
                          "Fix or remove it, then retry") from None
    if not isinstance(record, dict):
        raise RecordError(f"the existing file {path.name} does not contain a university record; "
                          "it was left untouched")
    return record


def merge(existing: dict | None, new: dict, fields: tuple[str, ...]) -> tuple[dict, list[str]]:
    """New values win, but a missing (null) value never erases a stored one; unknown keys are kept."""
    existing = existing or {}
    merged = {name: new[name] if new.get(name) is not None else existing.get(name) for name in fields}
    merged.update({k: v for k, v in existing.items() if k not in merged})
    changed = [name for name in fields if merged.get(name) != existing.get(name)]
    return merged, changed
