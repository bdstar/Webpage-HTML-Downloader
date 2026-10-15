"""Programme upserts (SQLite) and per-university JSON regeneration."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .db import Store, utcnow
from .detail import DetailResult
from .listing import ListingCard
from .records import (DETAIL_FIELDS, LISTING_FIELDS, build_record, fallback_programme_key, merge_values,
                      university_key)
from .sources import PORTALS
from .storage import read_json, safe_filename, write_json_atomic

UNKNOWN_UNIVERSITY = "Unknown university"
INDEX_FILE = "_index.json"


def _loads(value, default):
    return json.loads(value) if value else default


class Dataset:
    def __init__(self, store: Store, output_root: Path):
        self.store = store
        self.output_root = output_root
        self._locks = {category: threading.Lock() for category in PORTALS}

    def output_dir(self, category: str) -> Path:
        return self.output_root / PORTALS[category].output_subdir

    # --- upserts (call inside store.transaction()) ------------------------------------------

    def _university(self, conn: sqlite3.Connection, category: str, name: str | None) -> str:
        key = university_key(name) or "unknown"
        row = conn.execute("SELECT key FROM universities WHERE category = ? AND key = ?", (category, key)).fetchone()
        if row is None:
            display = name or UNKNOWN_UNIVERSITY
            base = safe_filename(display, suffix="")
            filename, n = f"{base}.json", 2
            while conn.execute("SELECT 1 FROM universities WHERE category = ? AND lower(filename) = lower(?)",
                               (category, filename)).fetchone() or filename == INDEX_FILE:
                filename, n = f"{base} ({n}).json", n + 1
            conn.execute("INSERT INTO universities (category, key, display_name, filename) VALUES (?, ?, ?, ?)",
                         (category, key, display, filename))
        return key

    def _save(self, conn, category: str, key: str, existing, study_id, program_url, listing, detail,
              provenance, warnings, task_id) -> set[str]:
        name = (listing or {}).get("university_name") or (detail or {}).get("university_name")
        ukey = self._university(conn, category, name)
        now = utcnow()
        values = (study_id, program_url, ukey, json.dumps(listing, ensure_ascii=False) if listing else None,
                  json.dumps(detail, ensure_ascii=False) if detail else None,
                  json.dumps(provenance, ensure_ascii=False), json.dumps(warnings, ensure_ascii=False))
        if existing is None:
            conn.execute("INSERT INTO programmes (study_id, program_url, university_key, listing_json, detail_json, "
                         "provenance_json, warnings_json, category, programme_key, first_task_id, last_task_id, "
                         "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (*values, category, key, task_id, task_id, now, now))
        else:
            conn.execute("UPDATE programmes SET study_id = ?, program_url = ?, university_key = ?, listing_json = ?, "
                         "detail_json = ?, provenance_json = ?, warnings_json = ?, last_task_id = ?, updated_at = ? "
                         "WHERE category = ? AND programme_key = ?",
                         (*values, task_id, now, category, key))
        affected = {ukey}
        if existing is not None and existing["university_key"]:
            affected.add(existing["university_key"])
        return affected

    @staticmethod
    def _existing(conn, category: str, key: str):
        row = conn.execute("SELECT * FROM programmes WHERE category = ? AND programme_key = ?",
                           (category, key)).fetchone()
        return dict(row) if row else None

    def upsert_listing(self, conn, category: str, card: ListingCard, source: dict,
                       task_id: str) -> tuple[bool, set[str]]:
        key = card.study_id or fallback_programme_key(card.university_name, card.program_name)
        existing = self._existing(conn, category, key)
        new = {name: getattr(card, name) for name in LISTING_FIELDS}
        listing, kept = merge_values(_loads(existing and existing["listing_json"], None), new, LISTING_FIELDS)
        warnings = _loads(existing and existing["warnings_json"], {})
        warnings["listing"] = card.warnings + [f"{name} kept from an earlier listing import" for name in kept]
        provenance = _loads(existing and existing["provenance_json"], {})
        provenance["portal"] = PORTALS[category].host
        provenance["listing"] = {**source, "position": card.position, "imported_at": utcnow(), "task_id": task_id}
        affected = self._save(conn, category, key, existing, card.study_id, card.program_url, listing,
                              _loads(existing and existing["detail_json"], None), provenance, warnings, task_id)
        return existing is None, affected

    def upsert_detail(self, conn, category: str, result: DetailResult, source: dict,
                      task_id: str) -> tuple[bool, set[str]]:
        key = result.study_id
        existing = self._existing(conn, category, key)
        detail, kept = merge_values(_loads(existing and existing["detail_json"], None), result.data, DETAIL_FIELDS)
        warnings = _loads(existing and existing["warnings_json"], {})
        warnings["detail"] = result.warnings + [f"{name} kept from an earlier detail import" for name in kept]
        provenance = _loads(existing and existing["provenance_json"], {})
        provenance["portal"] = PORTALS[category].host
        provenance["detail"] = {**source, "imported_at": utcnow(), "task_id": task_id}
        affected = self._save(conn, category, key, existing, result.study_id, result.program_url,
                              _loads(existing and existing["listing_json"], None), detail, provenance, warnings,
                              task_id)
        return existing is None, affected

    # --- JSON output ------------------------------------------------------------------------

    def record_from_row(self, category: str, row: dict) -> dict:
        warnings = _loads(row["warnings_json"], {})
        flat = [w for group in warnings.values() for w in group]
        return build_record(category, row["programme_key"], _loads(row["listing_json"], None),
                            _loads(row["detail_json"], None), _loads(row["provenance_json"], {}), flat)

    def write_universities(self, category: str, ukeys: set[str], output_dir: Path | None = None) -> int:
        """Regenerate the JSON file of each affected university; returns the number of files written."""
        output_dir = output_dir or self.output_dir(category)
        written = 0
        with self._locks[category]:
            known = self.store.programme_keys(category)
            for ukey in sorted(k for k in ukeys if k):
                written += self._write_university(category, ukey, known, output_dir)
            self._write_index(category, output_dir)
        return written

    def _write_university(self, category: str, ukey: str, known: set[str], output_dir: Path) -> int:
        university = self.store.university(category, ukey)
        if university is None:
            return 0
        path = output_dir / university["filename"]
        records = [self.record_from_row(category, row) for row in self.store.programmes_for_university(category, ukey)]

        preserved = []
        if path.exists():
            try:
                previous = read_json(path)
                for item in previous.get("programs", []) if isinstance(previous, dict) else []:
                    ident = item.get("program_url") or (item.get("study_id") and str(item.get("study_id")))
                    if ident and ident not in known and item.get("study_id") not in known:
                        preserved.append(item)  # added outside this tool; keep it untouched
            except (OSError, ValueError) as exc:
                raise OSError(f"existing file {path.name} is unreadable ({exc}); not overwriting it") from exc

        if not records and not preserved:
            path.unlink(missing_ok=True)
            return 0
        programs = sorted(records + preserved, key=lambda r: ((r.get("program_name") or "").casefold(),
                                                              r.get("program_url") or ""))
        payload = {
            "university_name": university["display_name"],
            "category": category,
            "portal": PORTALS[category].host,
            "generated_at": utcnow(),
            "programme_count": len(programs),
            "programs": programs,
        }
        write_json_atomic(path, payload, verify_count=len(programs))
        return 1

    def _write_index(self, category: str, output_dir: Path) -> None:
        counts = {r["university_key"]: r["n"] for r in self.store.query(
            "SELECT university_key, COUNT(*) AS n FROM programmes WHERE category = ? GROUP BY university_key",
            (category,))}
        entries = [{"university_name": u["display_name"], "file": u["filename"], "programme_count": counts[u["key"]]}
                   for u in self.store.universities(category)
                   if counts.get(u["key"]) and (output_dir / u["filename"]).exists()]
        write_json_atomic(output_dir / INDEX_FILE, {
            "category": category, "generated_at": utcnow(), "university_count": len(entries),
            "programme_count": sum(e["programme_count"] for e in entries), "universities": entries})

    def rebuild_all(self, category: str, output_dir: Path | None = None) -> int:
        keys = {u["key"] for u in self.store.universities(category)}
        return self.write_universities(category, keys, output_dir)
