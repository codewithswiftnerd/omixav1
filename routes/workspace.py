from flask import Blueprint, render_template

workspace_bp = Blueprint("workspace", __name__)


@workspace_bp.get("/clean")
def index():
    return render_template("clean.html")
