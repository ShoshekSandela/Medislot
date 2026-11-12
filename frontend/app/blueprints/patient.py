from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import (
    Blueprint, flash, redirect, render_template, request, session, url_for
)

from ..api_client import APIError, get, post, put
from ..auth import clear_session, login_session, patient_required

bp = Blueprint("patient", __name__, url_prefix="/patient")


def _safe_profile():
    try:
        data = get("/patients")
        return data.get("patient", data) if isinstance(data, dict) else {}
    except APIError as exc:
        flash(exc.message, "error")
        return {}


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not email or not password:
            flash("Enter your email and password.", "error")
        else:
            try:
                payload = post("/auth/login", {"email": email, "password": password}, auth=False)
                if payload.get("role") != "patient":
                    flash("This is not a patient account. Please use the correct login.", "error")
                else:
                    # Do not fetch the profile during login and again on the
                    # dashboard. The dashboard performs the single profile GET.
                    login_session(payload)
                    session["patient_name"] = "Patient"
                    return redirect(url_for("patient.dashboard"))
            except APIError as exc:
                message = exc.message or "Unable to sign in."
                if exc.status == 401 and any(term in message.lower() for term in ("not found", "does not exist", "invalid email", "user not found")):
                    message = "Patient account not found. Create a patient account to continue."
                flash(message, "error")
    return render_template("patient_login.html")


@bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        form = request.form
        body = {
            "role": "patient",
            "first_name": form.get("first_name", "").strip(),
            "last_name": form.get("last_name", "").strip(),
            "email": form.get("email", "").strip().lower(),
            "password": form.get("password", ""),
            "phone": form.get("phone", "").strip() or None,
            "date_of_birth": form.get("date_of_birth") or None,
            "gender": form.get("gender") or None,
        }
        try:
            post("/auth/register", body, auth=False)
            flash("Patient account created. Please sign in.", "success")
            return redirect(url_for("patient.login"))
        except APIError as exc:
            flash(exc.message, "error")
    return render_template("patient_register.html")


@bp.get("/dashboard")
@patient_required
def dashboard():
    profile = _safe_profile()
    return render_template("components/patient_dashboard.html", profile=profile, doctors=None, filters={}, selected_doctor=None, availability=None)


@bp.post("/profile")
@patient_required
def update_profile():
    fields = ("first_name", "last_name", "phone", "date_of_birth", "gender")
    body = {field: request.form.get(field) or None for field in fields if field in request.form}
    try:
        put("/patients", body)
        flash("Your profile was updated successfully.", "success")
    except APIError as exc:
        flash(exc.message, "error")
    return redirect(url_for("patient.dashboard"))


@bp.get("/doctors")
@patient_required
def search_doctors():
    filters = {key: request.args.get(key, "").strip() for key in ("name", "hospital", "specialisation", "city")}
    params = {key: value for key, value in filters.items() if value}
    params["page"] = request.args.get("page", "1")
    params["page_size"] = request.args.get("page_size", "10")
    try:
        result = get("/doctors", params=params)
        profile = _safe_profile()
        return render_template("components/patient_dashboard.html", profile=profile, doctors=result, filters=filters, selected_doctor=None, availability=None)
    except APIError as exc:
        flash(exc.message, "error")
        return redirect(url_for("patient.dashboard"))


@bp.get("/doctors/<int:doctor_id>")
@patient_required
def doctor_detail(doctor_id):
    today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    start_date = request.args.get("start_date") or today_ist.isoformat()
    end_date = request.args.get("end_date") or (today_ist + timedelta(days=7)).isoformat()
    try:
        selected = get(f"/doctors/{doctor_id}")
        availability = get(f"/doctors/{doctor_id}/slots", params={"start_date": start_date, "end_date": end_date})
        profile = _safe_profile()
        return render_template("components/patient_dashboard.html", profile=profile, doctors=None, filters={}, selected_doctor=selected, availability=availability)
    except APIError as exc:
        flash(exc.message, "error")
        return redirect(url_for("patient.dashboard"))


@bp.post("/logout")
def logout():
    try:
        if session.get("refreshToken"):
            post("/auth/logout", {"refreshToken": session.get("refreshToken")}, auth=False)
    except APIError:
        pass
    clear_session()
    flash("You have been signed out.", "success")
    return redirect(url_for("patient.login"))


@bp.post("/deactivate")
@patient_required
def deactivate_account():
    from ..api_client import delete
    try:
        delete("/patients")
        clear_session()
        flash("Your patient account has been deactivated.", "success")
        return redirect(url_for("patient.login"))
    except APIError as exc:
        flash(exc.message, "error")
        return redirect(url_for("patient.dashboard"))
