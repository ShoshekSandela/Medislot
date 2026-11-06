from flask import (

    Blueprint, render_template, request, redirect, url_for,

    flash, session, current_app, abort

)

from datetime import date, datetime, timedelta, time

from zoneinfo import ZoneInfo

from concurrent.futures import ThreadPoolExecutor

from contextvars import copy_context

from werkzeug.utils import secure_filename



from ..api_client import (

    get, post, put, delete, request as api_request,

    APIError, upload_to_presigned

)

from ..auth import doctor_required, login_session, clear_session

from ..formatters import *



import re

import io



bp = Blueprint("doctor", __name__, url_prefix="/doctor")



ALLOWED = {

    "image/jpeg": "jpg",

    "image/png": "png",

    "image/webp": "webp"

}





def password_errors(p):

    e = []

    if not 12 <= len(p) <= 72:

        e.append("Use 12–72 characters")

    if not re.search(r"[a-z]", p):

        e.append("Add a lowercase letter")

    if not re.search(r"[A-Z]", p):

        e.append("Add an uppercase letter")

    if not re.search(r"\d", p):

        e.append("Add a number")

    if not re.search(r"[^A-Za-z0-9]", p):

        e.append("Add a special character")

    return e





def email_ok(v):

    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v or ""))





def page(title, active, **kw):

    return render_template(

        "doctor_page.html",

        title=title,

        active=active,

        **kw

    )





def safe_call(fn, *a, **k):

    try:

        return fn(*a, **k), None

    except APIError as e:

        if e.status == 401:

            flash("Session expired, please sign in again", "warning")

            abort(401)

        if e.status == 403:

            abort(403)

        flash(e.message, "error")

        return None, e





def clear_schedule_cache():

    """Remove cached schedule data after availability/blackout changes."""

    session.pop("schedule_cache", None)





def generation_message(result, success_message, failure_message):
    """Display the actual slot-generation status returned by Doctor API."""
    if not isinstance(result, dict):
        flash(
            "Availability saved, but slot-generation status was not returned.",
            "warning"
        )
        return

    generation = result.get("slot_generation")
    if not isinstance(generation, dict):
        flash(
            "Availability saved, but slot generation is not confirmed. "
            "Check the Doctor Lambda and Slot Materializer logs.",
            "warning"
        )
        return

    status = str(generation.get("status", "")).lower()
    if status in ("completed", "success", "succeeded"):
        flash(success_message, "success")
    elif status in ("failed", "error"):
        flash(str(generation.get("message") or failure_message), "error")
    else:
        flash(
            "Availability saved, but slot generation is not confirmed. "
            "Check the Slot Materializer logs.",
            "warning"
        )



@bp.route("/login", methods=["GET", "POST"])

def login():

    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()

        password = request.form.get("password", "")



        if not email_ok(email) or not password:

            flash("Enter a valid email and password.", "error")

        else:

            try:

                data = post(

                    "/auth/login",

                    {"email": email, "password": password},

                    auth=False

                )



                if data.get("role") != "doctor":

                    flash(

                        "This is a patient account. Use Patient login.",

                        "error"

                    )

                else:

                    login_session(data)



                    try:

                        p = get("/doctors/profile")

                        session["profile"] = p or {}

                        session["doctor_name"] = (

                            p.get("first_name", "") + " " +

                            p.get("last_name", "")

                        ).strip() or "Doctor"

                        session["profile_status"] = p.get(

                            "profile_status", "—"

                        )

                    except APIError:

                        pass



                    return redirect(url_for("doctor.dashboard"))



            except APIError as e:

                flash(e.message, "error")



    return render_template("auth.html", mode="login")





@bp.route("/register", methods=["GET", "POST"])

def register():

    if request.method == "POST":

        f = request.form

        email = f.get("email", "").strip().lower()

        pw = f.get("password", "")

        errs = password_errors(pw)



        if not email_ok(email):

            errs.append("Enter a valid email")



        if (

            not f.get("first_name", "").strip()

            or len(f.get("first_name", "")) > 100

        ):

            errs.append("First name is required (max 100)")



        if (

            not f.get("registration_number", "").strip()

            or len(f.get("registration_number", "")) > 100

        ):

            errs.append("Registration number is required (max 100)")



        try:

            exp = int(f.get("experience_years") or 0)

            assert 0 <= exp <= 100

        except (ValueError, AssertionError):

            errs.append("Experience must be between 0 and 100")

            exp = 0



        if errs:

            for x in errs:

                flash(x, "error")

        else:

            body = {

                "role": "doctor",

                "email": email,

                "password": pw,

                "first_name": f["first_name"].strip(),

                "last_name": f.get("last_name", "").strip(),

                "registration_number": f["registration_number"].strip(),

                "qualification": f.get("qualification", "").strip() or None,

                "experience_years": exp

            }



            if f.get("hospital_id"):

                body["hospital_id"] = int(f["hospital_id"])



            try:

                post("/auth/register", body, auth=False)

                flash(

                    "Registration submitted. You can sign in when your profile is active.",

                    "success"

                )

                return redirect(url_for("doctor.login"))



            except (APIError, ValueError) as e:

                flash(

                    getattr(e, "message", "Hospital ID must be numeric"),

                    "error"

                )



    return render_template("auth.html", mode="register")





@bp.post("/logout")

@doctor_required

def logout():

    try:

        post(

            "/auth/logout",

            {"refreshToken": session.get("refreshToken")}

        )

    except APIError:

        pass



    clear_session()

    flash("You have been signed out.", "success")

    return redirect(url_for("landing.index"))





@bp.get("/dashboard")

@doctor_required

def dashboard():

    today = datetime.now(

        ZoneInfo("Asia/Kolkata")

    ).date().isoformat()



    errors = []

    schedule_data = None

    availability_rows = None

    blackout_rows = None



    try:

        schedule_data = get("/doctors/schedule", {"date": today})

    except APIError as e:

        errors.append(e.message)



    try:

        availability_rows = get("/doctors/availability")

    except APIError as e:

        errors.append(e.message)



    try:

        blackout_rows = get("/doctors/blackouts")

    except APIError as e:

        errors.append(e.message)



    slots = (

        schedule_data.get("slots", [])

        if isinstance(schedule_data, dict)

        else []

    )



    appts = (

        schedule_data.get("appointments", [])

        if isinstance(schedule_data, dict)

        else []

    )



    booked = sum(

        int(x.get("bookedCount", 0) or 0)

        for x in slots

    )



    capacity = sum(

        int(x.get("capacity", 0) or 0)

        for x in slots

    )



    upcoming = sorted(

        [

            a for a in appts

            if a.get("status") == "BOOKED"

            and (

                a.get("start_at_ist") or ""

            ) >= datetime.now(

                ZoneInfo("Asia/Kolkata")

            ).strftime("%Y-%m-%d %H:%M:%S")

        ],

        key=lambda x: x.get("start_at_ist", "")

    )



    active_windows = (

        sum(

            1 for a in availability_rows

            if as_bool(a.get("is_active"))

        )

        if isinstance(availability_rows, list)

        else None

    )



    future_blackouts = sorted(

        [

            b for b in (

                blackout_rows

                if isinstance(blackout_rows, list)

                else []

            )

            if (

                b.get("start_at_ist") or ""

            ) >= datetime.now(

                ZoneInfo("Asia/Kolkata")

            ).strftime("%Y-%m-%d %H:%M:%S")

        ],

        key=lambda x: x.get("start_at_ist", "")

    )



    stats = [

        {

            "label": "Slots today",

            "value": len(slots) if schedule_data is not None else "—",

            "note": "Today's generated slots"

        },

        {

            "label": "Booked seats",

            "value": booked if schedule_data is not None else "—",

            "note": "Across today's slots"

        },

        {

            "label": "Free seats",

            "value": capacity - booked if schedule_data is not None else "—",

            "note": "Remaining capacity"

        },

        {

            "label": "Utilisation",

            "value": (

                f"{(booked / capacity * 100):.0f}%"

                if schedule_data is not None and capacity

                else ("0%" if schedule_data is not None else "—")

            ),

            "note": "Booked seats / capacity"

        }

    ]



    return page(

        "Overview",

        "dashboard",

        stats=stats,

        next_patient=upcoming[0] if upcoming else None,

        active_windows=active_windows,

        next_blackout=(

            future_blackouts[0].get("start_at_ist")

            if future_blackouts else None

        ),

        overview_errors=errors

    )





@bp.route("/profile", methods=["GET", "POST"])

@doctor_required

def profile():

    if request.method == "POST":

        f = request.form

        body = {}



        # Always submit the current form values. Session profile data may be stale
        # after another request or an earlier failed save.
        for key in ("first_name", "last_name", "qualification"):
            body[key] = f.get(key, "").strip()



        try:

            experience = int(f.get("experience_years", 0))



            if not 0 <= experience <= 100:

                raise ValueError("Experience must be between 0 and 100")



            body["experience_years"] = experience



            raw_ids = f.getlist("specialisation_ids")

            selected_ids = []



            for value in raw_ids:

                ident = int(value)



                if ident <= 0:

                    raise ValueError("Invalid specialisation selection.")



                selected_ids.append(ident)



            body["specialisation_ids"] = sorted(set(selected_ids))



            if body:

                put("/doctors/profile", body)

                # Reload the persisted profile from RDS instead of keeping the
                # update response/message object in the browser session.
                refreshed_profile = get("/doctors/profile")
                if isinstance(refreshed_profile, dict):
                    session["profile"] = refreshed_profile
                    session["doctor_name"] = (
                        f"{refreshed_profile.get('first_name', '')} "
                        f"{refreshed_profile.get('last_name', '')}"
                    ).strip() or "Doctor"
                    session["profile_status"] = refreshed_profile.get(
                        "profile_status", "—"
                    )

                flash("Profile updated successfully.", "success")



        except ValueError as e:

            flash(str(e) or "Please check the profile details.", "error")



        except APIError as e:

            flash(e.message, "error")



        return redirect(url_for("doctor.profile"))



    profile_data, profile_error = safe_call(

        get,

        "/doctors/profile"

    )



    profile_data = profile_data or {}

    session["profile"] = profile_data



    session["doctor_name"] = (

        (

            profile_data.get("first_name", "") + " " +

            profile_data.get("last_name", "")

        ).strip() or "Doctor"

    )



    session["profile_status"] = profile_data.get(

        "profile_status", "—"

    )



    specialisations, specialisation_error = safe_call(

        get,

        "/doctors/specialisations"

    )



    return page(

        "My profile",

        "profile",

        profile=profile_data,

        specialisations=(

            specialisations

            if isinstance(specialisations, list)

            else []

        ),

        error=profile_error or specialisation_error

    )





@bp.post("/profile/photo")

@doctor_required

def photo():

    f = request.files.get("photo")



    if not f or not f.filename:

        flash("Choose a photo first.", "error")

        return redirect(url_for("doctor.profile"))



    content = f.mimetype



    if content not in ALLOWED:

        flash("Use JPEG, PNG or WebP.", "error")

        return redirect(url_for("doctor.profile"))



    blob = f.read(5 * 1024 * 1024 + 1)



    if len(blob) > 5 * 1024 * 1024:

        flash("Photo must be 5 MB or smaller.", "error")

        return redirect(url_for("doctor.profile"))



    try:

        name = secure_filename(f.filename) or (

            "profile." + ALLOWED[content]

        )



        result = post(

            "/doctors/photo",

            {

                "fileName": name,

                "contentType": content

            }

        )



        upload_to_presigned(

            result["uploadUrl"],

            content,

            blob

        )



        refreshed = get("/doctors/profile")

        session["profile"] = refreshed or {}



        if (

            refreshed

            and refreshed.get("profile_photo_display_url")

        ):

            flash("Profile photo uploaded successfully.", "success")

        else:

            flash(

                "Photo was uploaded, but the display URL is unavailable. "

                "Check the Doctor Lambda S3 read permission and bucket configuration.",

                "warning"

            )



    except (APIError, KeyError) as e:

        flash(

            getattr(e, "message", "Photo upload failed"),

            "error"

        )



    return redirect(url_for("doctor.profile"))





@bp.route("/consultation-types", methods=["GET", "POST"])

@doctor_required

def consultation_types():

    if request.method == "POST":

        f = request.form

        name = f.get("type_name", "").strip()

        camp = name == "Health Camp Session"



        try:

            duration = int(f.get("duration_minutes"))

            fee = float(f.get("consultation_fee"))

            capacity = int(f.get("capacity") or 1)



            if duration <= 0 or fee < 0:

                raise ValueError(

                    "Duration must be positive and fee cannot be negative"

                )



            if camp and not (

                duration == 60 and 2 <= capacity <= 10

            ):

                raise ValueError(

                    "Health Camp Session must be 60 minutes with capacity 2–10"

                )



            if not camp:

                capacity = 1



            body = {

                "type_name": name,

                "duration_minutes": duration,

                "consultation_fee": fee,

                "capacity": capacity,

                "description": f.get("description", "").strip(),

                "is_active": f.get("is_active") == "on"

            }



            ident = f.get("consultation_type_id")



            if ident:

                put(

                    f"/doctors/consultation-types/{int(ident)}",

                    body

                )

            else:

                post("/doctors/consultation-types", body)



            flash("Consultation type saved.", "success")

            return redirect(url_for("doctor.consultation_types"))



        except (ValueError, APIError) as e:

            flash(

                getattr(e, "message", str(e)),

                "error"

            )



    data, err = safe_call(

        get,

        "/doctors/consultation-types"

    )



    return page(

        "Consultation types",

        "types",

        types=data if isinstance(data, list) else [],

        error=err

    )





@bp.post("/consultation-types/<int:ident>/deactivate")

@doctor_required

def deactivate_type(ident):

    try:

        delete(f"/doctors/consultation-types/{ident}")

        flash("Consultation type deactivated.", "success")



    except APIError as e:

        flash(e.message, "error")



    return redirect(url_for("doctor.consultation_types"))





@bp.route("/availability", methods=["GET", "POST"])

@doctor_required

def availability():

    if request.method == "POST":

        f = request.form

        ident = f.get("availability_id")



        try:

            body = {

                "hospital_id": int(f.get("hospital_id")),

                "consultation_type_id": int(

                    f.get("consultation_type_id")

                ),

                "day_of_week": int(f.get("day_of_week")),

                "start_time": normalise_time_seconds(

                    f.get("start_time")

                ),

                "end_time": normalise_time_seconds(

                    f.get("end_time")

                ),

                "effective_from": f.get("effective_from"),

                "effective_until": (

                    f.get("effective_until") or None

                ),

                "is_active": f.get("is_active") == "on"

            }



            if body["end_time"] <= body["start_time"]:

                raise ValueError(

                    "End time must be later than start time"

                )



            if (

                body["effective_until"]

                and body["effective_until"] < body["effective_from"]

            ):

                raise ValueError(

                    "Effective-until cannot precede effective-from"

                )



            if ident:

                result = put(

                    f"/doctors/availability/{int(ident)}",

                    body

                )

            else:

                result = post(

                    "/doctors/availability",

                    body

                )



            clear_schedule_cache()



            generation_message(

                result,

                "Availability saved and schedule slots regenerated.",

                "Availability saved, but slot generation did not complete. Check Slot Materializer logs."

            )



            return redirect(url_for("doctor.availability"))



        except (ValueError, APIError) as e:

            flash(

                getattr(e, "message", str(e)),

                "error"

            )



    rows, err = safe_call(

        get,

        "/doctors/availability"

    )



    types, _ = safe_call(

        get,

        "/doctors/consultation-types"

    )



    rows = rows if isinstance(rows, list) else []

    types = types if isinstance(types, list) else []



    hospitals = sorted({

        r.get("hospital_id")

        for r in rows

        if r.get("hospital_id") is not None

    })



    return page(

        "Weekly availability",

        "availability",

        availability=rows,

        types=types,

        hospitals=hospitals,

        error=err,

        days=[

            "Sunday", "Monday", "Tuesday", "Wednesday",

            "Thursday", "Friday", "Saturday"

        ]

    )





@bp.post("/availability/<int:ident>/delete")

@doctor_required

def delete_availability(ident):

    try:

        result = delete(

            f"/doctors/availability/{ident}"

        )



        clear_schedule_cache()



        generation_message(

            result,

            "Availability deleted and schedule refreshed.",

            "Availability deleted, but slot refresh failed. Check Lambda logs."

        )



    except APIError as e:

        flash(e.message, "error")



    return redirect(url_for("doctor.availability"))





@bp.route("/blackouts", methods=["GET", "POST"])

@doctor_required

def blackouts():

    if request.method == "POST":

        f = request.form

        start = f.get("start_date")

        end = f.get("end_date") or start



        if f.get("full_day") == "on":

            start_at, end_at = full_day_range(start, end)

        else:

            start_at = f"{start} {f.get('start_time')}:00"

            end_at = f"{end} {f.get('end_time')}:00"



        try:

            if end_at <= start_at:

                raise ValueError("End must be after start")



            result = post(

                "/doctors/blackouts",

                {

                    "hospital_id": int(f.get("hospital_id")),

                    "start_at_ist": start_at,

                    "end_at_ist": end_at,

                    "reason": f.get("reason", "").strip()

                }

            )



            clear_schedule_cache()



            generation_message(

                result,

                "Blackout added and schedule refreshed.",

                "Blackout saved, but slot refresh failed. Check Lambda logs."

            )



            return redirect(url_for("doctor.blackouts"))



        except (ValueError, APIError) as e:

            flash(

                getattr(e, "message", str(e)),

                "error"

            )



    rows, err = safe_call(

        get,

        "/doctors/blackouts"

    )



    rows = rows if isinstance(rows, list) else []

    rows.sort(

        key=lambda x: x.get("start_at_ist", "")

    )



    return page(

        "Blackouts",

        "blackouts",

        blackouts=rows,

        error=err,

        hospitals=sorted({

            r.get("hospital_id")

            for r in rows

            if r.get("hospital_id") is not None

        })

    )





@bp.post("/blackouts/<int:ident>/delete")

@doctor_required

def delete_blackout(ident):

    try:

        result = delete(

            f"/doctors/blackouts/{ident}"

        )



        clear_schedule_cache()



        generation_message(

            result,

            "Blackout deleted and schedule refreshed.",

            "Blackout deleted, but slot refresh failed. Check Lambda logs."

        )



    except APIError as e:

        flash(e.message, "error")



    return redirect(url_for("doctor.blackouts"))





@bp.get("/schedule")

@doctor_required

def schedule():

    timezone = ZoneInfo("Asia/Kolkata")



    day = request.args.get(

        "date",

        datetime.now(timezone).date().isoformat()

    )



    view = request.args.get("view", "day")



    if view not in {"day", "week"}:

        view = "day"



    try:

        datetime.strptime(day, "%Y-%m-%d")

    except ValueError:

        day = datetime.now(timezone).date().isoformat()



    dates = (

        [

            (

                date.fromisoformat(day) + timedelta(days=i)

            ).isoformat()

            for i in range(7)

        ]

        if view == "week"

        else [day]

    )



    # Always retrieve the latest schedule from the Doctor API.
    clear_schedule_cache()
    pending = list(dates)
    schedules = {}

    def load(d):

        try:

            result = get(

                "/doctors/schedule",

                {"date": d}

            )



            return d, result, None



        except APIError as e:

            if e.status == 404:

                return d, {

                    "slots": [],

                    "appointments": [],

                    "blackouts": []

                }, None



            return d, None, e



    with ThreadPoolExecutor(max_workers=7) as pool:

        futures = [

            pool.submit(copy_context().run, load, d)

            for d in pending

        ]



        results = [

            future.result()

            for future in futures

        ]



    for d, result, error in results:
        if result is not None:
            schedules[d] = result

    errors = [

        error

        for d, result, error in results

        if error

    ]



    # Fetch consultation types.

    types, type_error = safe_call(

        get,

        "/doctors/consultation-types"

    )



    if type_error:

        errors.append(type_error)



    consultation_types = (

        types if isinstance(types, list) else []

    )



    type_map = {}



    for t in consultation_types:

        if not isinstance(t, dict):

            continue



        ident = (

            t.get("consultation_type_id")

            or t.get("consultationTypeId")

        )



        if ident is not None:

            type_map[str(ident)] = t



    def parse_time(value):

        if not value:

            return None



        try:

            if isinstance(value, time):

                return value



            value = str(value).replace("Z", "")



            if "T" in value:

                value = value.split("T", 1)[1]



            if " " in value:

                value = value.split(" ", 1)[1]



            return time.fromisoformat(value)



        except (ValueError, TypeError):

            return None



    def get_slot_time(item, *keys):

        for key in keys:

            if item.get(key):

                parsed = parse_time(item[key])



                if parsed:

                    return parsed



        return None



    def attach_consultation_type(item):

        ident = (

            item.get("consultationTypeId")

            or item.get("consultation_type_id")

        )



        consultation_type = type_map.get(

            str(ident),

            {}

        )



        item["consultationType"] = consultation_type



        item["consultationTypeName"] = (

            consultation_type.get("type_name")

            or consultation_type.get("typeName")

            or "Unknown"

        )



        item["durationMinutes"] = (

            consultation_type.get("duration_minutes")

            or consultation_type.get("durationMinutes")

        )



        if item.get("consultationFee") is None:

            item["consultationFee"] = (

                consultation_type.get("consultation_fee")

            )



        item["consultationCapacity"] = (

            item.get("capacity")

            or consultation_type.get("capacity")

        )



        return item



    schedule_items = {}



    for d in dates:

        data = schedules.get(d) or {}



        if not isinstance(data, dict):

            data = {}



        slots = data.get("slots") or []

        blackouts = data.get("blackouts") or []



        combined = []



        for original_slot in slots:

            if str(original_slot.get("status", "")).upper() == "INACTIVE":
                continue

            slot = attach_consultation_type(

                dict(original_slot)

            )



            slot_start = get_slot_time(

                slot,

                "startTime",

                "start_time",

                "start"

            )



            slot_end = get_slot_time(

                slot,

                "endTime",

                "end_time",

                "end"

            )



            booked = int(

                slot.get("bookedCount", 0) or 0

            )



            capacity = int(

                slot.get("capacity", 1) or 1

            )



            overlapping_blackout = None



            for blackout in blackouts:

                blackout_start = get_slot_time(

                    blackout,

                    "startTime",

                    "start_time",

                    "start"

                )



                blackout_end = get_slot_time(

                    blackout,

                    "endTime",

                    "end_time",

                    "end"

                )



                if (

                    slot_start

                    and slot_end

                    and blackout_start

                    and blackout_end

                    and slot_start < blackout_end

                    and slot_end > blackout_start

                ):

                    overlapping_blackout = blackout

                    break



            slot["isBlackout"] = bool(

                overlapping_blackout

            )



            if overlapping_blackout:

                slot["status"] = "BLACKOUT"

                slot["blackoutReason"] = (

                    overlapping_blackout.get("reason")

                    or overlapping_blackout.get("description")

                    or "Doctor unavailable"

                )



            elif booked >= capacity:

                slot["status"] = "BOOKED"



            elif booked > 0:

                slot["status"] = "PARTIALLY_BOOKED"



            else:

                slot["status"] = "OPEN"



            slot["itemType"] = "SLOT"

            combined.append(slot)



        for appointment in data.get("appointments", []):

            attach_consultation_type(appointment)



        for blackout in blackouts:

            blackout_item = dict(blackout)



            blackout_item["itemType"] = "BLACKOUT"

            blackout_item["status"] = "BLACKOUT"



            blackout_item["startTime"] = (

                blackout.get("startTime")

                or blackout.get("start_time")

                or blackout.get("start")

            )



            blackout_item["endTime"] = (

                blackout.get("endTime")

                or blackout.get("end_time")

                or blackout.get("end")

            )



            combined.append(blackout_item)



        combined.sort(

            key=lambda item: (

                get_slot_time(

                    item,

                    "startTime",

                    "start_time",

                    "start"

                ) or time.min,

                0 if item.get("itemType") == "BLACKOUT" else 1

            )

        )



        schedule_items[d] = combined



    day_data = (

        schedules.get(day, {})

        if view == "day"

        else {}

    )



    day_slots = (

        day_data.get("slots", [])

        if isinstance(day_data, dict)

        else []

    )



    day_blackouts = (

        day_data.get("blackouts", [])

        if isinstance(day_data, dict)

        else []

    )



    total_slots = len(day_slots)

    blackout_slot_count = 0

    available_count = 0

    booked_count = 0



    for slot in day_slots:

        booked = int(

            slot.get("bookedCount", 0) or 0

        )



        capacity = int(

            slot.get("capacity", 1) or 1

        )



        if slot.get("isBlackout", False):

            blackout_slot_count += 1



        elif booked >= capacity:

            booked_count += 1



        else:

            available_count += 1



    return page(

        "Schedule",

        "schedule",

        view=view,

        day=day,

        dates=dates,

        schedules=schedules,

        schedule_items=schedule_items,

        type_map=type_map,

        consultation_types=consultation_types,

        errors=errors,

        schedule_stats={

            "total": total_slots,

            "available": available_count,

            "booked": booked_count,

            "inactive": blackout_slot_count,

            "blackouts": len(day_blackouts)

        }

    )





@bp.get("/appointments")

@doctor_required

def appointments():

    return page(

        "Appointments",

        "appointments",

        feature_enabled=False

    )