"""
Upload validation: extension whitelist, content sniffing beyond the
extension (utils/file_handler.validate_upload_content), and the
platform-level size limit.
"""

import io
import zipfile

import openpyxl
import pytest


def _post(client, filename, content_bytes):
    data = {"file": (io.BytesIO(content_bytes), filename)}
    return client.post("/api/upload/", data=data, content_type="multipart/form-data")


def _real_xlsx_bytes():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Name", "Age"])
    ws.append(["John", 24])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_accepts_valid_csv(client):
    r = _post(client, "people.csv", b"Name,Age\nJohn,24\n")
    assert r.status_code == 201
    assert r.get_json()["filename"] == "people.csv"


def test_accepts_valid_xlsx(client):
    r = _post(client, "people.xlsx", _real_xlsx_bytes())
    assert r.status_code == 201


def test_rejects_disallowed_extension(client):
    r = _post(client, "people.exe", b"MZ\x90\x00fake binary content")
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_rejects_empty_filename(client):
    r = client.post(
        "/api/upload/", data={"file": (io.BytesIO(b"x"), "")}, content_type="multipart/form-data"
    )
    assert r.status_code == 400


def test_rejects_no_file_part(client):
    r = client.post("/api/upload/", data={}, content_type="multipart/form-data")
    assert r.status_code == 400


def test_rejects_binary_content_disguised_as_csv(client):
    """Renaming an arbitrary binary file to .csv must not be enough
    to get it accepted, see validate_upload_content()."""
    r = _post(client, "innocent.csv", b"\x00\x01\x02binary garbage\x00\xff")
    assert r.status_code == 400


def test_rejects_non_zip_content_disguised_as_xlsx(client):
    r = _post(client, "innocent.xlsx", b"this is not a real xlsx file at all")
    assert r.status_code == 400


def test_rejects_random_zip_disguised_as_xlsx(client):
    """A zip file that isn't actually an Excel workbook (no xl/
    directory) must be rejected even though the outer magic bytes
    match a zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", "not a spreadsheet")
    r = _post(client, "innocent.xlsx", buf.getvalue())
    assert r.status_code == 400


def test_rejects_macro_enabled_workbook_disguised_as_xlsx(client):
    """An .xlsm (macro-enabled) file renamed to .xlsx must be
    rejected outright, see validate_upload_content()'s vbaProject.bin
    check."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/workbook.xml", "<workbook/>")
        zf.writestr("xl/vbaProject.bin", b"\x00\x01fakevba")
    r = _post(client, "macro.xlsx", buf.getvalue())
    assert r.status_code == 400
    assert "macro" in r.get_json()["error"].lower()


def test_oversized_upload_rejected(app):
    app.config["MAX_CONTENT_LENGTH"] = 100  # bytes, just for this test
    client = app.test_client()
    try:
        r = _post(client, "big.csv", b"Name,Age\n" + b"a,1\n" * 100)
        assert r.status_code == 413
    finally:
        from config import Config
        app.config["MAX_CONTENT_LENGTH"] = Config.MAX_CONTENT_LENGTH
