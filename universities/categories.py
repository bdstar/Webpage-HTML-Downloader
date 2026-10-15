"""University categories (which portal and count field) and the per-country export layout."""

from __future__ import annotations

import re
from dataclasses import dataclass

from programmes.sources import PORTALS, Portal
from programmes.storage import safe_filename


@dataclass(frozen=True)
class UniversityCategory:
    key: str                     # "master" | "bachelor" (also the dropdown value)
    label: str                   # shown in the dropdown
    portal: Portal               # where the saved pages come from
    count_field: str             # JSON field holding the number of programmes
    count_label: re.Pattern      # matches the label next to that number on the page

    @property
    def fields(self) -> tuple[str, ...]:
        return ("university", "location", "country", "global_top", "worldwide_number", "official_webiste",
                self.count_field, "qs_rank", "institution_type")


CATEGORIES: dict[str, UniversityCategory] = {
    "master": UniversityCategory(
        key="master",
        label="UK / Master's",
        portal=PORTALS["master"],
        count_field="number_master_program",
        count_label=re.compile(r"\bmaster", re.IGNORECASE),
    ),
    "bachelor": UniversityCategory(
        key="bachelor",
        label="UK / Bachelor's",
        portal=PORTALS["bachelor"],
        count_field="number_bachelor_program",
        count_label=re.compile(r"\bbachelor", re.IGNORECASE),
    ),
}

# JSON files go to <export folder>/<country folder>/universities/<University>.json
UNIVERSITIES_SUBDIR = "universities"
COUNTRY_FOLDERS = {"United Kingdom": "UK"}
UNKNOWN_COUNTRY_FOLDER = "Unknown country"


def country_folder(country: str | None) -> str:
    if not country:
        return UNKNOWN_COUNTRY_FOLDER
    return COUNTRY_FOLDERS.get(country) or safe_filename(country, suffix="")


POLICIES = {
    "update": "Update existing records",
    "create_only": "Create new records only",
    "skip_imported": "Skip already imported records",
}
DEFAULT_POLICY = "update"
