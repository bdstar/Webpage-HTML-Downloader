"""Programme detail page parser (identical markup on Mastersportal and Bachelorsportal)."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

from .pagination import canonical_programme_url, strip_tracking
from .sources import Portal
from .text import clean_text, parse_amount, parse_number, visible_text

NOISE = ".InfoIcon, .Tooltip, .LockIcon, .FactListTitleArrow, sup"
DELIVERY_WORDS = re.compile(r"^(on[- ]?campus|online|blended|distance( learning)?|hybrid)$", re.IGNORECASE)
TOP_PERCENT = re.compile(r"top\s*(\d+(?:\.\d+)?)\s*%", re.IGNORECASE)
TRACKING_HOSTS = ("prtl.co", "studyportals", "mastersportal.com", "bachelorsportal.com",
                  "studyportalstracking.com")
PERIODS = {"year": "year", "years": "year", "annum": "year", "annual": "year", "semester": "semester",
           "term": "term", "month": "month", "module": "module", "credit": "credit",
           "full": "full programme", "programme": "full programme", "program": "full programme",
           "course": "full programme"}
LANGUAGE_KEYS = (("duolingo", "duolingo"), ("toefl", "toefl"), ("pte", "pte"), ("ielts", "ielts"))


@dataclass
class DetailResult:
    study_id: str | None
    program_url: str | None
    data: dict
    warnings: list[str] = field(default_factory=list)


def _text(element: Tag | None) -> str | None:
    return visible_text(element, exclude_selector=NOISE)


def _fact(soup: BeautifulSoup, title: str) -> Tag | None:
    """``article.FactItem`` in the key-facts box whose heading starts with ``title``."""
    for article in soup.select("#StudyKeyFacts article.FactItem"):
        heading = clean_text(article.select_one(".FactItemTitle").get_text(" ")) if article.select_one(
            ".FactItemTitle") else ""
        if heading and heading.lower().startswith(title.lower()):
            return article
    return None


def _plural(amount: int | float, unit: str) -> str:
    unit = unit.rstrip("s") if amount == 1 else (unit if unit.endswith("s") else unit + "s")
    amount_text = int(amount) if float(amount).is_integer() else amount
    return f"{amount_text} {unit}"


# --- header -------------------------------------------------------------------------------------

def extract_program_name(soup) -> str | None:
    link = soup.select_one("h1.StudyTitleWrapper .StudyTitle a[title]") or soup.select_one(
        "h1 .StudyName a[title]")
    if link is not None:
        return clean_text(link.get("title")) or _text(link)
    return _text(soup.select_one("h1.StudyTitleWrapper .StudyTitle"))


def extract_university(soup) -> tuple[str | None, str | None]:
    """University name and its portal profile path (``/universities/<id>/<slug>.html``)."""
    link = soup.select_one("h1 .OrganisationName a") or soup.select_one(".OrganisationName a")
    if link is None:
        return None, None
    return _text(link), clean_text(link.get("href"))


def extract_location(soup) -> str | None:
    items = [_text(i) for i in soup.select(".OrganisationLocationInfoLinks .LocationItem")]
    items = [i for i in items if i]
    if items:
        return ", ".join(items)
    campuses = [_text(li) for li in soup.select(".CampusLocationList li")] if soup.select_one(
        ".CampusLocationList") else []
    return campuses[0] if campuses else None


def extract_degree_and_delivery(soup) -> tuple[str | None, str | None]:
    tags = [_text(t) for t in soup.select(".DegreeTags .Tag")]
    tags = [t for t in tags if t]
    delivered = _text(_fact(soup, "Delivered").select_one(".FactItemInformation")) if _fact(
        soup, "Delivered") else None
    degree = next((t for t in tags if not DELIVERY_WORDS.match(t) and t != delivered), None)
    delivery = delivered or next((t for t in tags if DELIVERY_WORDS.match(t)), None)
    return degree, delivery


# --- fees ---------------------------------------------------------------------------------------

def _period(text: str | None) -> str | None:
    if not text:
        return None
    words = re.findall(r"[a-z]+", text.lower())
    for word in words:
        if word in PERIODS:
            return PERIODS[word]
    return clean_text(text.strip(" /"))


def _fee(item: Tag | None) -> dict | None:
    if item is None or item.select_one(".js-notAvailable"):
        return None
    base = item.select_one(".js-base-price-original[data-amount]")
    original = item.select_one(".js-currencyAmount")
    amount = parse_amount(base.get("data-amount")) if base else None
    if amount is None and original is not None:
        amount = parse_amount(original.get("data-original-amount") or original.get("data-amount"))
    if amount is None:
        return None
    currency_type = item.select_one(".CurrencyType[data-institution-currency]")
    currency = (_text(item.select_one(".LocalCurrency"))
                or (currency_type.get("data-institution-currency") if currency_type else None)
                or (original.get("data-currency") if original else None))
    period_text = _text(item.select_one(".PaymentPeriod")) or (original.get("data-period") if original else None)
    note = _text(item.select_one(".StudentType"))
    return {"amount": amount, "currency": currency, "per": _period(period_text), "note": note}


def extract_tuition(soup) -> tuple[dict, str | None]:
    container = soup.select_one("#TuitionFeeContainer")
    fees = {"international": None, "domestic": None}
    if container is None:
        return fees, None
    fees["international"] = _fee(container.select_one('li.ItemContainer[data-target="international"]'))
    fees["domestic"] = _fee(container.select_one('li.ItemContainer[data-target="national"]'))
    extra = container.select_one(".ExtraDescription")
    return fees, _describe(extra) if extra is not None else None


def _describe(block: Tag) -> str | None:
    """Flatten "<p>Heading</p><ul><li>a</li><li>b</li></ul>" into "Heading: a; b"."""
    segments: list[str] = []
    heading: str | None = None
    for child in block.find_all(["p", "ul", "ol"], recursive=False):
        if child.name == "p":
            if heading:
                segments.append(heading)
            heading = _text(child)
            continue
        items = "; ".join(t for t in (_text(li) for li in child.find_all("li")) if t)
        if items:
            segments.append(f"{heading.rstrip(':')}: {items}" if heading else items)
        heading = None
    if heading:
        segments.append(heading)
    return " | ".join(segments) or _text(block)


# --- links, ranking, disciplines ----------------------------------------------------------------

def extract_official_url(soup, study_id: str | None) -> tuple[str | None, str | None]:
    """Programme website from the portal's outbound links (``data-description``), tracking removed."""
    votes: Counter = Counter()
    fallback: Counter = Counter()
    for link in soup.select("a.ProgrammeWebsiteLink[data-description], a.ChampionButton[data-description]"):
        target = clean_text(link.get("data-description"))
        if not target or not target.startswith(("http://", "https://")):
            continue
        if any(host in target.split("/")[2] for host in TRACKING_HOSTS):
            continue
        try:
            tracking = json.loads(link.get("data-ga-tracking") or "{}")
        except ValueError:
            tracking = {}
        if tracking.get("category") == "Study" and (study_id is None or str(tracking.get("label")) == study_id):
            votes[target] += 1
        else:
            fallback[target] += 1
    if votes:
        return strip_tracking(votes.most_common(1)[0][0]), None
    if fallback:
        return strip_tracking(fallback.most_common(1)[0][0]), (
            "official_url taken from a generic outbound link; it may be the institution website")
    return None, None


def extract_ranking(soup) -> tuple[int | float | None, dict | None]:
    ranking = soup.select_one(".Ranking")
    if ranking is None:
        return None, None
    value = _text(ranking.select_one(".Value"))
    match = TOP_PERCENT.search(value or "")
    percent = parse_number(match.group(1)) if match else None
    context = {"text": value, "scope": _text(ranking.select_one(".Location")),
               "ranking_name": _text(ranking.select_one(".RankingName"))}
    return percent, context


def extract_disciplines(soup) -> list[str]:
    fact = _fact(soup, "Disciplines")
    names = []
    if fact is not None:
        for link in fact.select("a"):
            if "LandingPageLink" in (link.get("class") or []):
                continue
            name = clean_text(link.get("title")) or _text(link)
            if name and name not in names:
                names.append(name)
    if not names:
        crumb = soup.select_one("li.BreadCrumbsItem.DisciplineListItem a")
        name = (clean_text(crumb.get("title")) or _text(crumb)) if crumb else None
        if name:
            names.append(name)
    return names


# --- key facts ----------------------------------------------------------------------------------

def extract_duration(soup) -> tuple[dict | None, dict | None]:
    fact = _fact(soup, "Duration")
    if fact is None:
        return None, None
    durations, notes = {}, {}
    for item in fact.select("li.FactListItem"):
        mode = _text(item.select_one(".FactListTitle")) or "Duration"
        span = item.select_one("span.Duration")
        value = None
        if span is not None:
            amount = parse_amount(span.get("data-duration"))
            unit = clean_text(span.get("data-period"))
            value = _plural(amount, unit) if amount is not None and unit else _text(span)
        if value:
            durations[mode] = value
        tooltip = item.select_one(".DurationTooltip [data-tooltip-text]")
        note = clean_text(BeautifulSoup(tooltip["data-tooltip-text"], "html.parser").get_text(" ")) if tooltip else None
        if note:
            notes[mode] = note
    if not durations:
        text = _text(fact.select_one(".FactItemInformation"))
        if text:
            durations["Duration"] = text
    return durations or None, notes or None


def extract_intakes(soup) -> tuple[list[dict], str | None, str | None]:
    """Start dates with their deadlines, a human summary of the deadline, and extra notes."""
    intakes = []
    for item in soup.select("#js-StartdateContainer li.StartDateItem"):
        start_time = item.select_one(".StartDateItemTime time")
        start = _text(start_time) or _text(item.select_one(".StartDateItemTime"))
        start_iso = (start_time.get("datetime") or "")[:10] or None if start_time else None
        deadlines = []
        for deadline in item.select("ul.DeadlinesList li.ApplicationDeadline"):
            when = deadline.select_one("time")
            deadlines.append({"text": _text(deadline),
                              "date": (when.get("datetime") or "")[:10] or None if when else None})
        if not deadlines:
            text = _text(item.select_one(".Deadline"))
            if text:
                deadlines.append({"text": text, "date": None})
        anytime = any(re.search(r"apply any ?time", d["text"] or "", re.IGNORECASE) for d in deadlines)
        intakes.append({"start": start, "start_date": start_iso, "apply_anytime": anytime,
                        "deadlines": deadlines})

    texts = []
    for intake in intakes:
        for deadline in intake["deadlines"]:
            if deadline["text"] and deadline["text"] not in texts:
                texts.append(deadline["text"])
    if intakes and all(i["apply_anytime"] for i in intakes):
        summary = "Anytime"
    else:
        summary = "; ".join(texts) or None
    notes = _text(soup.select_one(".MoreDeadlineDetailsText"))
    return intakes, summary, notes


def extract_credits(soup) -> dict | None:
    fact = _fact(soup, "Credits")
    text = _text(fact.select_one(".FactItemInformation")) if fact else None
    if not text:
        return None
    upper = text.upper()
    system = "ECTS" if "ECTS" in upper else ("UK credits" if "CATS" in upper or "UK CREDIT" in upper else None)
    return {"amount": parse_number(text), "system": system, "note": text}


# --- requirements -------------------------------------------------------------------------------

def _card_scores(card: Tag) -> tuple[list, str | None]:
    container = card.select_one(".ScoreInformationContainer")
    scores = [_text(s) for s in card.select(".ScoreInformationContainer .Score")]
    scores = [s for s in scores if s]
    if not scores:
        text = _text(container)
        return [], text
    return scores, None


def extract_language(soup) -> tuple[dict, dict]:
    scores = {"duolingo": None, "toefl": None, "pte": None, "ielts": None}
    details: dict = {}
    cards = soup.select("#EnglishRequirements .EnglishCardContents") or soup.select(
        "#StudyKeyFacts .EnglishCardContents")
    for card in cards:
        name = clean_text((card.get("data-cy") or "").removeprefix("card-")) or _text(card.select_one(".Heading"))
        variant = _text(card.select_one(".SubHeading"))
        raw, text = _card_scores(card)
        values = [parse_number(s) for s in raw]
        key = next((k for needle, k in LANGUAGE_KEYS if needle in (name or "").lower()), None)
        entry = {"test": name, "variant": variant, "scores": values or None, "text": text}
        if key == "toefl" and values:
            is_ibt = variant is None or "ibt" in variant.replace(" ", "").lower()
            # Since 2026 TOEFL iBT reports a 1-6 band next to the 0-120 total; use the total.
            totals = [v for v in values if v is not None and v > 6]
            entry["scales"] = ["0-120" if v is not None and v > 6 else "1-6 band" for v in values]
            if is_ibt:
                scores["toefl"] = totals[0] if totals else values[0]
        elif key and values:
            scores[key] = values[0]
        details[key or (name or "other").lower()] = entry
    return scores, details


def extract_academic(soup) -> dict:
    result = {"gpa": None, "gmat": None, "gre": None, "note": None}
    article = soup.select_one("#AcademicRequirements")
    if article is None:
        return result
    for card in article.select(".CardContents"):
        heading = (_text(card.select_one(".Heading")) or "").lower()
        raw, text = _card_scores(card)
        value = raw[0] if raw else text
        for key in ("gpa", "gmat", "gre"):
            if heading.startswith(key):
                number = parse_number(value) if value and re.fullmatch(r"[\d.,\s]+", value) else None
                result[key] = number if number is not None else value
    if not any(result[k] for k in ("gpa", "gmat", "gre")):
        result["note"] = _text(article.select_one("p, .NotProvided, .Text")) or clean_text(
            _text(article).removeprefix("Academic requirements") if _text(article) else None)
    return result


def extract_other_requirements(soup) -> list[str]:
    content = soup.select_one("#OtherRequirements .OtherRequirementsContent")
    if content is None:
        return []
    items = [_text(li) for li in content.select("li, p")]
    return [i for i in items if i]


# --- entry point --------------------------------------------------------------------------------

def parse_detail(soup: BeautifulSoup, portal: Portal, source_url: str | None) -> DetailResult:
    program_url = canonical_programme_url(source_url, portal)
    study_id = program_url.split("/")[4] if program_url else None
    warnings: list[str] = []

    university, university_path = extract_university(soup)
    degree, delivery = extract_degree_and_delivery(soup)
    fees, fee_details = extract_tuition(soup)
    official_url, official_warning = extract_official_url(soup, study_id)
    percent, ranking = extract_ranking(soup)
    disciplines = extract_disciplines(soup)
    duration, duration_notes = extract_duration(soup)
    intakes, apply_summary, deadline_notes = extract_intakes(soup)
    language, language_details = extract_language(soup)

    start_dates = []
    for intake in intakes:
        if intake["start"] and intake["start"] not in start_dates:
            start_dates.append(intake["start"])

    data = {
        "university_name": university,
        "university_location": extract_location(soup),
        "university_profile_url": f"https://{portal.host}{university_path}" if university_path else None,
        "program_name": extract_program_name(soup),
        "degree_type": degree,
        "tuition_fee": fees,
        "tuition_fee_details": fee_details,
        "delivery": delivery,
        "official_url": official_url,
        "duration": duration,
        "duration_notes": duration_notes,
        "apply_date": apply_summary,
        "application_deadline_notes": deadline_notes,
        "intakes": intakes or None,
        "start_dates": start_dates or None,
        "field_of_study": disciplines[0] if disciplines else None,
        "fields_of_study": disciplines or None,
        "top_ranking_percent": percent,
        "ranking": ranking,
        "language_score": language,
        "language_score_details": language_details or None,
        "credits": extract_credits(soup),
        "academic_requirements": extract_academic(soup),
        "other_requirements": extract_other_requirements(soup) or None,
    }

    if official_warning:
        warnings.append(official_warning)
    for name in ("university_name", "program_name"):
        if not data[name]:
            warnings.append(f"detail page is missing {name}")
    if fees["international"] is None and fees["domestic"] is None:
        warnings.append("no tuition fee published on the detail page")
    if not official_url:
        warnings.append("no official programme website link on the detail page")
    if not program_url:
        warnings.append("could not determine the programme URL (no canonical link)")
    return DetailResult(study_id=study_id, program_url=program_url, data=data, warnings=warnings)
