from datetime import date, datetime
from zoneinfo import ZoneInfo
import uuid

from flask import (
    Blueprint, flash, redirect, render_template, request, session, url_for
)

from ..api_client import APIError, get, post, put
from ..auth import clear_session, login_session, patient_required

bp = Blueprint("patient", __name__, url_prefix="/patient")


def _today_ist():
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


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
                    login_session(payload)
                    profile = _safe_profile()
                    session["patient_name"] = (profile.get("first_name", "") + " " + (profile.get("last_name") or "")).strip() or "Patient"
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
        date_of_birth = form.get("date_of_birth") or None
        if date_of_birth:
            try:
                parsed_dob = date.fromisoformat(date_of_birth)
            except (TypeError, ValueError):
                flash("Enter a valid date of birth.", "error")
                return render_template(
                    "patient_register.html", today=_today_ist().isoformat()
                )
            if parsed_dob > _today_ist():
                flash("Date of birth cannot be in the future.", "error")
                return render_template(
                    "patient_register.html", today=_today_ist().isoformat()
                )

        body = {
            "role": "patient",
            "first_name": form.get("first_name", "").strip(),
            "last_name": form.get("last_name", "").strip(),
            "email": form.get("email", "").strip().lower(),
            "password": form.get("password", ""),
            "phone": form.get("phone", "").strip() or None,
            "date_of_birth": date_of_birth,
            "gender": form.get("gender") or None,
        }
        try:
            post("/auth/register", body, auth=False)
            flash("Patient account created. Please sign in.", "success")
            return redirect(url_for("patient.login"))
        except APIError as exc:
            flash(exc.message, "error")
    return render_template("patient_register.html", today=_today_ist().isoformat())


@bp.get("/dashboard")
@patient_required
def dashboard():
    """Show the patient profile and the first page of active doctors."""
    profile = _safe_profile()
    doctors = None
    try:
        # Load the first page for the overview and the full capped directory
        # for dynamic search-filter options (the API caps page_size at 100).
        doctors = get("/doctors", params={"page": 1, "page_size": 10})
        directory = get("/doctors", params={"page": 1, "page_size": 100})
    except APIError as exc:
        flash(exc.message, "error")
        directory = doctors or {"doctors": [], "pagination": {"total": 0, "total_pages": 1, "page": 1}}
    return render_template(
        "components/patient_dashboard.html",
        profile=profile, doctors=doctors, directory=directory, today=_today_ist().isoformat(),
        filters={}, selected_doctor=None, availability=None,
        bookings=None, active_page="overview",
    )


@bp.post("/profile")
@patient_required
def update_profile():
    fields = ("first_name", "last_name", "phone", "date_of_birth", "gender")
    body = {field: request.form.get(field) or None for field in fields if field in request.form}
    dob = body.get("date_of_birth")
    if dob:
        try:
            parsed_dob = date.fromisoformat(dob)
        except (TypeError, ValueError):
            flash("Enter a valid date of birth.", "error")
            return redirect(url_for("patient.dashboard") + "#profile")
        if parsed_dob > _today_ist():
            flash("Date of birth cannot be in the future.", "error")
            return redirect(url_for("patient.dashboard") + "#profile")
    try:
        put("/patients", body)
        flash("Your profile was updated successfully.", "success")
    except APIError as exc:
        flash(exc.message, "error")
    return redirect(url_for("patient.dashboard") + "#profile")


@bp.get("/doctors")
@patient_required
def search_doctors():
    filters = {key: request.args.get(key, "").strip() for key in ("name", "hospital", "specialisation", "city")}
    params = {key: value for key, value in filters.items() if value}
    params["page"] = request.args.get("page", "1")
    params["page_size"] = request.args.get("page_size", "10")
    try:
        result = get("/doctors", params=params)
        try:
            directory = get("/doctors", params={"page": 1, "page_size": 100})
        except APIError:
            directory = result
        profile = _safe_profile()
        return render_template(
            "components/patient_dashboard.html", profile=profile,
            doctors=result, directory=directory, filters=filters,
            selected_doctor=None, availability=None, bookings=None,
            active_page="find-doctors", today=_today_ist().isoformat(),
        )
    except APIError as exc:
        flash(exc.message, "error")
        return redirect(url_for("patient.dashboard"))


@bp.get("/doctors/<int:doctor_id>")
@patient_required
def doctor_detail(doctor_id):
    today_ist = _today_ist()
    selected_date = request.args.get("date") or request.args.get("start_date") or today_ist.isoformat()
    try:
        selected_date = date.fromisoformat(selected_date).isoformat()
    except (TypeError, ValueError):
        selected_date = today_ist.isoformat()
    try:
        selected = get(f"/doctors/{doctor_id}")
        availability = get(f"/doctors/{doctor_id}/slots", params={"start_date": selected_date, "end_date": selected_date})
        profile = _safe_profile()

        # Enrich API slots for the patient-facing display. Slot timing is
        # generated from consultation duration without an additional buffer.
        consultation_types = selected.get("consultation_types") or []
        type_by_id = {
            str(item.get("consultation_type_id")): item
            for item in consultation_types
        }
        display_slots = []
        raw_slots = availability.get("slots", []) if isinstance(availability, dict) else []
        for slot in raw_slots:
            slot = dict(slot)
            raw_time = str(slot.get("slotTime") or "")
            try:
                parsed_time = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                if parsed_time.tzinfo is None:
                    parsed_time = parsed_time.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
                parsed_time = parsed_time.astimezone(ZoneInfo("Asia/Kolkata"))
                slot["display_date"] = parsed_time.strftime("%d %b %Y")
                slot["display_time"] = parsed_time.strftime("%I:%M %p").lstrip("0")
            except (TypeError, ValueError):
                slot["display_date"] = raw_time[:10] or "Date unavailable"
                slot["display_time"] = raw_time[11:16] or "Time unavailable"

            consultation = type_by_id.get(str(slot.get("consultationTypeId")), {})
            slot["consultation_type_name"] = consultation.get("type_name", "Consultation")
            slot["duration_minutes"] = consultation.get("duration_minutes")
            slot["idempotency_key"] = str(uuid.uuid4())
            display_slots.append(slot)
        try:
            directory = get("/doctors", params={"page": 1, "page_size": 100})
        except APIError:
            directory = {"doctors": [], "pagination": {"total": 0, "total_pages": 1, "page": 1}}
        return render_template(
            "components/patient_dashboard.html", profile=profile,
            doctors=None, directory=directory, filters={},
            selected_doctor=selected, availability=availability,
            display_slots=display_slots,
            bookings=None, active_page="doctor-detail", today=today_ist.isoformat(),
            selected_date=selected_date,
        )
    except APIError as exc:
        flash(exc.message, "error")
        return redirect(url_for("patient.dashboard"))


@bp.post("/book")
@patient_required
def book_slot():
    try:
        doctor_id = int(request.form.get("doctor_id", ""))
    except (TypeError, ValueError):
        flash("The selected doctor is invalid.", "error")
        return redirect(url_for("patient.search_doctors"))

    slot_time = (request.form.get("slot_time") or "").strip()
    slot_id = (request.form.get("slot_id") or "").strip()
    idempotency_key = (request.form.get("idempotency_key") or "").strip()
    selected_date = (request.form.get("date") or slot_time[:10]).strip()

    if not slot_time or not slot_id or not idempotency_key:
        flash("The selected slot details are incomplete. Please refresh the slots and try again.", "error")
        return redirect(url_for("patient.doctor_detail", doctor_id=doctor_id, date=selected_date))

    try:
        result = post(
            "/appointments",
            {"doctor_id": doctor_id, "slot_time": slot_time, "slot_id": slot_id},
            headers={"Idempotency-Key": idempotency_key},
        )
        appointment = result.get("appointment", {}) if isinstance(result, dict) else {}
        flash(
            f"Appointment booked successfully. Booking ID: {appointment.get('appointment_id', 'confirmed')}.",
            "success",
        )
    except APIError as exc:
        flash(exc.message, "error")

    return redirect(url_for("patient.doctor_detail", doctor_id=doctor_id, date=selected_date))


@bp.post("/booking-history/<int:appointment_id>/cancel")
@patient_required
def cancel_booking(appointment_id):
    try:
        result = post(f"/appointments/{appointment_id}/cancel", {})
        cancellation = result.get("cancellation", {}) if isinstance(result, dict) else {}
        flash(
            f"Appointment cancelled. Refund recorded: ₹{cancellation.get('refund_amount', '0.00')} "
            f"({cancellation.get('refund_percentage', '0')}% of the consultation fee).",
            "success",
        )
    except APIError as exc:
        flash(exc.message, "error")
    return redirect(url_for("patient.booking_history") + "#booking-history")


@bp.get("/booking-history")
@patient_required
def booking_history():
    profile = _safe_profile()
    try:
        result = get("/patients/appointments")
        bookings = result.get("appointments", result) if isinstance(result, dict) else result
        if not isinstance(bookings, list):
            bookings = []
    except APIError as exc:
        flash(exc.message, "error")
        bookings = None
    refund_by_appointment = {}
    try:
        refund_result = get("/appointments/refunds")
        refund_rows = refund_result.get("refunds", []) if isinstance(refund_result, dict) else []
        if isinstance(refund_rows, list):
            refund_by_appointment = {
                str(item.get("appointment_id")): item
                for item in refund_rows
                if isinstance(item, dict)
            }
    except APIError:
        # Booking history remains available even if refund history cannot be loaded.
        refund_by_appointment = {}
    try:
        directory = get("/doctors", params={"page": 1, "page_size": 100})
    except APIError:
        directory = {"doctors": [], "pagination": {"total": 0, "total_pages": 1, "page": 1}}

    # Appointment responses contain the doctor ID but not the browser-ready
    # profile image URL. Reuse the doctor directory's short-lived S3 URLs.
    directory_doctors = directory.get("doctors", []) if isinstance(directory, dict) else []
    photo_by_doctor = {
        str(item.get("doctor_id")): item.get("profile_photo_display_url")
        for item in directory_doctors
        if isinstance(item, dict) and item.get("doctor_id") and item.get("profile_photo_display_url")
    }
    if isinstance(bookings, list):
        for booking in bookings:
            if isinstance(booking, dict):
                booking["profile_photo_display_url"] = (
                    booking.get("profile_photo_display_url")
                    or photo_by_doctor.get(str(booking.get("doctor_id")))
                )

    return render_template(
        "components/patient_dashboard.html", profile=profile,
        doctors=None, directory=directory, filters={},
        selected_doctor=None, availability=None, bookings=bookings,
        refund_by_appointment=refund_by_appointment,
        active_page="booking-history", today=_today_ist().isoformat(),
    )


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
