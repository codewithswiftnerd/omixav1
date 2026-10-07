from flask import Blueprint, Response, jsonify

from accounts import auth
from accounts.entitlements import pro_required
from accounts.store import get_store
from proquality import reports

sessions_bp = Blueprint("sessions", __name__)


def _get(sid):
    return get_store().get_session(auth.current_uid(), sid)


def _slug(name):
    return "".join(c if c.isalnum() else "-" for c in (name or "dataset").rsplit(".", 1)[0])[:40].strip("-") or "dataset"


@sessions_bp.get("/")
@pro_required
def list_sessions():
    items = get_store().list_sessions(auth.current_uid(), limit=100)
    slim = [{k: s.get(k) for k in ("id", "dataset_name", "created_at", "score_before", "score_after", "profile_name",
                                  "compliance", "issue_count", "status")} for s in items]
    return jsonify({"sessions": slim}), 200


@sessions_bp.get("/<sid>")
@pro_required
def get_session(sid):
    s = _get(sid)
    return (jsonify({"session": s}), 200) if s else (jsonify({"error": "Not found"}), 404)


@sessions_bp.delete("/<sid>")
@pro_required
def delete_session(sid):
    return (jsonify({"status": "deleted"}), 200) if get_store().delete_session(auth.current_uid(), sid) else (jsonify({"error": "Not found"}), 404)


def _download(sid, kind):
    s = _get(sid)
    if not s:
        return jsonify({"error": "Not found"}), 404
    base = f"omixa-{_slug(s['dataset_name'])}"
    if kind == "report":
        return Response(reports.quality_report_pdf(s), mimetype="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{base}-quality-report.pdf"'})
    if kind == "changes":
        return Response(reports.what_changed_text(s), mimetype="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{base}-what-changed.txt"'})
    return Response(reports.change_log_csv(s), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{base}-change-log.csv"'})


@sessions_bp.get("/<sid>/report.pdf")
@pro_required
def report_pdf(sid):
    return _download(sid, "report")


@sessions_bp.get("/<sid>/what-changed.txt")
@pro_required
def what_changed(sid):
    return _download(sid, "changes")


@sessions_bp.get("/<sid>/change-log.csv")
@pro_required
def change_log(sid):
    return _download(sid, "log")
