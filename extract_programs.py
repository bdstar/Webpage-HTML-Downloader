"""Extract Master's programme cards from a saved Mastersportal search page into JSON.

Reads a local, JavaScript-rendered HTML file (no network access) and writes one
JSON object per ``section.Card.ProgrammeCard``.

Usage (Windows):
    python extract_programs.py "D:\\path\\to\\page.html"
    python extract_programs.py "D:\\path\\to\\page.html" --output "D:\\out\\programs.json"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

DEFAULT_INPUT = Path(
    r"D:\Website\Global Study Net\download\webpage_html_downloader\downloads"
    r"\mastersportal.com_search_master_united-kingdom_rendered_20261014-191722_91bb79.html"
)
BASE_URL = "https://www.mastersportal.com/"
CARD_SELECTOR = "section.Card.ProgrammeCard"
FIELDS = ("university_name", "university_location", "program_name", "program_type", "cost", "program_url")
REQUIRED_FIELDS = ("university_name", "program_name")

# Card tags that describe study pace or delivery rather than the degree type.
NON_DEGREE_TAG = re.compile(
    r"^(full[- ]?time|part[- ]?time|on[- ]?campus|online|blended|distance( learning)?|"
    r"self[- ]?paced|hybrid|\+\d+( more)?)$",
    re.IGNORECASE,
)
SPACE_LIKE = re.compile(r"[\s\u00a0\u202f\u2007\u2009\u200a]+")


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


def extract_university_name(card: Tag) -> str | None:
    element = card.select_one("strong.OrganisationName")
    if element is None:
        return None
    return clean_text(element.get("title")) or visible_text(element)


def extract_location(card: Tag) -> str | None:
    return visible_text(card.select_one("span.Locations"))


def extract_program_name(card: Tag) -> str | None:
    return visible_text(card.select_one("h2.Title"))


def extract_program_type(card: Tag) -> str | None:
    """Return the degree tag (e.g. "M.Sc."), skipping pace/delivery tags like "Full-time"."""
    tags = card.select(".ProgrammeFacts span.Tag.default") or card.select("span.Tag.default")
    for tag in tags:
        label = clean_text(tag.get("aria-label")) or visible_text(tag, exclude_selector=".Counter")
        if label and not NON_DEGREE_TAG.match(label):
            return label
    return None


def extract_cost(card: Tag) -> str | None:
    """Displayed price such as "BDT 3,067,027", or None when no amount is shown."""
    element = (card.select_one(".ProgrammePrice .CurrentPrice b")
               or card.select_one(".CurrentPrice b")
               or card.select_one(".CurrentPrice"))
    text = visible_text(element)
    if not text or not re.search(r"\d", text):
        return None  # e.g. "Tuition not available"
    return text


def extract_program_url(card: Tag) -> str | None:
    link = card.select_one("a.VisitProgramme[href]")
    if link is None:
        return None
    href = clean_text(link.get("href"))
    if not href or href.startswith(("#", "javascript:")):
        return None
    return urljoin(BASE_URL, href)


def extract_card(card: Tag) -> dict:
    return {
        "university_name": extract_university_name(card),
        "university_location": extract_location(card),
        "program_name": extract_program_name(card),
        "program_type": extract_program_type(card),
        "cost": extract_cost(card),
        "program_url": extract_program_url(card),
    }


@dataclass
class ExtractionResult:
    cards_found: int = 0
    records: list[dict] = field(default_factory=list)
    skipped_cards: list[int] = field(default_factory=list)
    missing_required: list[tuple[int, list[str]]] = field(default_factory=list)
    missing_counts: Counter = field(default_factory=Counter)
    duplicate_urls: dict[str, list[int]] = field(default_factory=dict)


def extract_programs(html: str) -> ExtractionResult:
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # lxml missing or unable to parse
        soup = BeautifulSoup(html, "html.parser")

    result = ExtractionResult()
    cards = soup.select(CARD_SELECTOR)
    result.cards_found = len(cards)
    url_positions: dict[str, list[int]] = {}

    for index, card in enumerate(cards, start=1):
        record = extract_card(card)
        # A card with no name, programme or link is a placeholder/skeleton, not a programme.
        if not any(record[k] for k in ("university_name", "program_name", "program_url")):
            result.skipped_cards.append(index)
            continue
        missing = [k for k in REQUIRED_FIELDS if record[k] is None]
        if missing:
            result.missing_required.append((index, missing))
        for name in FIELDS:
            if record[name] is None:
                result.missing_counts[name] += 1
        if record["program_url"]:
            url_positions.setdefault(record["program_url"], []).append(index)
        result.records.append(record)

    result.duplicate_urls = {u: pos for u, pos in url_positions.items() if len(pos) > 1}
    return result


def read_html(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        print("Warning: file is not valid UTF-8; undecodable bytes were replaced.", file=sys.stderr)
        return data.decode("utf-8", errors="replace")


def write_json_atomic(records: list[dict], output: Path) -> None:
    """Write to a temp file in the same folder, then replace, so reruns never leave a half-written file."""
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".programs-", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(records, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, output)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def print_summary(input_path: Path, output: Path, result: ExtractionResult, status: str) -> None:
    print(f"Input HTML: {input_path.name}")
    print(f"Programme cards found: {result.cards_found}")
    print(f"Valid programme records: {len(result.records)}")
    if result.skipped_cards:
        print(f"Skipped empty cards: {len(result.skipped_cards)} (card #{', #'.join(map(str, result.skipped_cards))})")
    print(f"Records with missing fields: {len(result.missing_required)}")
    for index, missing in result.missing_required:
        print(f"  - card #{index}: missing {', '.join(missing)}")
    optional_missing = {k: v for k, v in result.missing_counts.items() if k not in REQUIRED_FIELDS}
    if optional_missing:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(optional_missing.items()))
        print(f"Optional fields stored as null: {detail}")
    print(f"Duplicate programme URLs: {len(result.duplicate_urls)}")
    for url, positions in result.duplicate_urls.items():
        print(f"  - {url} (cards #{', #'.join(map(str, positions))})")
    print(f"Output file: {output}")
    print(f"Status: {status}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract Mastersportal programme cards to JSON.")
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT,
                        help="Saved Mastersportal HTML file (default: the UK search page download)")
    parser.add_argument("-o", "--output", type=Path,
                        help="Output JSON path (default: programs.json next to the input file)")
    args = parser.parse_args(argv)

    input_path: Path = args.input.expanduser()
    output: Path = (args.output or input_path.with_name("programs.json")).expanduser()

    try:
        html = read_html(input_path)
    except FileNotFoundError:
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        return 1
    except PermissionError:
        print(f"Error: permission denied reading: {input_path}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Error: could not read {input_path}: {exc}", file=sys.stderr)
        return 1

    try:
        result = extract_programs(html)
    except Exception as exc:  # malformed markup that even the parser cannot handle
        print(f"Error: could not parse HTML: {exc}", file=sys.stderr)
        return 1

    if result.cards_found == 0:
        print(f"Error: no '{CARD_SELECTOR}' elements found in {input_path.name}. "
              "Is this a rendered Mastersportal search page?", file=sys.stderr)
        return 2

    try:
        write_json_atomic(result.records, output)
    except PermissionError:
        print(f"Error: permission denied writing: {output}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Error: could not write {output}: {exc}", file=sys.stderr)
        return 1

    warnings = result.missing_required or result.duplicate_urls or result.skipped_cards
    print_summary(input_path, output, result, "Completed with warnings" if warnings else "Completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
