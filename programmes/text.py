"""Text normalisation helpers shared by the parsers."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

SPACE_LIKE = re.compile(r"[\s\u00a0\u202f\u2007\u2009\u200a\u200b]+")
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")


def clean_text(value: str | None) -> str | None:
    """Collapse whitespace (including non-breaking spaces); return None if empty."""
    if value is None:
        return None
    text = SPACE_LIKE.sub(" ", value).strip()
    return text or None


def visible_text(element: Tag | None, exclude_selector: str | None = None) -> str | None:
    """Text of ``element`` without HTML comments, optionally ignoring some children."""
    if element is None:
        return None
    if exclude_selector:
        element = BeautifulSoup(str(element), "html.parser").find()
        for child in element.select(exclude_selector):
            child.decompose()
    # get_text() skips Comment nodes, so "<!-- -->" markers never leak into values.
    return clean_text(element.get_text(" "))


def parse_number(text: str | None) -> int | float | None:
    """First number in ``text`` ("6.5" -> 6.5, "125" -> 125); None if there is none."""
    if not text:
        return None
    match = NUMBER.search(THOUSANDS.sub("", text))
    if not match:
        return None
    raw = match.group(0)
    return float(raw) if "." in raw else int(raw)


def parse_amount(value: str | None) -> int | float | None:
    """Parse a machine-readable amount attribute such as data-amount="18792"."""
    if value is None or not str(value).strip():
        return None
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    return int(number) if number.is_integer() else number
