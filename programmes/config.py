"""Settings for the programme importer."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from downloader.config import BASE_DIR


@dataclass(frozen=True)
class ImportSettings:
    data_dir: Path = BASE_DIR / "data"
    output_root: Path = BASE_DIR
    import_root: Path = BASE_DIR / "imports"
    delete_html_default: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "programmes.sqlite3"

    def import_folder(self, category: str) -> Path:
        return self.import_root / category

    @classmethod
    def from_env(cls) -> "ImportSettings":
        def path(name: str, default: Path) -> Path:
            value = os.environ.get(name)
            return Path(value).expanduser().resolve() if value else default

        keep = os.environ.get("KEEP_IMPORTED_HTML", "").strip().lower() in ("1", "true", "yes", "on")
        return cls(
            data_dir=path("PROGRAMME_DATA_DIR", cls.data_dir),
            output_root=path("PROGRAMME_OUTPUT_ROOT", cls.output_root),
            import_root=path("PROGRAMME_IMPORT_ROOT", cls.import_root),
            delete_html_default=not keep,
        )
