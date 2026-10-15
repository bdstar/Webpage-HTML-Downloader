"""Extract university profile facts from a saved Mastersportal / Bachelorsportal university page."""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

from programmes.classify import BLOCK_MARKERS, BLOCK_TITLES, PORTAL_TYPE, SAVED_FROM
from programmes.sources import PORTALS, Portal, portal_for_host
from programmes.text import clean_text, visible_text

from .categories import UniversityCategory

UNIVERSITY_PATH = re.compile(r"/universities/(\d+)(?:/|$)")
GLOBAL_TOP = re.compile(r"top\s*(\d+(?:\.\d+)?)\s*%", re.IGNORECASE)
WORLDWIDE = re.compile(r"#\s*(\d[\d,]*)")
COUNT = re.compile(r"^\d[\d,]*$")
RANK = re.compile(r"^#?\s*(\d+(?:\s*[-–]\s*\d+)?\+?)$")
QS_NAME = re.compile(r"\bQS\b")
MAX_NAME_LENGTH = 200


@dataclass
class UniversityPage:
    data: dict[str, str | None]
    portal: Portal | None
    source_url: str | None
    university_id: str | None
    warnings: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def looks_like_university_page(text: str) -> bool:
    """Cheap check used when scanning a folder (the full parse happens during the import)."""
    return 'data-cy="OrganisationName"' in text or ('class="OrganisationName"' in text
                                                     and "OrganisationLocation" in text)


def blocked_reason(html: str, soup: BeautifulSoup) -> str | None:
    title = clean_text(soup.title.get_text()) if soup.title else None
    if (title and BLOCK_TITLES.search(title)) or (
            not soup.select_one(".OrganisationName") and any(marker in html for marker in BLOCK_MARKERS)):
        return ("The saved page is a Cloudflare/CAPTCHA check, not the university profile. Open the page in "
                "your browser, complete any check it shows, and save it again.")
    return None


def source_of(html: str, soup: BeautifulSoup) -> tuple[str | None, Portal | None, str | None]:
    """Original URL, portal and Studyportals university id of a saved page."""
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
    found = UNIVERSITY_PATH.search(urlsplit(source_url).path) if source_url else None
    return source_url, portal, found.group(1) if found else None


# --- individual fields ------------------------------------------------------------------------------

def _university(soup: BeautifulSoup, warnings: list[str]) -> str | None:
    primary = soup.select_one('span.OrganisationName[data-cy="OrganisationName"]')
    name = visible_text(primary, ".PremiumBadge, .Tooltip") if primary else None
    others = {visible_text(el, ".PremiumBadge, .Tooltip") for el in soup.select("span.OrganisationName")
              if el is not primary} - {None}
    if name is None and len(others) == 1:
        name = others.pop()
        warnings.append("university name taken from a section heading (the page header has no name)")
    elif name is None and others:
        warnings.append(f"conflicting university names on the page: {', '.join(sorted(others))}")
        return None
    elif name and others - {name}:
        warnings.append(f"other names also appear on the page: {', '.join(sorted(others - {name}))}")
    if name and len(name) > MAX_NAME_LENGTH:
        warnings.append("university name is implausibly long; ignored")
        return None
    return name


def _location(soup: BeautifulSoup, warnings: list[str]) -> tuple[str | None, str | None]:
    box = soup.select_one("div.OrganisationLocation")
    if box is None:
        return None, None
    parts = [clean_text(span.get_text(" ")) for span in box.find_all("span", recursive=False)
             if "NumberOfVenues" not in (span.get("class") or [])]
    parts = [p for p in parts if p]
    if len(parts) >= 2:
        if len(parts) > 2:
            warnings.append(f"location has {len(parts)} parts; used the first as location and the last as country")
        return parts[0], parts[-1]
    if len(parts) == 1:
        if parts[0].endswith(","):
            return parts[0], None
        warnings.append(f"only one location value ('{parts[0]}'); stored as the country")
        return None, parts[0]
    return None, None


def _global_top(soup: BeautifulSoup) -> str | None:
    match = GLOBAL_TOP.search(visible_text(soup.select_one("span.GlobalRank")) or "")
    return f"{match.group(1)}%" if match else None


def _worldwide(soup: BeautifulSoup) -> str | None:
    match = WORLDWIDE.search(visible_text(soup.select_one("span.GlobalRankTop")) or "")
    return match.group(1).replace(",", "") if match else None


def official_url(raw: str | None) -> str | None:
    """``data-description`` value as a clean http(s) URL without the query string."""
    if not raw:
        return None
    text = raw.strip()
    for _ in range(3):  # entities can be double-encoded ("&amp;amp;")
        decoded = html_lib.unescape(text)
        if decoded == text:
            break
        text = decoded
    try:
        parts = urlsplit(text.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", "")).strip()


def _website(soup: BeautifulSoup, warnings: list[str]) -> str | None:
    urls = []
    for link in soup.select("a.UniversityWebsiteLink[data-description]"):
        url = official_url(link.get("data-description"))
        if url and url not in urls:
            urls.append(url)
    if len(urls) > 1:
        warnings.append(f"several website links found ({', '.join(urls)}); used the first")
    return urls[0] if urls else None


def _number(text: str | None) -> str | None:
    text = clean_text(text)
    return text.replace(",", "") if text and COUNT.match(text) else None


def _programme_count(soup: BeautifulSoup, category: UniversityCategory, warnings: list[str]) -> str | None:
    quick = None
    for item in soup.select("li.QuickFactWrapper"):
        label = visible_text(item.select_one(".QuickFactType"))
        if item.get("data-cy") == "QuickFactStudies" or (label and category.count_label.search(label)):
            if label and not category.count_label.search(label):
                warnings.append(f"the programme count is labelled '{label}', not {category.portal.label}")
                continue
            quick = _number(visible_text(item.select_one(".QuickFactData")))
            break
    stat = None
    for item in soup.select("li.KeyStatisticItem"):
        label = visible_text(item.select_one(".FigureType"))
        if label and category.count_label.search(label):
            stat = _number(visible_text(item.select_one(".Figure")))
            break
    if quick and stat and quick != stat:
        warnings.append(f"programme count differs between quick facts ({quick}) and key statistics ({stat})")
    return quick or stat


def _qs_rank(soup: BeautifulSoup, warnings: list[str]) -> str | None:
    values = []
    for info in soup.select(".RankingInfo"):
        name = visible_text(info.select_one(".RankingName"))
        if not name or not QS_NAME.search(name):
            continue
        match = RANK.match(visible_text(info.select_one(".LatestRanking")) or "")
        if match:
            value = re.sub(r"\s+", "", match.group(1)).replace("–", "-")
            if value not in values:
                values.append(value)
    if len(values) > 1:
        warnings.append(f"several QS rankings found ({', '.join(values)}); used the first")
    return values[0] if values else None


def _key_statistic(soup: BeautifulSoup, label: str) -> str | None:
    for item in soup.select("li.KeyStatisticItem"):
        if (visible_text(item.select_one(".FigureType")) or "").casefold() == label.casefold():
            return visible_text(item.select_one(".Figure"))
    return None


# --- whole page -------------------------------------------------------------------------------------

def parse_university(html: str, soup: BeautifulSoup, category: UniversityCategory) -> UniversityPage:
    warnings: list[str] = []
    source_url, portal, university_id = source_of(html, soup)
    location, country = _location(soup, warnings)
    data = {
        "university": _university(soup, warnings),
        "location": location,
        "country": country,
        "global_top": _global_top(soup),
        "worldwide_number": _worldwide(soup),
        "official_webiste": _website(soup, warnings),
        category.count_field: _programme_count(soup, category, warnings),
        "qs_rank": _qs_rank(soup, warnings),
        "institution_type": _key_statistic(soup, "Institution Type"),
    }
    missing = [name for name in category.fields if data[name] is None]
    warnings.extend(f"{name} not found" for name in missing)
    return UniversityPage(data, portal, source_url, university_id, warnings, missing)
