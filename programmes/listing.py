"""Listing (search results) page parser for both portals."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from extract_programs import (CARD_SELECTOR, extract_cost, extract_location, extract_program_name,
                              extract_program_type, extract_university_name)

from .pagination import canonical_programme_url, study_id_of
from .sources import Portal
from .text import clean_text, visible_text


@dataclass
class ListingCard:
    position: int
    study_id: str | None
    program_url: str | None
    university_name: str | None
    university_location: str | None
    program_name: str | None
    program_type: str | None
    cost: str | None
    listing_duration: str | None
    warnings: list[str] = field(default_factory=list)


@dataclass
class ListingResult:
    cards_found: int = 0
    cards: list[ListingCard] = field(default_factory=list)
    skipped_positions: list[int] = field(default_factory=list)
    duplicate_ids: dict[str, list[int]] = field(default_factory=dict)


def _program_url(card, portal: Portal) -> str | None:
    link = card.select_one("a.VisitProgramme[href]") or card.select_one('a[href*="/studies/"]')
    href = clean_text(link.get("href")) if link else None
    if not href or href.startswith(("#", "javascript:")):
        return None
    return canonical_programme_url(urljoin(portal.base_url, href), portal)


def parse_listing(soup: BeautifulSoup, portal: Portal) -> ListingResult:
    result = ListingResult()
    cards = soup.select(CARD_SELECTOR)
    result.cards_found = len(cards)
    positions: dict[str, list[int]] = {}

    for index, card in enumerate(cards, start=1):
        url = _program_url(card, portal)
        item = ListingCard(
            position=index,
            study_id=study_id_of(url),
            program_url=url,
            university_name=extract_university_name(card),
            university_location=extract_location(card),
            program_name=extract_program_name(card),
            program_type=extract_program_type(card),
            cost=extract_cost(card),
            listing_duration=visible_text(card.select_one(".ProgrammePrice .Duration")),
        )
        if not (item.university_name or item.program_name or item.program_url):
            result.skipped_positions.append(index)  # skeleton/placeholder card
            continue
        for name in ("university_name", "program_name", "program_url"):
            if getattr(item, name) is None:
                item.warnings.append(f"listing card is missing {name}")
        if item.study_id:
            positions.setdefault(item.study_id, []).append(index)
        result.cards.append(item)

    result.duplicate_ids = {k: v for k, v in positions.items() if len(v) > 1}
    return result
