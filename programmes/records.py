"""Combine listing-card and detail-page data into one programme record."""

from __future__ import annotations

import hashlib
import re
import unicodedata

LISTING_FIELDS = ("university_name", "university_location", "program_name", "program_type", "cost",
                  "program_url", "listing_duration")
DETAIL_FIELDS = ("university_name", "university_location", "university_profile_url", "program_name",
                 "degree_type", "tuition_fee", "tuition_fee_details", "delivery", "official_url",
                 "duration", "duration_notes", "apply_date", "application_deadline_notes", "intakes",
                 "start_dates", "field_of_study", "fields_of_study", "top_ranking_percent", "ranking",
                 "language_score", "language_score_details", "credits", "academic_requirements",
                 "other_requirements")
# Order of keys in the output JSON.
RECORD_FIELDS = ("university_name", "university_location", "program_name", "program_type", "cost",
                 "program_url", "degree_type", "tuition_fee", "tuition_fee_details", "delivery",
                 "official_url", "duration", "duration_notes", "apply_date", "application_deadline_notes",
                 "intakes", "start_dates", "field_of_study", "fields_of_study", "top_ranking_percent",
                 "ranking", "language_score", "language_score_details", "credits", "academic_requirements",
                 "other_requirements")
EMPTY_DETAIL = {
    "tuition_fee": {"international": None, "domestic": None},
    "language_score": {"duolingo": None, "toefl": None, "pte": None, "ielts": None},
    "academic_requirements": {"gpa": None, "gmat": None, "gre": None, "note": None},
}


def university_key(name: str | None) -> str | None:
    """Grouping key so "University of X", "university of x" and "The University of X" share a file."""
    if not name:
        return None
    text = unicodedata.normalize("NFKC", name).casefold().replace("&", " and ")
    text = re.sub(r"[^\w]+", " ", text).strip()
    text = re.sub(r"^the ", "", text)
    return text or None


def fallback_programme_key(university: str | None, program: str | None) -> str:
    """Stable key for listing cards that have no programme link."""
    digest = hashlib.sha1(f"{university_key(university)}|{(program or '').casefold()}".encode()).hexdigest()
    return f"nolink-{digest[:16]}"


def merge_values(old: dict | None, new: dict, fields) -> tuple[dict, list[str]]:
    """New non-null values win; a null in a newer page never erases an earlier non-null value."""
    merged = dict(old or {})
    kept = []
    for name in fields:
        value = new.get(name)
        if value is None or value == {} or value == []:
            if merged.get(name) is not None:
                kept.append(name)
            merged.setdefault(name, None)
        else:
            merged[name] = value
    return merged, kept


def build_record(category: str, study_id: str, listing: dict | None, detail: dict | None,
                 provenance: dict, warnings: list[str]) -> dict:
    listing = listing or {}
    detail = detail or {}
    record: dict = {}
    for name in RECORD_FIELDS:
        if name in ("university_name", "university_location", "program_name"):
            record[name] = listing.get(name) or detail.get(name)
        elif name in LISTING_FIELDS:
            record[name] = listing.get(name)
        else:
            value = detail.get(name)
            record[name] = value if value is not None else EMPTY_DETAIL.get(name)
            if isinstance(record[name], dict) and name in EMPTY_DETAIL:
                record[name] = {**EMPTY_DETAIL[name], **record[name]}

    if listing and detail:
        status = "complete"
    elif detail:
        status = "detail_only"
    else:
        status = "listing_only"
    record.update({
        "study_id": None if study_id.startswith("nolink-") else study_id,
        "category": category,
        "detail_status": status,
        "listing_duration": listing.get("listing_duration"),
        "extraction_warnings": sorted(set(warnings)),
        "source": provenance,
    })
    return record
