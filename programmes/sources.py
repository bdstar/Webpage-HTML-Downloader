"""Per-portal configuration for the two programme categories."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Portal:
    category: str          # "master" | "bachelor"
    label: str             # human-readable category name
    host: str              # canonical host name
    base_listing_url: str  # UK search page (pagination page 1)
    output_subdir: str     # relative to the output root, e.g. "UK/master/programs"

    @property
    def base_url(self) -> str:
        return f"https://{self.host}/"


PORTALS: dict[str, Portal] = {
    "master": Portal(
        category="master",
        label="Master's",
        host="www.mastersportal.com",
        base_listing_url="https://www.mastersportal.com/search/master/united-kingdom",
        output_subdir="UK/master/programs",
    ),
    "bachelor": Portal(
        category="bachelor",
        label="Bachelor's",
        host="www.bachelorsportal.com",
        base_listing_url="https://www.bachelorsportal.com/search/bachelor/united-kingdom",
        output_subdir="UK/bachelor/programs",
    ),
}


def portal_for_host(host: str | None) -> Portal | None:
    if not host:
        return None
    host = host.lower().removeprefix("www.")
    for portal in PORTALS.values():
        if portal.host.removeprefix("www.") == host:
            return portal
    return None
