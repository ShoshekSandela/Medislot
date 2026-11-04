from flask import Blueprint, render_template

bp = Blueprint("patient", __name__)


@bp.get("/patient/login")
def login():
    return render_template(
        "placeholder.html",
        title="Patient login",
        message="Patient sign-in is not available in this dashboard.",
    )
