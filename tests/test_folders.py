"""Download-folder selection: validation, folder browser API and per-job download folders."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import pytest

from app import create_app
from downloader.folders import FolderError, create_folder, list_folders, validate_download_dir
from conftest import PAGE

REMOTE = {"REMOTE_ADDR": "192.168.1.50"}


def test_validate_download_dir(tmp_path):
    assert validate_download_dir(f'  "{tmp_path}"  ') == tmp_path.resolve()
    (tmp_path / "file.txt").write_text("x")
    for bad in ("", "relative\\folder", str(tmp_path / "missing"), str(tmp_path / "file.txt")):
        with pytest.raises(FolderError):
            validate_download_dir(bad)


def test_list_folders_shows_only_visible_subfolders(tmp_path):
    for name in ("b", "A", ".hidden", "$Recycle"):
        (tmp_path / name).mkdir()
    (tmp_path / "file.html").write_text("x")
    listing = list_folders(str(tmp_path))
    assert [f["name"] for f in listing["folders"]] == ["A", "b"]
    assert listing["path"] == str(tmp_path.resolve())
    assert listing["parent"] == str(tmp_path.resolve().parent)
    drives = list_folders("")
    assert drives["path"] is None and drives["folders"]


@pytest.mark.parametrize("name", ["", "..", "CON", "a/b", 'x"y', "dot.", "bad?"])
def test_create_folder_rejects_bad_names(tmp_path, name):
    with pytest.raises(FolderError):
        create_folder(str(tmp_path), name)


def test_create_folder(tmp_path):
    created = create_folder(str(tmp_path), "HTML pages")
    assert created.is_dir() and created.name == "HTML pages"
    with pytest.raises(FolderError):
        create_folder(str(tmp_path), "HTML pages")


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


def test_download_into_chosen_folder(client, server, settings, tmp_path):
    target = tmp_path / "chosen"
    target.mkdir()
    res = client.post("/api/jobs", json={"url": server + "/page", "download_dir": str(target)})
    assert res.status_code == 202
    job = wait_for_final(client, res.get_json()["job"]["id"])
    assert job["status"] == "completed", job["error"]
    assert Path(job["file_path"]).parent == target.resolve()
    assert job["download_dir"] == str(target.resolve())
    assert not settings.download_dir.exists() or not any(settings.download_dir.iterdir())
    assert client.get(f"/api/jobs/{job['id']}/file").data == PAGE


def test_invalid_download_dir_rejected(client, server, tmp_path):
    res = client.post("/api/jobs", json={"url": server + "/page", "download_dir": str(tmp_path / "nope")})
    assert res.status_code == 400 and res.get_json()["code"] == "invalid_folder"


def test_folder_api(client, tmp_path):
    tmp_path = tmp_path / "browse"
    (tmp_path / "sub").mkdir(parents=True)
    listing = client.get("/api/folders", query_string={"path": str(tmp_path)}).get_json()
    assert [f["name"] for f in listing["folders"]] == ["sub"]
    made = client.post("/api/folders", json={"parent": str(tmp_path), "name": "new"})
    assert made.status_code == 201 and (tmp_path / "new").is_dir()
    assert client.post("/api/folders", json={"parent": str(tmp_path), "name": "CON"}).status_code == 400
    assert client.get("/api/folders", query_string={"path": str(tmp_path / "missing")}).status_code == 400


def test_remote_clients_cannot_browse_or_choose_folders(client, server, tmp_path):
    assert client.get("/api/folders", environ_base=REMOTE).status_code == 403
    assert client.post("/api/folders", json={"parent": str(tmp_path), "name": "x"},
                       environ_base=REMOTE).status_code == 403
    res = client.post("/api/jobs", json={"url": server + "/page", "download_dir": str(tmp_path)},
                      environ_base=REMOTE)
    assert res.status_code == 403
    page = client.get("/", environ_base=REMOTE).get_data(as_text=True)
    assert 'id="browse-btn" type="button" class="btn secondary"\n                  disabled' in page


def test_remote_folder_selection_can_be_enabled(settings, tmp_path):
    app = create_app(replace(settings, allow_remote_folder_selection=True))
    assert app.test_client().get("/api/folders", query_string={"path": str(tmp_path)},
                                 environ_base=REMOTE).status_code == 200


def test_index_has_multi_url_and_location_controls(client):
    body = client.get("/").get_data(as_text=True)
    for element in ('<textarea id="url-input"', 'id="url-file"', 'accept=".txt,text/plain"',
                    "Download Interval Duration", 'id="download-interval"', 'value="2"',
                    "Browse Location", 'id="download-dir"', 'id="browse-btn"', 'id="batch-card"',
                    'id="folder-dialog"', "window.APP_CONFIG"):
        assert element in body
