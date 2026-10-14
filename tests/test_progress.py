import json
import threading
import time

import pytest

from app import create_app
from conftest import PAGE
from downloader.errors import DuplicateJobError, TooManyJobsError
from downloader.fetcher import run_http_download
from downloader.jobs import JobManager, calculate_percent


# --- percentage calculation ----------------------------------------------------------------

@pytest.mark.parametrize("downloaded, total, expected", [
    (0, 1000, 0.0),
    (250, 1000, 25.0),
    (333, 1000, 33.3),
    (999, 1000, 99.0),
    (1000, 1000, 99.0),   # all bytes received but not yet saved
    (1500, 1000, 99.0),
    (100, None, None),    # unknown total -> indeterminate
    (100, 0, None),
])
def test_calculate_percent(downloaded, total, expected):
    assert calculate_percent(downloaded, total) == expected


# --- job manager ---------------------------------------------------------------------------

def blocking_runner(release: threading.Event):
    def run(job, settings):
        job.set_stage("downloading", "working")
        release.wait(5)
        job.complete("x.html", "/tmp/x.html", 1)
    return run


def test_duplicate_active_job_rejected(settings):
    release = threading.Event()
    manager = JobManager(settings, {"http": blocking_runner(release)})
    first = manager.start("https://example.com/", "http")
    with pytest.raises(DuplicateJobError) as info:
        manager.start("https://example.com/", "http")
    assert info.value.job.id == first.id
    other = manager.start("https://example.org/", "http")  # different URL is fine
    release.set()
    for job in (first, other):
        manager.wait_for_change(job.id, -1, 1)
    deadline = time.time() + 5
    while first.is_active and time.time() < deadline:
        time.sleep(0.01)
    # Once finished, the same URL can be downloaded again.
    again = manager.start("https://example.com/", "http")
    assert again.id != first.id


def test_too_many_active_jobs(settings):
    release = threading.Event()
    manager = JobManager(settings, {"http": blocking_runner(release)})
    for i in range(settings.max_active_jobs):
        manager.start(f"https://example.com/{i}", "http")
    with pytest.raises(TooManyJobsError):
        manager.start("https://example.com/extra", "http")
    release.set()


def test_wait_for_change_wakes_on_update(settings):
    release = threading.Event()
    manager = JobManager(settings, {"http": blocking_runner(release)})
    job = manager.start("https://example.com/", "http")
    version = manager.snapshot(job.id)["version"]
    threading.Timer(0.1, release.set).start()
    start = time.monotonic()
    snap = manager.wait_for_change(job.id, version, 5)
    assert snap["version"] != version
    assert time.monotonic() - start < 4


def test_runner_crash_marks_job_failed(settings):
    def crash(job, settings):
        raise RuntimeError("kaboom")

    manager = JobManager(settings, {"http": crash})
    job = manager.start("https://example.com/", "http")
    deadline = time.time() + 5
    while job.is_active and time.time() < deadline:
        time.sleep(0.01)
    assert job.status == "failed" and "kaboom" in job.error


# --- HTTP API end to end -------------------------------------------------------------------

@pytest.fixture
def client(settings):
    app = create_app(settings)
    app.testing = True
    return app.test_client()


def wait_for_final(client, job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()["job"]
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_index_page_renders(client):
    res = client.get("/")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    for element in ('id="url-input"', 'id="download-btn"', 'id="progress-track"',
                    'id="status-badge"', 'id="result-card"', 'id="save-btn"', 'id="error-card"',
                    'id="cancel-btn"', "does not bypass"):
        assert element in body


def test_full_workflow_via_api(client, server, settings):
    res = client.post("/api/jobs", json={"url": server + "/page"})
    assert res.status_code == 202
    job_id = res.get_json()["job"]["id"]

    job = wait_for_final(client, job_id)
    assert job["status"] == "completed", job["error"]
    assert job["percent"] == 100.0
    assert job["file_size"] == len(PAGE)
    assert job["filename"].endswith(".html")

    file_res = client.get(f"/api/jobs/{job_id}/file")
    assert file_res.status_code == 200
    assert file_res.data == PAGE
    assert "attachment" in file_res.headers["Content-Disposition"]
    assert job["filename"] in file_res.headers["Content-Disposition"]


def test_sse_stream_reports_progress_until_completion(client, server):
    job_id = client.post("/api/jobs", json={"url": server + "/slow-chunks"}).get_json()["job"]["id"]
    res = client.get(f"/api/jobs/{job_id}/events")
    assert res.mimetype == "text/event-stream"
    events = [json.loads(line[len("data: "):])
              for line in res.get_data(as_text=True).splitlines() if line.startswith("data: {")]
    events = [e for e in events if e]
    assert events[-1]["status"] == "completed"
    assert events[-1]["percent"] == 100.0
    in_progress = [e for e in events if e["status"] == "running" and e["stage"] == "downloading"]
    assert in_progress, "SSE should emit intermediate download progress"
    assert all(e["percent"] is None or e["percent"] < 100 for e in in_progress)
    versions = [e["version"] for e in events]
    assert versions == sorted(versions)


def test_invalid_url_returns_400(client):
    res = client.post("/api/jobs", json={"url": "ftp://example.com"})
    assert res.status_code == 400
    assert "http" in res.get_json()["error"]


def test_private_url_returns_400(client):
    res = client.post("/api/jobs", json={"url": "http://192.168.0.1/"})
    assert res.status_code == 400
    assert res.get_json()["code"] == "blocked_destination"


def test_rendered_mode_unavailable_by_default(client):
    res = client.post("/api/jobs", json={"url": "https://example.com", "mode": "rendered"})
    assert res.status_code == 400
    assert res.get_json()["code"] == "render_unavailable"


def test_duplicate_request_returns_existing_job(client, server):
    first = client.post("/api/jobs", json={"url": server + "/slow-chunks"})
    second = client.post("/api/jobs", json={"url": server + "/slow-chunks"})
    assert first.status_code == 202
    assert second.status_code == 409
    assert second.get_json()["job"]["id"] == first.get_json()["job"]["id"]
    wait_for_final(client, first.get_json()["job"]["id"])


def test_cancel_via_api(client, server, settings):
    job_id = client.post("/api/jobs", json={"url": server + "/slow-chunks"}).get_json()["job"]["id"]
    res = client.post(f"/api/jobs/{job_id}/cancel")
    assert res.status_code == 200
    job = wait_for_final(client, job_id)
    assert job["status"] == "cancelled"
    assert client.get(f"/api/jobs/{job_id}/file").status_code == 409
    assert list(settings.download_dir.iterdir()) == []


def test_failed_job_reports_error_and_has_no_file(client, server):
    job_id = client.post("/api/jobs", json={"url": server + "/404"}).get_json()["job"]["id"]
    job = wait_for_final(client, job_id)
    assert job["status"] == "failed"
    assert "404" in job["error"]
    assert client.get(f"/api/jobs/{job_id}/file").status_code == 409


def test_unknown_job_404(client):
    assert client.get("/api/jobs/doesnotexist").status_code == 404
    assert client.get("/api/jobs/doesnotexist/events").status_code == 404
    assert client.post("/api/jobs/doesnotexist/cancel").status_code == 404
    assert client.get("/api/jobs/doesnotexist/file").status_code == 404


def test_run_http_download_is_used_for_http_mode(client):
    manager = client.application.config["JOB_MANAGER"]
    assert manager.runners["http"] is run_http_download
