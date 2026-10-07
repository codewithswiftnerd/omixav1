from flask import Blueprint, render_template

from config import Config

workspace_bp = Blueprint("workspace", __name__)


@workspace_bp.get("/clean")
def index():
    return render_template("clean.html", free_mb=Config.FREE_MAX_UPLOAD_MB, pro_mb=Config.PRO_MAX_UPLOAD_MB)
