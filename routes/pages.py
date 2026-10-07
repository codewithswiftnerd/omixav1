import json

from flask import Blueprint, render_template

from config import Config

pages_bp = Blueprint("pages", __name__)


@pages_bp.get("/")
def landing():
    return render_template("landing.html", free_mb=Config.FREE_MAX_UPLOAD_MB, pro_mb=Config.PRO_MAX_UPLOAD_MB)


@pages_bp.get("/pricing")
def pricing():
    from accounts import paystack
    m, a = Config.PRICE_USD_MONTHLY, Config.PRICE_USD_ANNUAL
    return render_template(
        "pricing.html", usd_monthly=m, usd_annual=a, saves=round(m * 12 - a, 2),
        months_free=round((m * 12 - a) / m, 1) if m else 0,
        ngn_monthly=Config.PRICE_NGN_MONTHLY, ngn_annual=Config.PRICE_NGN_ANNUAL,
        free_mb=Config.FREE_MAX_UPLOAD_MB, pro_mb=Config.PRO_MAX_UPLOAD_MB,
        payments_available=paystack.configured(),
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
    return render_template("dashboard.html")


@pages_bp.get("/profiles/new")
def profile_new():
    return render_template("profile_edit.html", pid="")


@pages_bp.get("/profiles/<pid>")
def profile_edit(pid):
    return render_template("profile_edit.html", pid=pid)


@pages_bp.get("/sessions/<sid>")
def session_view(sid):
    return render_template("session.html", sid=sid)
