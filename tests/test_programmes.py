"""Tests for the programme importer (parsers, storage, task lifecycle and dashboard API)."""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

import pytest

from programmes.classify import KIND_BLOCKED, KIND_DETAIL, KIND_LISTING, KIND_UNKNOWN, classify, make_soup
from programmes.config import ImportSettings
from programmes.db import Store
from programmes.detail import _period, extract_credits, parse_detail
from programmes.listing import parse_listing
from programmes.pagination import (PaginationError, canonical_programme_url, page_number_of, page_url, page_urls,
                                   strip_tracking, validate_listing_url)
from programmes.records import merge_values, university_key
from programmes.sources import PORTALS
from programmes.storage import safe_filename
from programmes.tasks import ImportManager, TaskError

FIXTURES = Path(__file__).parent / "fixtures" / "portal"
MASTER_LISTING = FIXTURES / "master_listing_uk.html"
MASTER_DETAIL = FIXTURES / "master_detail_brighton_management.html"
BACHELOR_LISTING = FIXTURES / "bachelor_listing_uk.html"
BACHELOR_DETAIL = FIXTURES / "bachelor_detail_kingston_geography.html"
BLOCKED = FIXTURES / "blocked_cloudflare.html"
MASTER, BACHELOR = PORTALS["master"], PORTALS["bachelor"]


def parse(path: Path):
    html = path.read_text(encoding="utf-8")
    soup = make_soup(html)
    return classify(html, soup), soup


@pytest.fixture(scope="module")
def master_detail():
    info, soup = parse(MASTER_DETAIL)
    return parse_detail(soup, info.portal, info.source_url)


@pytest.fixture(scope="module")
def bachelor_detail():
    info, soup = parse(BACHELOR_DETAIL)
    return parse_detail(soup, info.portal, info.source_url)


# --- pagination -------------------------------------------------------------------------------------

def test_page_one_has_no_page_parameter():
    assert page_url(MASTER.base_listing_url, 1) == MASTER.base_listing_url


def test_later_pages_use_page_parameter():
    assert page_url(MASTER.base_listing_url, 2) == MASTER.base_listing_url + "?page=2"
    assert page_urls(MASTER.base_listing_url, 3)[-1].endswith("?page=3")
    assert len(page_urls(MASTER.base_listing_url, 985)) == 985


def test_page_url_replaces_existing_page_and_keeps_other_params():
    base = "https://www.mastersportal.com/search/master/united-kingdom?bot=true&page=7&sort=a"
    assert page_url(base, 3) == "https://www.mastersportal.com/search/master/united-kingdom?sort=a&page=3"
    assert page_url(base, 1) == "https://www.mastersportal.com/search/master/united-kingdom?sort=a"


@pytest.mark.parametrize("value", [0, -1, "3", 2.5, None])
def test_invalid_max_pagination_is_rejected(value):
    with pytest.raises(PaginationError):
        page_urls(MASTER.base_listing_url, value)


def test_listing_url_must_match_category_domain():
    assert validate_listing_url(MASTER.base_listing_url + "/", MASTER) == MASTER.base_listing_url
    with pytest.raises(PaginationError):
        validate_listing_url(BACHELOR.base_listing_url, MASTER)
    with pytest.raises(PaginationError):
        validate_listing_url("https://evil.example/search/master", MASTER)
    with pytest.raises(PaginationError):
        validate_listing_url("https://www.mastersportal.com/studies/1/x.html", MASTER)


def test_url_helpers():
    assert page_number_of("https://www.mastersportal.com/search/master/united-kingdom?page=4") == 4
    assert page_number_of("https://www.mastersportal.com/search/master/united-kingdom") == 1
    assert canonical_programme_url("https://mastersportal.com/studies/11264/management.html?bot=true#x") == \
        "https://www.mastersportal.com/studies/11264/management.html"
    assert strip_tracking("https://x.ac.uk/c?utm_source=a&id=3&utm_medium=b") == "https://x.ac.uk/c?id=3"


# --- classification and listing parser --------------------------------------------------------------

@pytest.mark.parametrize("path, kind, category", [
    (MASTER_LISTING, KIND_LISTING, "master"), (BACHELOR_LISTING, KIND_LISTING, "bachelor"),
    (MASTER_DETAIL, KIND_DETAIL, "master"), (BACHELOR_DETAIL, KIND_DETAIL, "bachelor"),
])
def test_classify_real_pages(path, kind, category):
    info, _ = parse(path)
    assert info.kind == kind
    assert info.portal.category == category


def test_classify_listing_page_numbers():
    info, _ = parse(MASTER_LISTING)
    assert (info.page_number, info.total_pages) == (1, 985)
    page2 = MASTER_LISTING.read_text(encoding="utf-8").replace('data-current-page="1"', 'data-current-page="2"')
    assert classify(page2).page_number == 2


def test_classify_blocked_and_unknown_pages():
    info, _ = parse(BLOCKED)
    assert info.kind == KIND_BLOCKED and "save it again" in info.reason
    assert classify("<html><head><title>Hello</title></head><body>hi</body></html>").kind == KIND_UNKNOWN
    assert classify("not html at all \x00\x01").kind == KIND_UNKNOWN


def test_saved_from_comment_gives_source_url():
    html = ("<!-- saved from url=(0070)https://www.mastersportal.com/search/master/united-kingdom?page=3 -->\n"
            + MASTER_LISTING.read_text(encoding="utf-8").replace(' data-current-page="1"', ""))
    info = classify(html)
    assert info.source_url.endswith("?page=3") and info.page_number == 3


@pytest.mark.parametrize("path, portal, first", [
    (MASTER_LISTING, MASTER, ("11264", "University of Brighton", "Management", "Master", "Brighton, United Kingdom")),
    (BACHELOR_LISTING, BACHELOR, ("256576", "Kingston University", "Geography (Hons)", "B.Sc.", "London, United Kingdom")),
])
def test_listing_parser(path, portal, first):
    _, soup = parse(path)
    result = parse_listing(soup, portal)
    assert result.cards_found == 20 and len(result.cards) == 20
    card = result.cards[0]
    assert (card.study_id, card.university_name, card.program_name, card.program_type,
            card.university_location) == first
    assert card.program_url.startswith(f"https://{portal.host}/studies/") and "bot=" not in card.program_url
    assert card.cost and card.cost.startswith("BDT")


# --- detail parser ----------------------------------------------------------------------------------

def test_master_detail_fields(master_detail):
    d = master_detail.data
    assert master_detail.study_id == "11264" and master_detail.warnings == []
    assert d["university_name"] == "University of Brighton"
    assert d["degree_type"] == "MSc, PGCert, PGDip"
    assert d["delivery"] == "On Campus"
    assert d["tuition_fee"]["international"] == {"amount": 18792, "currency": "GBP", "per": "year",
                                                 "note": "Non-residents"}
    assert d["tuition_fee"]["domestic"]["amount"] == 11502
    assert d["tuition_fee_details"] == "Placement year: 1,958 GBP"
    assert d["official_url"] == "https://www.brighton.ac.uk/courses/study/management-msc-pgcert-pgdip.aspx"
    assert d["duration"] == {"Full-time": "12 months", "Part-time": "24 months"}
    assert d["duration_notes"] == {"Part-time": "2–3 years"}
    assert d["apply_date"] == "Anytime"
    assert d["start_dates"] == ["January 2027", "September 2027"]
    assert d["intakes"][1]["start_date"] == "2027-09-28"
    assert d["field_of_study"] == "Management Studies"
    assert d["top_ranking_percent"] == 15 and d["ranking"]["scope"] == "worldwide"
    assert d["language_score"] == {"duolingo": 125, "toefl": 85, "pte": 59, "ielts": 6.5}
    assert d["credits"] == {"amount": 180, "system": None, "note": "180 alternative credits"}
    assert d["academic_requirements"]["gpa"] == "Lower Second Class"
    assert d["academic_requirements"]["gmat"] == "Required"
    assert any("2:2 honours" in r for r in d["other_requirements"])


def test_bachelor_detail_fields(bachelor_detail):
    d = bachelor_detail.data
    assert bachelor_detail.study_id == "256576"
    assert d["degree_type"] == "B.Sc."
    assert d["tuition_fee"]["international"]["amount"] == 20500
    assert d["tuition_fee"]["domestic"] == {"amount": 10050, "currency": "GBP", "per": "year",
                                            "note": "Citizens or residents"}
    assert d["apply_date"] == "Apply before Jan 2027"
    assert d["intakes"][0]["deadlines"] == [{"text": "Apply before Jan 2027", "date": "2027-01-13"}]
    assert d["duration_notes"]["Full-time"] == "4 years with a foundation or professional placement"
    # TOEFL lists the 0-120 total and the 1-6 band; the total is the headline score.
    assert d["language_score"] == {"duolingo": None, "toefl": 80, "pte": 59, "ielts": 6}
    assert d["language_score_details"]["toefl"]["scores"] == [80, 4]
    assert d["language_score_details"]["toefl"]["scales"] == ["0-120", "1-6 band"]
    assert d["academic_requirements"]["gpa"] is None
    assert "not aware" in d["academic_requirements"]["note"]
    assert d["official_url"] == "https://www.kingston.ac.uk/study/undergraduate/geography-bsc-hons"


def test_detail_with_missing_sections_uses_nulls():
    html = """<html><head><link rel="canonical" href="https://www.mastersportal.com/studies/5/x.html">
    <script>window.PortalType = 'master';</script></head><body>
    <h1 class="StudyTitleWrapper"><span class="StudyTitle"><a title="Data Science">Data Science</a></span>
    <span class="OrganisationName"><a href="/universities/9/u.html">Example University</a></span></h1>
    <div id="StudyKeyFacts"></div></body></html>"""
    info = classify(html)
    assert info.kind == KIND_DETAIL
    result = parse_detail(make_soup(html), info.portal, info.source_url)
    d = result.data
    assert d["program_name"] == "Data Science" and d["university_name"] == "Example University"
    assert d["tuition_fee"] == {"international": None, "domestic": None}
    for key in ("official_url", "duration", "apply_date", "start_dates", "top_ranking_percent", "credits"):
        assert d[key] is None
    assert d["language_score"] == {"duolingo": None, "toefl": None, "pte": None, "ielts": None}
    assert any("tuition" in w for w in result.warnings)


def test_fee_period_and_credit_variants():
    assert _period("/ year") == "year"
    assert _period("/ semester") == "semester"
    assert _period("full programme") == "full programme"
    html = '<div id="StudyKeyFacts"><article class="FactItem"><h3 class="FactItemTitle">Credits</h3>' \
           '<div class="FactItemInformation">90 ECTS</div></article></div>'
    assert extract_credits(make_soup(html)) == {"amount": 90, "system": "ECTS", "note": "90 ECTS"}


# --- storage helpers --------------------------------------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("University of Brighton", "University of Brighton.json"),
    ('A/B: "C"?', "A-B- -C.json"),
    ("CON", "_CON.json"),
    ("lpt1.uni", "_lpt1.uni.json"),
    ("Trailing dots...", "Trailing dots.json"),
    ("", "unnamed.json"),
])
def test_safe_filename(name, expected):
    assert safe_filename(name) == expected


def test_safe_filename_length_limit():
    assert len(safe_filename("x" * 500)) <= 125


def test_university_key_normalisation():
    assert university_key("The University of Brighton") == university_key("university  of brighton")
    assert university_key("A & B") == university_key("A and B")


def test_merge_never_erases_with_null():
    merged, kept = merge_values({"cost": "BDT 1", "program_name": "Old"}, {"cost": None, "program_name": "New"},
                                ("cost", "program_name"))
    assert merged == {"cost": "BDT 1", "program_name": "New"} and kept == ["cost"]


# --- import pipeline --------------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path):
    settings = ImportSettings(data_dir=tmp_path / "data", output_root=tmp_path / "out",
                              import_root=tmp_path / "imports")
    manager = ImportManager(settings)
    yield manager, settings
    manager.store.close()


def drop(folder: Path, *sources: Path, names: list[str] | None = None) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, source in enumerate(sources):
        target = folder / (names[index] if names else source.name)
        shutil.copyfile(source, target)
        paths.append(target)
    return paths


def run(manager: ImportManager, category: str, folder: Path, delete_html: bool = True) -> dict:
    task = manager.create(category, str(folder), delete_html)
    manager.wait(task["id"])
    return manager.snapshot(task["id"])


def university_file(settings: ImportSettings, category: str, name: str) -> dict:
    return json.loads((settings.output_root / "UK" / category / "programs" / name).read_text(encoding="utf-8"))


def test_import_listing_and_detail_creates_complete_record(env):
    manager, settings = env
    folder = settings.import_folder("master")
    listing, detail = drop(folder, MASTER_LISTING, MASTER_DETAIL)
    task = run(manager, "master", folder)

    assert task["status"] == "completed"
    assert task["file_counts"] == {"done": 2}
    assert (task["listing_done"], task["detail_done"]) == (1, 1)
    assert task["cards_found"] == 20 and task["programmes_created"] == 20

    data = university_file(settings, "master", "University of Brighton.json")
    record = next(r for r in data["programs"] if r["study_id"] == "11264")
    assert record["detail_status"] == "complete"
    assert record["program_type"] == "Master" and record["cost"].startswith("BDT")
    assert record["degree_type"] == "MSc, PGCert, PGDip"
    assert record["tuition_fee"]["international"]["amount"] == 18792
    assert record["source"]["listing"]["page"] == 1 and record["source"]["detail"]["file"] == detail.name
    assert data["programme_count"] == len(data["programs"])

    index = university_file(settings, "master", "_index.json")
    assert index["programme_count"] == 20
    # Both source files were imported and verified, so they are cleaned up.
    assert not listing.exists() and not detail.exists() and task["html_deleted"] == 2
    stats = manager.store.category_stats("master")
    assert stats["programmes"] == 20 and stats["complete"] == 1 and stats["listing_only"] == 19


def test_custom_export_folder(env, tmp_path):
    manager, settings = env
    folder = settings.import_folder("master")
    drop(folder, MASTER_LISTING)
    export = tmp_path / "my-export"
    export.mkdir()
    task = manager.create("master", str(folder), False, str(export))
    manager.wait(task["id"])
    task = manager.snapshot(task["id"])
    assert task["status"] == "completed" and task["output_dir"] == str(export.resolve())
    assert (export / "University of Brighton.json").exists()
    index = json.loads((export / "_index.json").read_text(encoding="utf-8"))
    assert index["programme_count"] == 20
    assert all((export / u["file"]).exists() for u in index["universities"])
    assert not (settings.output_root / "UK" / "master" / "programs").exists()


def test_export_folder_must_exist(env, tmp_path):
    manager, settings = env
    drop(settings.import_folder("master"), MASTER_LISTING)
    for bad in (str(tmp_path / "missing"), "relative\\folder"):
        with pytest.raises(TaskError, match="Export folder"):
            manager.create("master", "", False, bad)


def test_default_export_folder_is_created(env):
    manager, settings = env
    drop(settings.import_folder("master"), MASTER_LISTING)
    task = manager.create("master", "", False, "")
    manager.wait(task["id"])
    default = settings.output_root / "UK" / "master" / "programs"
    assert manager.snapshot(task["id"])["output_dir"] == str(default.resolve())
    assert (default / "University of Brighton.json").exists()


def test_old_database_gets_output_dir_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, category TEXT NOT NULL, folder TEXT NOT NULL, "
                 "delete_html INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'queued', "
                 "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("INSERT INTO tasks VALUES ('t1', 'master', 'x', 1, 'completed', 'finished', 'a', 'b')")
    conn.commit()
    conn.close()
    store = Store(path)
    assert store.get_task("t1")["output_dir"] is None
    store.close()


def test_export_folder_api(client, tmp_path):
    http, _, settings = client
    default = http.get("/api/programmes/master/export-folder", query_string={"path": ""}).get_json()
    assert default["is_default"] and default["folder"].endswith("programs")
    custom = http.get("/api/programmes/master/export-folder", query_string={"path": str(tmp_path)}).get_json()
    assert custom["exists"] and not custom["is_default"]
    missing = http.get("/api/programmes/master/export-folder", query_string={"path": str(tmp_path / "nope")})
    assert missing.status_code == 400
    remote = http.get("/api/programmes/master/export-folder", query_string={"path": str(tmp_path)},
                      environ_base={"REMOTE_ADDR": "192.168.1.50"})
    assert remote.status_code == 403
    page = http.get("/programmes").get_data(as_text=True)
    assert 'id="export-folder"' in page and "Export folder" in page and "export-folder.js" in page


def test_keep_html_option(env):
    manager, settings = env
    folder = settings.import_folder("bachelor")
    (path,) = drop(folder, BACHELOR_DETAIL)
    task = run(manager, "bachelor", folder, delete_html=False)
    assert task["status"] == "completed" and path.exists() and task["html_deleted"] == 0
    record = university_file(settings, "bachelor", "Kingston University.json")["programs"][0]
    assert record["detail_status"] == "detail_only" and record["program_type"] is None


def test_wrong_category_blocked_and_unknown_files_are_reported_not_deleted(env):
    manager, settings = env
    folder = settings.import_folder("master")
    bachelor, blocked = drop(folder, BACHELOR_DETAIL, BLOCKED)
    (folder / "notes.html").write_text("<html><body>my notes</body></html>", encoding="utf-8")
    task = run(manager, "master", folder)

    assert task["status"] == "completed_with_errors"
    assert task["file_counts"] == {"skipped": 2, "blocked": 1}
    files = {f["name"]: f for f in manager.store.files(task["id"])}
    assert "Bachelor's page" in files[bachelor.name]["reason"]
    assert files[blocked.name]["status"] == "blocked"
    assert bachelor.exists() and blocked.exists() and (folder / "notes.html").exists()
    assert not (settings.output_root / "UK" / "master" / "programs" / "Kingston University.json").exists()


def test_retry_after_resaving_blocked_page(env):
    manager, settings = env
    folder = settings.import_folder("master")
    (blocked,) = drop(folder, BLOCKED, names=["management.html"])
    task = run(manager, "master", folder)
    assert task["status"] == "completed_with_errors" and task["can_retry"]

    shutil.copyfile(MASTER_DETAIL, blocked)  # the user saves the page again from the browser
    manager.retry_failed(task["id"])
    manager.wait(task["id"])
    task = manager.snapshot(task["id"])
    assert task["status"] == "completed" and task["retries"] == 1 and task["file_counts"] == {"done": 1}


def test_duplicate_file_content_is_skipped(env):
    manager, settings = env
    folder = settings.import_folder("master")
    drop(folder, MASTER_DETAIL, MASTER_DETAIL, names=["a.html", "b.html"])
    task = run(manager, "master", folder, delete_html=False)
    assert task["file_counts"] == {"done": 1, "skipped": 1}
    assert manager.store.category_stats("master")["programmes"] == 1


def test_reimport_updates_without_duplicates(env):
    manager, settings = env
    folder = settings.import_folder("master")
    drop(folder, MASTER_LISTING)
    run(manager, "master", folder, delete_html=False)
    second = run(manager, "master", folder, delete_html=False)
    assert second["programmes_created"] == 0 and second["programmes_updated"] == 20
    data = university_file(settings, "master", "University of Brighton.json")
    ids = [r["study_id"] for r in data["programs"]]
    assert len(ids) == len(set(ids))
    assert manager.store.category_stats("master")["programmes"] == 20


def test_records_added_outside_the_tool_are_preserved(env):
    manager, settings = env
    out = settings.output_root / "UK" / "master" / "programs"
    out.mkdir(parents=True)
    manual = {"program_name": "Hand-entered", "program_url": "https://example.ac.uk/manual"}
    (out / "University of Brighton.json").write_text(json.dumps({"programs": [manual]}), encoding="utf-8")
    drop(settings.import_folder("master"), MASTER_DETAIL)
    run(manager, "master", settings.import_folder("master"))
    names = {r["program_name"] for r in university_file(settings, "master", "University of Brighton.json")["programs"]}
    assert names == {"Hand-entered", "Management"}


def test_unreadable_existing_json_is_not_overwritten(env):
    manager, settings = env
    out = settings.output_root / "UK" / "master" / "programs"
    out.mkdir(parents=True)
    (out / "University of Brighton.json").write_text("{broken", encoding="utf-8")
    (source,) = drop(settings.import_folder("master"), MASTER_DETAIL)
    task = run(manager, "master", settings.import_folder("master"))
    assert task["status"] == "completed_with_errors" and task["file_counts"] == {"failed": 1}
    assert (out / "University of Brighton.json").read_text(encoding="utf-8") == "{broken"
    assert source.exists()  # never delete a page whose data was not written


def test_empty_folder(env):
    manager, settings = env
    task = run(manager, "master", settings.import_folder("master"))
    assert task["status"] == "empty"


def test_invalid_folder_and_category(env):
    manager, settings = env
    with pytest.raises(TaskError):
        manager.create("master", str(settings.import_root / "missing"), True)
    with pytest.raises(TaskError):
        manager.create("phd", "", True)


class Gate:
    """Hold the worker inside a file so the test can pause/stop it at a known point."""

    def __init__(self, manager: ImportManager):
        self.entered = threading.Event()
        self.release = threading.Event()
        original = manager._import_file

        def gated(task, item):
            self.entered.set()
            assert self.release.wait(10)
            return original(task, item)

        manager._import_file = gated


def wait_for(manager, task_id, *statuses, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = manager.snapshot(task_id)
        if task["status"] in statuses:
            return task
        time.sleep(0.02)
    raise AssertionError(f"task stayed {manager.snapshot(task_id)['status']}")


def test_pause_resume_and_duplicate_task_prevention(env):
    manager, settings = env
    folder = settings.import_folder("master")
    drop(folder, MASTER_LISTING, MASTER_DETAIL)
    gate = Gate(manager)
    task = manager.create("master", str(folder), False)
    assert gate.entered.wait(10)

    with pytest.raises(TaskError) as conflict:
        manager.create("master", str(folder), False)
    assert conflict.value.http_status == 409

    manager.pause(task["id"])
    gate.release.set()
    paused = wait_for(manager, task["id"], "paused")
    assert paused["file_counts"] == {"done": 1, "pending": 1}

    manager.resume(task["id"])
    manager.wait(task["id"])
    assert manager.snapshot(task["id"])["status"] == "completed"


def test_stop_then_resume_continues_from_checkpoint(env):
    manager, settings = env
    folder = settings.import_folder("master")
    drop(folder, MASTER_LISTING, MASTER_DETAIL)
    gate = Gate(manager)
    task = manager.create("master", str(folder), False)
    assert gate.entered.wait(10)
    manager.stop(task["id"])
    gate.release.set()
    manager.wait(task["id"])
    stopped = manager.snapshot(task["id"])
    assert stopped["status"] == "stopped" and stopped["file_counts"] == {"done": 1, "pending": 1}

    manager.resume(task["id"])
    manager.wait(task["id"])
    done = manager.snapshot(task["id"])
    assert done["status"] == "completed" and done["file_counts"] == {"done": 2}


def test_restart_recovery_marks_interrupted_and_resumes(tmp_path):
    settings = ImportSettings(data_dir=tmp_path / "data", output_root=tmp_path / "out",
                              import_root=tmp_path / "imports")
    folder = settings.import_folder("master")
    drop(folder, MASTER_DETAIL)
    store = Store(settings.db_path)
    store.create_task("crashed1", "master", str(folder), False)
    store.update_task("crashed1", status="running", scanned=1, files_total=1, detail_total=1)
    store.add_file("crashed1", str(folder / MASTER_DETAIL.name), MASTER_DETAIL.name, 1, KIND_DETAIL, None)
    store.execute("UPDATE task_files SET status = 'processing'")
    store.close()

    manager = ImportManager(settings)  # simulates the app starting again
    task = manager.snapshot("crashed1")
    assert task["status"] == "interrupted" and task["file_counts"] == {"pending": 1} and task["can_resume"]
    manager.resume("crashed1")
    manager.wait("crashed1")
    assert manager.snapshot("crashed1")["status"] == "completed"
    manager.store.close()


# --- dashboard API ----------------------------------------------------------------------------------

@pytest.fixture
def client(env):
    from app import create_app

    manager, settings = env
    app = create_app(import_manager=manager)
    return app.test_client(), manager, settings


def test_dashboard_page_and_overview(client):
    http, manager, _ = client
    assert http.get("/programmes").status_code == 200
    assert b"Programme importer" in http.get("/").data
    body = http.get("/api/programmes/overview").get_json()
    assert set(body["categories"]) == {"master", "bachelor"}


def test_api_task_lifecycle_and_exports(client):
    http, manager, settings = client
    drop(settings.import_folder("master"), MASTER_LISTING)
    response = http.post("/api/programmes/tasks", json={"category": "master", "folder": "", "delete_html": False})
    assert response.status_code == 202
    task_id = response.get_json()["task"]["id"]
    manager.wait(task_id)

    task = http.get(f"/api/programmes/tasks/{task_id}").get_json()["task"]
    assert task["status"] == "completed"
    assert http.get(f"/api/programmes/tasks/{task_id}/files").get_json()["files"][0]["status"] == "done"
    events = http.get(f"/api/programmes/tasks/{task_id}/events").get_json()["events"]
    assert any("20 programme card(s)" in e["message"] for e in events)

    log = http.get(f"/api/programmes/tasks/{task_id}/log")
    assert "attachment" in log.headers["Content-Disposition"] and log.get_json()["task"]["id"] == task_id
    assert http.get(f"/api/programmes/tasks/{task_id}/log?format=csv").data.startswith(b"time,level")

    missing = http.get("/api/programmes/master/missing-details").get_json()
    assert missing["count"] == 20
    pages = http.get("/api/programmes/master/listing-pages?max_page=5").get_json()
    assert [p["page"] for p in pages["pages"]] == [2, 3, 4, 5]
    assert pages["pages"][0]["url"].endswith("?page=2")
    text = http.get("/api/programmes/master/missing-details?format=txt").data.decode()
    assert text.count("https://www.mastersportal.com/studies/") == 20

    assert http.get("/api/programmes/tasks").get_json()["tasks"][0]["id"] == task_id
    assert http.post(f"/api/programmes/tasks/{task_id}/pause").status_code == 409
    assert http.post(f"/api/programmes/tasks/{task_id}/retry").status_code == 409


def test_api_errors(client):
    http, _, settings = client
    assert http.get("/api/programmes/tasks/nope").status_code == 404
    bad = http.post("/api/programmes/tasks", json={"category": "master", "folder": str(settings.import_root / "x")})
    assert bad.status_code == 400 and "not found" in bad.get_json()["error"]
    assert http.get("/api/programmes/phd/missing-details").status_code == 404
    wrong = http.get("/api/programmes/master/listing-pages?base_url=https://www.bachelorsportal.com/search/bachelor/x")
    assert wrong.status_code == 400
