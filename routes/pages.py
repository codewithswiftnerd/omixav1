import json

from flask import Blueprint, render_template

from config import Config

pages_bp = Blueprint("pages", __name__)


def product_facts() -> dict:
    """ONE place the marketing pages read their numbers from, so FAQ/pricing/batch copy can never
    drift from what the backend enforces."""
    return {
        "free_mb": Config.FREE_MAX_UPLOAD_MB, "pro_mb": Config.PRO_MAX_UPLOAD_MB,
        "ttl_minutes": max(1, Config.JOB_TTL_SECONDS // 60),
        "batch_hours": round(Config.BATCH_RETENTION_SECONDS / 3600, 1),
        "batch_files": Config.BATCH_MAX_FILES, "batch_total_mb": Config.BATCH_MAX_TOTAL_MB,
        "max_rows": f"{Config.MAX_ROWS:,}", "max_cells": f"{Config.MAX_CELLS:,}",
        "batch_enabled": Config.PROCESSING_MODE == "queue",
    }


@pages_bp.get("/")
def landing():
    return render_template("landing.html", **product_facts())


@pages_bp.get("/batch")
def batch():
    """Server-rendered: Free/anonymous visitors get the upgrade state and NO upload UI at all."""
    from accounts import auth
    from routes.batches import limits
    from routes.cleaning import server_side_is_pro
    facts = product_facts()
    if not auth.current_uid():
        state = "signed_out"
    elif not server_side_is_pro():
        state = "free"
    elif not facts["batch_enabled"]:
        state = "unavailable"
    else:
        state = "pro"
    return render_template("batch.html", state=state, limits=limits(), **facts)


@pages_bp.get("/pricing")
def pricing():
    from accounts import paystack
    m, a = Config.PRICE_USD_MONTHLY, Config.PRICE_USD_ANNUAL
    return render_template(
        "pricing.html", usd_monthly=m, usd_annual=a, saves=round(m * 12 - a, 2),
        months_free=round((m * 12 - a) / m, 1) if m else 0,
        ngn_monthly=Config.PRICE_NGN_MONTHLY, ngn_annual=Config.PRICE_NGN_ANNUAL,
        payments_available=paystack.configured(), **product_facts(),
    )


@pages_bp.get("/login")
def login():
    try:
        cfg = json.loads(Config.FIREBASE_WEB_CONFIG_JSON) if Config.FIREBASE_WEB_CONFIG_JSON else {}
    except ValueError:
        cfg = {}
    return render_template("login.html", firebase_config=cfg)


@pages_bp.get("/dashboard")
def dashboard():
    return render_template("dashboard.html", **product_facts())


@pages_bp.get("/profiles/new")
def profile_new():
    return render_template("profile_edit.html", pid="")


@pages_bp.get("/profiles/<pid>")
def profile_edit(pid):
    return render_template("profile_edit.html", pid=pid)


@pages_bp.get("/sessions/<sid>")
def session_view(sid):
    return render_template("session.html", sid=sid)
