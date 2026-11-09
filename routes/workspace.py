from flask import Blueprint, redirect

workspace_bp = Blueprint("workspace", __name__)


@workspace_bp.get("/clean")
def index():
    """The cleaning tool IS the home page. /clean used to render the same page at a second URL, which made Google pick
    its own canonical ("Duplicate: Google chose different canonical"). It now 301s to the one real URL."""
    return redirect("/", code=301)
