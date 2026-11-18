"""Feedback / reviews: public form + API, storage, admin view."""

import re
import time

import pytest

import db as metadata_db
from routes import feedback as fb
from tests.conftest import TEST_ADMIN_PASSWORD
from tests.test_admin import _login


def _token(age_seconds=5):
    """A valid form token that is already old enough to pass the 'too fast' check."""
    s = fb._serializer()
    return s.dumps({"t": int(time.time()) - age_seconds})


def payload(**over):
    p = {"rating": 4, "category": "general", "message": "Cleaning my file was quick and easy.",
         "name": "Ada", "email": "ada@example.com", "website": "", "token": _token()}
    p.update(over)
    return p


@pytest.fixture(autouse=True)
def _no_rate_limits(monkeypatch):
    for name in ("RATE_LIMIT_FEEDBACK_PER_MINUTE", "RATE_LIMIT_FEEDBACK_PER_HOUR", "RATE_LIMIT_FEEDBACK_GLOBAL_PER_HOUR"):
        monkeypatch.setattr(fb.Config, name, 0)


def test_page_renders_form_with_rating_and_token(client):
    r = client.get("/feedback")
    assert r.status_code == 200
    html = r.data.decode()
    assert 'id="feedbackForm"' in html
    assert html.count('name="rating"') == 5
    assert re.search(r'id="fToken" value="[^"]+"', html)


def test_footer_links_to_feedback(client):
    assert b'href="/feedback"' in client.get("/contact").data


def test_valid_submission_is_stored(client):
    r = client.post("/api/feedback", json=payload())
    assert r.status_code == 200 and r.get_json()["ok"] is True
    items, total = metadata_db.list_feedback()
    assert total == 1
    assert items[0]["rating"] == 4 and items[0]["category"] == "general"
    assert items[0]["email"] == "ada@example.com" and items[0]["name"] == "Ada"


def test_name_and_email_are_optional(client):
    r = client.post("/api/feedback", json=payload(name="", email=""))
    assert r.status_code == 200
    items, _ = metadata_db.list_feedback()
    assert items[0]["name"] is None and items[0]["email"] is None


@pytest.mark.parametrize("bad", [0, 6, -1, "x", None, 3.5, True])
def test_rating_must_be_1_to_5(client, bad):
    r = client.post("/api/feedback", json=payload(rating=bad))
    assert r.status_code == 422 and "rating" in r.get_json()["fields"]
    assert metadata_db.list_feedback()[1] == 0


def test_unknown_category_message_and_email_are_rejected(client):
    r = client.post("/api/feedback", json=payload(category="hax", message="short", email="nope"))
    assert r.status_code == 422
    assert set(r.get_json()["fields"]) == {"category", "message", "email"}


def test_too_many_links_rejected(client):
    r = client.post("/api/feedback", json=payload(message="see http://a.co http://b.co http://c.co for details"))
    assert r.status_code == 422 and "message" in r.get_json()["fields"]


def test_message_is_stored_as_plain_text_without_control_characters(client):
    client.post("/api/feedback", json=payload(message="<b>great</b> tool\x00 works well"))
    items, _ = metadata_db.list_feedback()
    assert items[0]["message"] == "<b>great</b> tool works well"   # escaping is the renderer's job (admin.js esc())


def test_honeypot_pretends_success_but_stores_nothing(client):
    r = client.post("/api/feedback", json=payload(website="http://spam.example"))
    assert r.status_code == 200
    assert metadata_db.list_feedback()[1] == 0


def test_bad_or_too_fresh_token_rejected(client):
    assert client.post("/api/feedback", json=payload(token="garbage")).status_code == 400
    assert client.post("/api/feedback", json=payload(token=fb.new_form_token())).status_code == 400  # 0s old
    assert client.post("/api/feedback", json=payload(token=None)).status_code == 400
    assert metadata_db.list_feedback()[1] == 0


def test_non_json_body_rejected(client):
    assert client.post("/api/feedback", data="x", content_type="text/plain").status_code == 400


def test_rate_limit_per_minute(client, monkeypatch):
    monkeypatch.setattr(fb.Config, "RATE_LIMIT_FEEDBACK_PER_MINUTE", 2)
    codes = [client.post("/api/feedback", json=payload()).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_storage_failure_is_reported_honestly(client, monkeypatch):
    def boom(**kw):
        raise metadata_db._DB_ERRORS[0]("disk full")
    monkeypatch.setattr(metadata_db, "add_feedback", boom)
    r = client.post("/api/feedback", json=payload())
    assert r.status_code == 503 and "ok" not in r.get_json()


def test_summary_counts_and_average(client):
    for rating in (5, 5, 3):
        metadata_db.add_feedback(rating, "general", "x" * 12)
    s = metadata_db.feedback_summary()
    assert s["count"] == 3 and s["average"] == 4.33
    assert s["distribution"]["5"] == 2 and s["distribution"]["1"] == 0


def test_summary_empty(client):
    s = metadata_db.feedback_summary()
    assert s["count"] == 0 and s["average"] is None


# ---------------------------------------------------------------- admin

def test_admin_feedback_requires_login(client):
    assert client.get("/admin/api/feedback").status_code == 401


def test_admin_sees_feedback_and_can_filter(client):
    metadata_db.add_feedback(5, "idea", "Please add dark mode to the app", name="Bo")
    metadata_db.add_feedback(1, "bug", "The download button did nothing for me")
    assert _login(client).status_code in (200, 302)
    r = client.get("/admin/api/feedback")
    data = r.get_json()
    assert r.status_code == 200 and data["total"] == 2 and data["summary"]["count"] == 2
    only_bugs = client.get("/admin/api/feedback?rating=1").get_json()
    assert only_bugs["total"] == 1 and only_bugs["items"][0]["category"] == "bug"
    assert client.get("/admin/api/feedback?rating=9").status_code == 400
    assert client.get("/admin/api/feedback?limit=abc").status_code == 400


def test_dashboard_has_feedback_card(client):
    _login(client)
    assert b'id="feedbackList"' in client.get("/admin/").data
