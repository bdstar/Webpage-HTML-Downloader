"""Settings for the university importer (shares the data directory with the programme importer)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from downloader.config import BASE_DIR
from programmes.config import ImportSettings


@dataclass(frozen=True)
class UniversitySettings:
    data_dir: Path = BASE_DIR / "data"
    export_root: Path = BASE_DIR / "export"
    import_folder: Path = BASE_DIR / "imports" / "university"
    delete_html_default: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "universities.sqlite3"

    @classmethod
    def from_env(cls) -> "UniversitySettings":
        base = ImportSettings.from_env()

        def path(name: str, default: Path) -> Path:
            value = os.environ.get(name)
            return Path(value).expanduser().resolve() if value else default

        return cls(
            data_dir=base.data_dir,
            export_root=path("UNIVERSITY_EXPORT_ROOT", base.output_root / "export"),
            import_folder=path("UNIVERSITY_IMPORT_FOLDER", base.import_root / "university"),
            delete_html_default=base.delete_html_default,
        )
