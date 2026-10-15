"""Tests for the university importer (parser, records, task lifecycle, safe deletion and web page/API)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from programmes.classify import make_soup
from programmes.storage import safe_filename
from programmes.tasks import TaskError
from universities.categories import CATEGORIES
from universities.config import UniversitySettings
from universities.parser import official_url, parse_university
from universities.records import merge, validate_record
from universities.tasks import UniversityImportManager

FIXTURE = Path(__file__).parent / "fixtures" / "portal" / "university_southampton.html"
SOURCE = FIXTURE.read_text(encoding="utf-8")
MASTER, BACHELOR = CATEGORIES["master"], CATEGORIES["bachelor"]
EXPECTED = {
    "university": "University of Southampton",
    "location": "Southampton,",
    "country": "United Kingdom",
    "global_top": "2%",
    "worldwide_number": "96",
    "official_webiste": "https://www.southampton.ac.uk/",
    "number_master_program": "402",
    "qs_rank": "111",
    "institution_type": "Public",
}
REMOTE = {"REMOTE_ADDR": "192.168.1.50"}


def variant(*replacements: tuple[str, str]) -> str:
    html = SOURCE
    for old, new in replacements:
        assert old in html, old
        html = html.replace(old, new)
    return html


def other_university(name: str, uid: str, **changes: str) -> str:
    html = variant(("University of Southampton", name), ("/357/", f"/{uid}/"))
    for old, new in changes.items():
        html = html.replace(old, new)
    return html


def parse(html: str, category=MASTER):
    return parse_university(html, make_soup(html), category)


# --- extraction -------------------------------------------------------------------------------------

@pytest.mark.parametrize("field", list(EXPECTED))
def test_extracts_each_field(field):
    assert parse(SOURCE).data[field] == EXPECTED[field]


def test_page_metadata_and_no_warnings():
    page = parse(SOURCE)
    assert page.university_id == "357"
    assert page.portal.category == "master"
    assert page.warnings == [] and page.missing == []
    assert list(page.data) == list(MASTER.fields)


def test_name_excludes_premium_badge_tooltip():
    assert "verified partner" not in parse(SOURCE).data["university"]


def test_location_excludes_venue_count():
    data = parse(SOURCE).data
    assert "+9" not in data["location"] and "+9" not in data["country"]


@pytest.mark.parametrize("raw, expected", [
    ("https://www.southampton.ac.uk/?utm_campaign=studyportals&amp;utm_source=Studportals", "https://www.southampton.ac.uk/"),
    ("https://www.example.ac.uk/study?x=1&amp;amp;y=2#top", "https://www.example.ac.uk/study"),
    ("  http://example.ac.uk  ", "http://example.ac.uk/"),
    ("javascript:alert(1)", None), ("ftp://example.ac.uk/", None), ("", None), (None, None), ("not a url", None),
])
def test_official_url_decodes_entities_and_drops_query(raw, expected):
    assert official_url(raw) == expected


def test_tracking_href_is_never_used_as_official_website():
    html = variant(('data-description="https://www.southampton.ac.uk/?utm_campaign=studyportals&amp;utm_source='
                    'Studportals&amp;utm_medium=portal&amp;utm_term=all" ', ""))
    page = parse(html)
    assert page.data["official_webiste"] is None
    assert "official_webiste" in page.missing


def test_master_count_comes_from_studies_quick_fact_not_first_quick_fact():
    # The fixture lists Scholarships (89) before the Master's count (402).
    assert parse(SOURCE).data["number_master_program"] == "402"


def test_master_count_falls_back_to_key_statistics():
    html = variant(('<span class="QuickFactData">402</span>', '<span class="QuickFactData"></span>'))
    assert parse(html).data["number_master_program"] == "402"


def test_master_count_mismatch_is_reported():
    html = variant(('<span class="KeyStatisticText Figure">402</span>', '<span class="KeyStatisticText Figure">400</span>'))
    page = parse(html)
    assert page.data["number_master_program"] == "402"
    assert any("differs" in w for w in page.warnings)


def test_bachelor_category_uses_bachelor_count():
    html = variant(("mastersportal.com", "bachelorsportal.com"), ("PortalType = 'master'", "PortalType = 'bachelor'"),
                   (">Master's<", ">Bachelor's<"), (" Masters ", " Bachelors "))
    page = parse(html, BACHELOR)
    assert page.portal.category == "bachelor"
    assert page.data["number_bachelor_program"] == "402"
    assert "number_master_program" not in page.data


def test_qs_rank_is_taken_from_the_qs_section_only():
    assert parse(SOURCE).data["qs_rank"] == "111"  # US News (125) is listed first in the fixture


def test_missing_qs_rank_is_null_even_with_other_rankings():
    page = parse(variant(("QS World University Rankings", "Times Higher Education")))
    assert page.data["qs_rank"] is None
    assert "qs_rank" in page.missing
    assert page.data["worldwide_number"] == "96"  # not substituted


def test_conflicting_qs_rankings_use_first_and_warn():
    html = variant(("<h3 class=\"RankingName\">Academic Ranking of World Universities</h3>",
                    "<h3 class=\"RankingName\">QS Europe Rankings</h3>"))
    page = parse(html)
    assert page.data["qs_rank"] == "111"
    assert any("several QS rankings" in w for w in page.warnings)


def test_missing_values_are_null_with_warnings():
    html = variant(('<span class="GlobalRank">Global Rank Top 2%</span>', ""),
                   ('<span class="GlobalRankTop"># 96 Worldwide</span>', ""),
                   ('<span class="KeyStatisticText Figure">Public</span>', ""))
    page = parse(html)
    for name in ("global_top", "worldwide_number", "institution_type"):
        assert page.data[name] is None and name in page.missing
    assert all(value != "null" for value in page.data.values())


# --- records ----------------------------------------------------------------------------------------

def test_record_validation():
    assert validate_record(dict(EXPECTED), MASTER.fields) == []
    assert validate_record([], MASTER.fields) == ["the JSON file does not contain an object"]
    bad = dict(EXPECTED, qs_rank=111, location="null")
    del bad["country"]
    problems = " | ".join(validate_record(bad, MASTER.fields))
    assert "'country' is missing" in problems and "'qs_rank' must be a string" in problems
    assert "'location' must be null" in problems


def test_merge_never_erases_with_null_and_keeps_extra_keys():
    existing = dict(EXPECTED, notes="hand-added")
    merged, changed = merge(existing, dict(EXPECTED, qs_rank=None, worldwide_number="90"), MASTER.fields)
    assert merged["qs_rank"] == "111" and merged["worldwide_number"] == "90"
    assert merged["notes"] == "hand-added" and changed == ["worldwide_number"]


@pytest.mark.parametrize("name, expected", [
    ("University of Southampton", "University of Southampton.json"),
    ('Univ: of "A/B"?', "Univ- of -A-B.json"),
    ("CON", "_CON.json"),
    ("Name ending in dot.", "Name ending in dot.json"),
    ("../../escape", "escape.json"),
])
def test_filename_sanitisation(name, expected):
    assert safe_filename(name) == expected


# --- task lifecycle ---------------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path):
    settings = UniversitySettings(data_dir=tmp_path / "data", export_root=tmp_path / "export",
                                  import_folder=tmp_path / "imports" / "university")
    manager = UniversityImportManager(settings)
    yield manager, settings
    manager.store.close()


def put(folder: Path, name: str, html: str = SOURCE, companion: bool = False) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(html, encoding="utf-8")
    if companion:
        (folder / f"{path.stem}_files").mkdir()
        (folder / f"{path.stem}_files" / "logo.png").write_bytes(b"png")
        (folder / f"{path.stem}_files" / "saved_resource.html").write_text("<html></html>")
    return path


def run(manager: UniversityImportManager, category="master", folder="", delete_html=False, policy="update"):
    task = manager.create(category, folder, delete_html, policy)
    manager.wait(task["id"])
    return manager.snapshot(task["id"])


def output(settings: UniversitySettings, country="UK") -> Path:
    return settings.export_root / country / "universities"


def test_default_import_folder_is_created(env):
    _, settings = env
    assert settings.import_folder.is_dir()


def test_folder_inspection_counts_eligible_files(env, tmp_path):
    manager, settings = env
    folder = settings.import_folder
    put(folder, "University of Southampton _ 402 Masters.html", companion=True)
    put(folder, "notes.html", "<html><body>hello</body></html>")
    (folder / "readme.txt").write_text("x")
    info = manager.inspect_folder("")
    assert info["html_files"] == 2 and info["eligible"] == 1 and info["other"] == 1
    assert info["is_default"] and info["eligible_files"] == ["University of Southampton _ 402 Masters.html"]


@pytest.mark.parametrize("bad", ["relative\\folder", "missing", "file"])
def test_folder_validation_errors(env, tmp_path, bad):
    manager, _ = env
    (tmp_path / "file").write_text("x")
    raw = bad if bad == "relative\\folder" else str(tmp_path / bad)
    with pytest.raises(TaskError):
        manager.inspect_folder(raw)


def test_start_refuses_empty_or_ineligible_folder(env, tmp_path):
    manager, _ = env
    with pytest.raises(TaskError, match="No .html files"):
        manager.create("master", "", False)
    put(tmp_path / "other", "x.html", "<html></html>")
    with pytest.raises(TaskError, match="none is a university|None of the"):
        manager.create("master", str(tmp_path / "other"), False)
    with pytest.raises(TaskError, match="category"):
        manager.create("phd", "", False)


def test_full_import_writes_expected_json(env):
    manager, settings = env
    source = put(settings.import_folder, "University of Southampton _ 402 Masters.html")
    put(settings.import_folder, "notes.html", "<html><body>hello</body></html>")
    task = run(manager)
    assert task["status"] == "completed"
    assert task["files_total"] == 2 and task["eligible_total"] == 1 and task["percent"] == 100.0
    assert task["files_done"] == 1 and task["files_skipped"] == 1 and task["universities_created"] == 1
    assert task["json_files_written"] == 1 and task["html_deleted"] == 0
    target = output(settings) / "University of Southampton.json"
    assert json.loads(target.read_text(encoding="utf-8")) == EXPECTED
    assert source.exists()  # deletion disabled
    files = {f["name"]: f for f in manager.store.files(task["id"])}
    assert files["notes.html"]["status"] == "skipped" and "not a university" in files["notes.html"]["reason"]
    done = files[source.name]
    assert done["output_path"] == str(target) and done["university"] == EXPECTED["university"]
    assert done["started_at"] and done["processed_at"] and done["missing"] == []


BACHELOR_PAGE = (("mastersportal.com", "bachelorsportal.com"), ("PortalType = 'master'", "PortalType = 'bachelor'"),
                 (">Master's<", ">Bachelor's<"), (" Masters ", " Bachelors "))


def test_bachelor_and_master_share_one_file(env):
    manager, settings = env
    put(settings.import_folder, "bachelor.html", variant(*BACHELOR_PAGE))
    task = run(manager, "bachelor")
    assert task["status"] == "completed"
    target = output(settings) / "University of Southampton.json"
    assert json.loads(target.read_text("utf-8"))["number_bachelor_program"] == "402"
    put(settings.import_folder, "bachelor.html", SOURCE)
    assert run(manager, "master")["status"] == "completed"
    record = json.loads(target.read_text("utf-8"))
    assert record["number_bachelor_program"] == "402" and record["number_master_program"] == "402"
    assert [p.name for p in settings.export_root.iterdir()] == ["UK"]


def test_page_from_other_portal_is_skipped(env):
    manager, settings = env
    put(settings.import_folder, "southampton.html")
    task = run(manager, "bachelor")
    assert task["files_skipped"] == 1 and task["files_done"] == 0
    assert "UK / Master's" in manager.store.files(task["id"])[0]["reason"]


def test_non_uk_universities_go_to_their_country_folder(env):
    manager, settings = env
    put(settings.import_folder, "uni.html")
    put(settings.import_folder, "delft.html", other_university("Delft University", "98", **{
        "<span>United Kingdom</span>": "<span>Netherlands</span>"}))
    put(settings.import_folder, "escp.html", other_university("ESCP Business School", "97", **{
        "<span>United Kingdom</span>": "<span>France</span>"}))
    task = run(manager)
    assert task["status"] == "completed" and task["files_done"] == 3 and task["files_skipped"] == 0
    escp = json.loads((output(settings, "France") / "ESCP Business School.json").read_text("utf-8"))
    assert escp["university"] == "ESCP Business School" and escp["country"] == "France"
    assert (output(settings, "Netherlands") / "Delft University.json").exists()
    assert (output(settings) / "University of Southampton.json").exists()
    assert sorted(p.name for p in settings.export_root.iterdir()) == ["France", "Netherlands", "UK"]


def test_missing_country_goes_to_unknown_folder_with_warning(env):
    manager, settings = env
    put(settings.import_folder, "x.html", other_university("Nowhere College", "95", **{
        "<span>United Kingdom</span>": "<span></span>"}))
    task = run(manager)
    assert task["status"] == "completed"
    assert (output(settings, "Unknown country") / "Nowhere College.json").exists()
    assert any("country not found" in w for w in manager.store.files(task["id"])[0]["warnings"])


def test_files_recorded_before_country_folders_are_reused(env):
    manager, settings = env
    put(settings.import_folder, "uni.html")
    first = run(manager)
    target = output(settings) / "University of Southampton.json"
    manager.store.execute("UPDATE universities SET filename = 'University of Southampton.json'")
    assert run(manager)["universities_unchanged"] == 1 and first["universities_created"] == 1
    assert [p.name for p in output(settings).iterdir()] == [target.name]
    assert manager.store.university_by_filename("master", "UK/universities/University of Southampton.json")


def test_blocked_page_is_reported_and_kept(env):
    manager, settings = env
    put(settings.import_folder, "uni.html")
    path = put(settings.import_folder, "blocked.html",
               '<html><head><title>Just a moment...</title></head><body class="OrganisationName">'
               '<span class="OrganisationName" data-cy="OrganisationName"></span></body></html>')
    task = run(manager, delete_html=True)
    assert task["status"] == "completed_with_errors" and task["files_blocked"] == 1
    assert path.exists()


def test_records_with_missing_fields_count_as_warnings(env):
    manager, settings = env
    put(settings.import_folder, "uni.html", variant(("QS World University Rankings", "Other ranking")))
    task = run(manager)
    assert task["files_done"] == 1 and task["with_warnings"] == 1 and task["imported_clean"] == 0
    record = json.loads((output(settings) / "University of Southampton.json").read_text("utf-8"))
    assert record["qs_rank"] is None
    assert manager.store.files(task["id"])[0]["missing"] == ["qs_rank"]


def test_missing_university_name_fails_and_keeps_html(env):
    manager, settings = env
    path = put(settings.import_folder, "broken.html", variant(
        ('data-cy="OrganisationName">University of Southampton', 'data-cy="OrganisationName">'),
        ('data-cy="AboutSectionOrganisationName">University of Southampton', 'data-cy="AboutSectionOrganisationName">')))
    task = run(manager, delete_html=True)
    assert task["files_failed"] == 1 and path.exists()
    assert "university name not found" in manager.store.files(task["id"])[0]["reason"]
    assert not output(settings).exists() or not any(output(settings).iterdir())


def test_successful_import_deletes_html_and_companion_folder(env):
    manager, settings = env
    path = put(settings.import_folder, "University of Southampton _ 402 Masters.html", companion=True)
    task = run(manager, delete_html=True)
    assert task["status"] == "completed" and task["html_deleted"] == 1
    assert not path.exists() and not (settings.import_folder / f"{path.stem}_files").exists()
    assert manager.store.files(task["id"])[0]["deleted"] == 1
    assert (output(settings) / "University of Southampton.json").exists()
    assert any("Deleted" in e["message"] for e in manager.store.events(task["id"]))


def test_write_failure_keeps_html_and_retry_succeeds(env, monkeypatch):
    manager, settings = env
    path = put(settings.import_folder, "uni.html")
    import universities.tasks as tasks_module

    real_write = tasks_module.write_json_atomic

    def failing_write(*args, **kwargs):
        raise PermissionError("Access is denied: output folder")

    monkeypatch.setattr(tasks_module, "write_json_atomic", failing_write)
    task = run(manager, delete_html=True)
    assert task["status"] == "completed_with_errors" and task["files_failed"] == 1 and task["can_retry"]
    assert path.exists() and "Access is denied" in task["last_error"]

    monkeypatch.setattr(tasks_module, "write_json_atomic", real_write)
    manager.retry_failed(task["id"])
    manager.wait(task["id"])
    task = manager.snapshot(task["id"])
    assert task["status"] == "completed" and task["files_done"] == 1 and task["retries"] == 1
    assert not path.exists() and task["html_deleted"] == 1


def test_history_failure_prevents_deletion(env, monkeypatch):
    manager, settings = env
    path = put(settings.import_folder, "uni.html")

    def broken_transaction():
        raise OSError("database is locked")

    monkeypatch.setattr(manager.store, "transaction", broken_transaction)
    task = run(manager, delete_html=True)
    assert task["files_failed"] == 1 and task["html_deleted"] == 0
    assert path.exists()


def test_progress_does_not_count_a_file_until_json_is_verified(env, monkeypatch):
    manager, settings = env
    put(settings.import_folder, "uni.html")
    import universities.tasks as tasks_module

    seen = []
    real_write = tasks_module.write_json_atomic

    def spy(path, payload, *args, **kwargs):
        task = manager.snapshot(manager.store.list_tasks(1)[0]["id"])
        seen.append((task["percent"], task["files_processed"], task["file_stage"], task["current_university"]))
        return real_write(path, payload, *args, **kwargs)

    monkeypatch.setattr(tasks_module, "write_json_atomic", spy)
    task = run(manager)
    assert seen == [(0.0, 0, "writing JSON", "University of Southampton")]
    assert task["percent"] == 100.0 and task["files_processed"] == 1 and task["files_pending"] == 0


def test_existing_invalid_json_is_not_overwritten(env):
    manager, settings = env
    path = put(settings.import_folder, "uni.html")
    target = output(settings) / "University of Southampton.json"
    target.parent.mkdir(parents=True)
    target.write_text("{ broken", encoding="utf-8")
    task = run(manager, delete_html=True)
    assert task["files_failed"] == 1 and path.exists()
    assert target.read_text(encoding="utf-8") == "{ broken"
    assert "not valid JSON" in manager.store.files(task["id"])[0]["reason"]


def test_existing_record_is_updated_without_losing_data(env):
    manager, settings = env
    target = output(settings) / "University of Southampton.json"
    target.parent.mkdir(parents=True)
    old = dict(EXPECTED, worldwide_number="120", notes="kept")
    target.write_text(json.dumps(old), encoding="utf-8")
    put(settings.import_folder, "uni.html", variant(("QS World University Rankings", "Other ranking")))
    task = run(manager)
    assert task["universities_updated"] == 1 and task["universities_created"] == 0
    record = json.loads(target.read_text("utf-8"))
    assert record["worldwide_number"] == "96"     # updated
    assert record["qs_rank"] == "111"             # not erased by the missing value
    assert record["notes"] == "kept"              # hand-added keys survive


def test_reimport_is_unchanged_and_creates_no_duplicates(env):
    manager, settings = env
    put(settings.import_folder, "uni.html")
    run(manager)
    task = run(manager)
    assert task["universities_unchanged"] == 1 and task["json_files_written"] == 0
    assert [p.name for p in output(settings).iterdir()] == ["University of Southampton.json"]


def test_policies_create_only_and_skip_imported(env):
    manager, settings = env
    put(settings.import_folder, "uni.html")
    run(manager)
    created_only = run(manager, policy="create_only")
    assert created_only["files_skipped"] == 1
    assert "already exists" in manager.store.files(created_only["id"])[0]["reason"]
    skipped = run(manager, policy="skip_imported")
    assert skipped["files_skipped"] == 1
    assert "already imported" in manager.store.files(skipped["id"])[0]["reason"]


def test_same_university_twice_in_one_task_is_merged(env):
    manager, settings = env
    put(settings.import_folder, "a.html")
    put(settings.import_folder, "b.html", variant(("# 96 Worldwide", "# 95 Worldwide")))
    put(settings.import_folder, "c.html")  # byte-identical to a.html
    task = run(manager)
    files = {f["name"]: f for f in manager.store.files(task["id"])}
    assert files["a.html"]["outcome"] == "created"
    assert files["b.html"]["outcome"] == "updated" and "same university as a.html" in files["b.html"]["reason"]
    assert files["c.html"]["status"] == "skipped" and "identical content" in files["c.html"]["reason"]
    assert [p.name for p in output(settings).iterdir()] == ["University of Southampton.json"]


def test_different_institutions_with_same_name_get_separate_files(env):
    manager, settings = env
    put(settings.import_folder, "a.html")
    put(settings.import_folder, "b.html", variant(("/357/", "/9999/"), ("# 96 Worldwide", "# 500 Worldwide")))
    task = run(manager)
    assert task["universities_created"] == 2
    names = sorted(p.name for p in output(settings).iterdir())
    assert names == ["University of Southampton (2).json", "University of Southampton.json"]
    assert json.loads((output(settings) / names[1]).read_text("utf-8"))["worldwide_number"] == "96"


def test_file_of_another_institution_with_the_same_file_name_is_not_overwritten(env):
    manager, settings = env
    target = output(settings) / "University of Southampton.json"
    target.parent.mkdir(parents=True)
    other = dict(EXPECTED, university="Southampton Business School")
    target.write_text(json.dumps(other), encoding="utf-8")
    put(settings.import_folder, "uni.html")
    task = run(manager)
    assert task["universities_created"] == 1
    assert json.loads(target.read_text("utf-8")) == other
    second = output(settings) / "University of Southampton (2).json"
    assert json.loads(second.read_text("utf-8")) == EXPECTED


def test_history_persists_across_restart(env):
    manager, settings = env
    put(settings.import_folder, "uni.html")
    task = run(manager)
    manager.store.close()
    reopened = UniversityImportManager(settings)
    try:
        again = reopened.snapshot(task["id"])
        assert again["status"] == "completed" and again["files_done"] == 1
        files = reopened.store.files(task["id"])
        assert files[0]["output_path"].endswith("University of Southampton.json")
        assert any("Imported uni.html" in e["message"] for e in reopened.store.events(task["id"]))
        assert reopened.store.category_stats("master")["universities"] == 1
    finally:
        reopened.store.close()


def test_custom_export_folder(env, tmp_path):
    manager, settings = env
    put(settings.import_folder, "uni.html")
    export = tmp_path / "exports"
    export.mkdir()
    task = manager.create("master", "", False, "update", str(export))
    manager.wait(task["id"])
    task = manager.snapshot(task["id"])
    assert task["status"] == "completed" and task["output_dir"] == str(export.resolve())
    assert json.loads((export / "UK" / "universities" / "University of Southampton.json").read_text("utf-8")) == EXPECTED
    assert not output(settings).exists()
    assert manager.store.files(task["id"])[0]["output_path"] == str(export.resolve() / "UK" / "universities" / "University of Southampton.json")


def test_export_folder_must_exist_and_be_absolute(env, tmp_path):
    manager, settings = env
    put(settings.import_folder, "uni.html")
    for bad in (str(tmp_path / "missing"), "relative\\folder"):
        with pytest.raises(TaskError, match="Export folder"):
            manager.create("master", "", False, "update", bad)


def test_second_task_is_refused_while_one_is_active(env):
    manager, settings = env
    put(settings.import_folder, "uni.html")
    manager.store.create_task("busy", "master", str(settings.import_folder), False)
    manager.store.update_task("busy", status="running")
    with pytest.raises(TaskError) as exc:
        manager.create("master", "", False)
    assert exc.value.http_status == 409


# --- web page and API --------------------------------------------------------------------------------

@pytest.fixture
def client(env, tmp_path):
    from app import create_app
    from programmes.config import ImportSettings
    from programmes.tasks import ImportManager

    manager, settings = env
    programmes = ImportManager(ImportSettings(data_dir=tmp_path / "pdata", output_root=tmp_path / "pout",
                                              import_root=tmp_path / "pimports"))
    app = create_app(import_manager=programmes, university_manager=manager)
    yield app.test_client(), manager, settings
    programmes.store.close()


def test_page_renders_controls(client):
    http, _, _ = client
    response = http.get("/universities")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    for text in ("Universities Importer", "University Category", '<option value="master">UK / Master\'s</option>',
                 '<option value="bachelor">UK / Bachelor\'s</option>', "Import Folder", 'id="browse-btn"',
                 "Delete each HTML file after its data has been saved and verified.", "Start Import",
                 'id="folder-dialog"', "folder-picker.js"):
        assert text.replace("'", "&#39;") in body or text in body, text


def test_navigation_links_all_pages(client):
    http, _, _ = client
    for page in ("/", "/programmes", "/universities"):
        assert 'href="/universities"' in http.get(page).get_data(as_text=True)


def test_api_overview_inspect_and_import(client):
    http, manager, settings = client
    put(settings.import_folder, "University of Southampton _ 402 Masters.html")
    overview = http.get("/api/universities/overview").get_json()
    assert overview["import_folder"] == str(settings.import_folder)
    assert set(overview["categories"]) == {"master", "bachelor"} and overview["folder_selection"] is True
    assert overview["category_order"] == ["master", "bachelor"]
    assert [p["value"] for p in overview["policies"]] == ["update", "create_only", "skip_imported"]
    info = http.get("/api/universities/folder", query_string={"path": ""}).get_json()
    assert info["eligible"] == 1
    response = http.post("/api/universities/tasks", json={"category": "master", "folder": "", "delete_html": False})
    assert response.status_code == 202
    task_id = response.get_json()["task"]["id"]
    manager.wait(task_id)
    task = http.get(f"/api/universities/tasks/{task_id}").get_json()["task"]
    assert task["status"] == "completed"
    files = http.get(f"/api/universities/tasks/{task_id}/files").get_json()["files"]
    assert files[0]["university"] == "University of Southampton"
    assert http.get(f"/api/universities/tasks/{task_id}/events").get_json()["events"]
    log = http.get(f"/api/universities/tasks/{task_id}/log?format=csv")
    assert log.status_code == 200 and "Imported" in log.get_data(as_text=True)
    assert http.get("/api/universities/tasks").get_json()["tasks"][0]["id"] == task_id


def test_api_validation_errors(client, tmp_path):
    http, _, _ = client
    missing = http.get("/api/universities/folder", query_string={"path": str(tmp_path / "nope")})
    assert missing.status_code == 400 and "not found" in missing.get_json()["error"]
    bad = http.post("/api/universities/tasks", json={"category": "phd", "folder": ""})
    assert bad.status_code == 400


def test_remote_clients_cannot_choose_other_folders(client, tmp_path):
    http, _, settings = client
    put(settings.import_folder, "uni.html")
    denied = http.get("/api/universities/folder", query_string={"path": str(tmp_path)}, environ_base=REMOTE)
    assert denied.status_code == 403
    allowed = http.get("/api/universities/folder", query_string={"path": ""}, environ_base=REMOTE)
    assert allowed.status_code == 200
    started = http.post("/api/universities/tasks", json={"category": "master", "folder": str(tmp_path)},
                        environ_base=REMOTE)
    assert started.status_code == 403


def test_export_folder_field_and_api(client, tmp_path):
    http, manager, settings = client
    body = http.get("/universities").get_data(as_text=True)
    assert "Export Folder" in body and 'id="export-folder"' in body and 'id="export-browse-btn"' in body
    assert body.index("Import Folder") < body.index("Export Folder")
    default = http.get("/api/universities/bachelor/export-folder", query_string={"path": ""}).get_json()
    assert default["is_default"] and not default["exists"]
    assert default["folder"] == str(settings.export_root.resolve())
    assert http.get("/api/universities/phd/export-folder").status_code == 404

    put(settings.import_folder, "uni.html")
    export = tmp_path / "out-api"
    export.mkdir()
    denied = http.post("/api/universities/tasks", json={"category": "master", "export_folder": str(export)},
                       environ_base=REMOTE)
    assert denied.status_code == 403
    response = http.post("/api/universities/tasks", json={"category": "master", "folder": "",
                                                          "export_folder": str(export), "delete_html": False})
    assert response.status_code == 202
    manager.wait(response.get_json()["task"]["id"])
    assert (export / "UK" / "universities" / "University of Southampton.json").exists()
    info = http.get("/api/universities/master/export-folder", query_string={"path": str(export)}).get_json()
    assert info["json_files"] == 1 and not info["is_default"]


def test_programme_importer_still_works(client):
    http, _, _ = client
    assert http.get("/programmes").status_code == 200
    assert set(http.get("/api/programmes/overview").get_json()["categories"]) == {"master", "bachelor"}
