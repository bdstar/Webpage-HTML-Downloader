"""Identify what a saved HTML file is: portal, page kind, original URL and page number."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .pagination import page_number_of, study_id_of
from .sources import Portal, PORTALS, portal_for_host
from .text import clean_text

KIND_LISTING = "listing"
KIND_DETAIL = "detail"
KIND_BLOCKED = "blocked"
KIND_UNKNOWN = "unknown"

PORTAL_TYPE = re.compile(r"PortalType\s*=\s*['\"](\w+)['\"]")
SAVED_FROM = re.compile(r"<!--\s*saved from url=\(\d+\)(\S+?)\s*-->", re.IGNORECASE)
BLOCK_TITLES = re.compile(r"attention required|just a moment|access denied|verify you are human|"
                          r"security check|captcha", re.IGNORECASE)
BLOCK_MARKERS = ("cf-error-details", "cf-browser-verification", "challenge-platform", "cf-turnstile",
                 "g-recaptcha", "h-captcha")


@dataclass
class PageInfo:
    kind: str
    portal: Portal | None
    source_url: str | None
    page_number: int | None = None
    total_pages: int | None = None
    study_id: str | None = None
    title: str | None = None
    reason: str | None = None  # why a page is blocked/unknown


def make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # lxml unavailable
        return BeautifulSoup(html, "html.parser")


def _int_attr(element, name: str) -> int | None:
    value = element.get(name) if element is not None else None
    return int(value) if value and str(value).isdigit() else None


def classify(html: str, soup: BeautifulSoup | None = None) -> PageInfo:
    soup = soup or make_soup(html)
    title = clean_text(soup.title.get_text()) if soup.title else None

    saved = SAVED_FROM.search(html[:4096])
    canonical = soup.select_one('link[rel="canonical"][href]')
    og_url = soup.select_one('meta[property="og:url"][content]')
    candidates = [saved.group(1) if saved else None,
                  canonical["href"] if canonical else None,
                  og_url["content"] if og_url else None]
    source_url = next((u for u in candidates if u and u.startswith("http")), None)

    portal = None
    match = PORTAL_TYPE.search(html)
    if match and match.group(1) in PORTALS:
        portal = PORTALS[match.group(1)]
    elif source_url:
        portal = portal_for_host(urlsplit(source_url).hostname)

    if (title and BLOCK_TITLES.search(title)) or (
            not soup.select("section.Card.ProgrammeCard") and not soup.select_one("#StudyKeyFacts")
            and any(marker in html for marker in BLOCK_MARKERS)):
        return PageInfo(KIND_BLOCKED, portal, source_url, title=title,
                        reason="The saved page is a Cloudflare/CAPTCHA check, not portal content. "
                               "Open the page in your browser, complete any check it shows, and save it again.")

    path = urlsplit(source_url).path if source_url else ""
    cards = soup.select("section.Card.ProgrammeCard")
    if cards or path.startswith("/search/"):
        nav = soup.select_one("nav.Pagination")
        page = _int_attr(nav, "data-current-page") or page_number_of(source_url)
        if cards:
            return PageInfo(KIND_LISTING, portal, source_url, page_number=page,
                            total_pages=_int_attr(nav, "data-total-pages"), title=title)
        return PageInfo(KIND_UNKNOWN, portal, source_url, page_number=page, title=title,
                        reason="This search page has no programme cards. Save it again after the results "
                               "have loaded (Ctrl+S, 'Webpage, Complete' or 'HTML only').")

    if soup.select_one("#StudyKeyFacts") or study_id_of(source_url):
        if not soup.select_one("h1 .StudyTitle, h1.StudyTitleWrapper"):
            return PageInfo(KIND_UNKNOWN, portal, source_url, study_id=study_id_of(source_url), title=title,
                            reason="The programme page has no title block; it may not have finished loading.")
        return PageInfo(KIND_DETAIL, portal, source_url, study_id=study_id_of(source_url), title=title)

    return PageInfo(KIND_UNKNOWN, portal, source_url, title=title,
                    reason="Not a Mastersportal/Bachelorsportal listing or programme page.")
