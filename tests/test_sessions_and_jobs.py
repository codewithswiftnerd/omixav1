"""
Session-based job ownership tests — the core of the auth rewrite: no
API key anywhere, a job belongs to whichever browser session created
it, and every other session gets an identical "not found" response
regardless of whether the job_id is real, expired, or belongs to
someone else (see routes/process.py's owns() note on why that
response is deliberately indistinguishable).
"""

from tests.conftest import make_csv


def _upload(client, filename="people.csv", content=None):
    data = {"file": (make_csv(content), filename)}
    return client.post("/api/upload/", data=data, content_type="multipart/form-data")


def test_session_cookie_issued_on_first_request(client):
    r = client.get("/")
    assert r.status_code == 200
    cookie = client.get_cookie("omixa_session")
    assert cookie is not None
    assert cookie.http_only is True
    assert cookie.same_site == "Lax"


def test_upload_report_process_download_happy_path(client):
    r = _upload(client)
    assert r.status_code == 201
    job_id = r.get_json()["job_id"]

    r = client.get(f"/api/report/{job_id}")
    assert r.status_code == 200
    assert "score" in r.get_json()["report"]

    r = client.post(f"/api/process/{job_id}", json={})
    assert r.status_code == 200
    assert r.get_json()["status"] == "completed"

    r = client.get(f"/api/download/{job_id}")
    assert r.status_code == 200
    assert len(r.data) > 0


def test_second_session_cannot_read_report_of_first_sessions_job(client, app):
    owner = client
    stranger = app.test_client()

    r = _upload(owner)
    job_id = r.get_json()["job_id"]

    r = stranger.get(f"/api/report/{job_id}")
    assert r.status_code == 404


def test_second_session_cannot_process_first_sessions_job(client, app):
    owner = client
    stranger = app.test_client()

    r = _upload(owner)
    job_id = r.get_json()["job_id"]

    r = stranger.post(f"/api/process/{job_id}", json={})
    assert r.status_code == 404
    assert r.get_json().get("status") != "completed"


def test_second_session_cannot_download_first_sessions_job(client, app):
    owner = client
    stranger = app.test_client()

    r = _upload(owner)
    job_id = r.get_json()["job_id"]
    client.post(f"/api/process/{job_id}", json={})

    r = stranger.get(f"/api/download/{job_id}")
    assert r.status_code == 404


def test_nonexistent_job_id_gets_same_shape_response_as_someone_elses_job(client, app):
    """A stranger probing a real job_id and a client probing a
    made-up one must not be distinguishable from the response, or a
    job_id becomes enumerable."""
    owner = client
    stranger = app.test_client()

    r = _upload(owner)
    real_job_id = r.get_json()["job_id"]

    real_response = stranger.get(f"/api/report/{real_job_id}")
    fake_response = stranger.get("/api/report/00000000-0000-0000-0000-000000000000")

    assert real_response.status_code == fake_response.status_code == 404
    assert real_response.get_json() == fake_response.get_json()


def test_owner_can_still_access_after_processing(client):
    r = _upload(client)
    job_id = r.get_json()["job_id"]
    client.post(f"/api/process/{job_id}", json={})

    r = client.get(f"/api/report/{job_id}")
    assert r.status_code == 200


def test_download_deletes_job_so_second_download_by_owner_fails(client):
    r = _upload(client)
    job_id = r.get_json()["job_id"]
    client.post(f"/api/process/{job_id}", json={})

    first = client.get(f"/api/download/{job_id}")
    assert first.status_code == 200

    second = client.get(f"/api/download/{job_id}")
    assert second.status_code == 404
