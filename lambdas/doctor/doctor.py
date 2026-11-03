import json
import logging
import os
import uuid

import boto3
import pymysql
from boto3.dynamodb.conditions import Attr

logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets_client = boto3.client("secretsmanager")
s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")


def response(status, data):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(data, default=str),
    }


def get_db_connection():
    secret_response = secrets_client.get_secret_value(
        SecretId=os.environ["DB_SECRET_ARN"]
    )
    secret = json.loads(secret_response["SecretString"])

    return pymysql.connect(
        host=os.environ["DB_HOST"],
        user=secret.get("username") or secret.get("user"),
        password=secret["password"],
        database=os.environ.get("DB_NAME", "medislot"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=5,
        init_command="SET time_zone = '+05:30'",
    )


def get_auth(event):
    authorizer = (
        event.get("requestContext", {}).get("authorizer", {}) or {}
    )

    # Supports the normal REST API Lambda-authorizer context.
    if isinstance(authorizer.get("lambda"), dict):
        authorizer = {**authorizer, **authorizer["lambda"]}

    identity = (
        authorizer.get("profileId")
        or authorizer.get("principalId")
        or authorizer.get("sub")
    )
    role = str(authorizer.get("role") or "").strip().lower()

    return str(identity) if identity else None, role


def get_request_body(event):
    try:
        return json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        raise ValueError("Request body must be valid JSON")


def get_request_path(event):
    request_path = event.get("path") or event.get("rawPath") or ""
    stage = event.get("requestContext", {}).get("stage")

    # REST API proxy events can include the stage in event.path.
    if stage and request_path.startswith(f"/{stage}/"):
        request_path = request_path[len(stage) + 1:]

    return request_path.rstrip("/") or "/"


def doctor_id_from_auth(connection, profile_id):
    """JWT subject is the numeric doctors.doctor_id; no users table is used."""
    try:
        value = int(profile_id)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def get_profile(connection, doctor_id):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                d.doctor_id,
                d.registration_number,
                d.first_name,
                d.last_name,
                d.qualification,
                d.experience_years,
                d.profile_photo_url,
                d.profile_status,
                d.created_at,
                d.updated_at
            FROM doctors AS d
            WHERE d.doctor_id = %s
            """,
            (doctor_id,),
        )
        return cursor.fetchone()


def list_doctors(connection):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                d.doctor_id,
                d.registration_number,
                d.first_name,
                d.last_name,
                d.qualification,
                d.experience_years,
                d.profile_photo_url,
                d.profile_status
            FROM doctors AS d
            WHERE UPPER(d.profile_status) = 'ACTIVE'
            ORDER BY d.first_name, d.last_name
            """
        )
        return cursor.fetchall()


def update_profile(connection, doctor_id, data):
    allowed = {
        "first_name",
        "last_name",
        "qualification",
        "experience_years",
        "profile_photo_url",
    }
    updates = {key: value for key, value in data.items() if key in allowed}

    if not updates:
        return response(400, {"message": "No valid profile fields provided"})
    for field in ("first_name", "last_name", "qualification", "profile_photo_url"):
        if field in updates and updates[field] is not None and not isinstance(updates[field], str):
            return response(400, {"message": f"{field} must be a string"})
    if "first_name" in updates and not updates["first_name"].strip():
        return response(400, {"message": "first_name cannot be empty"})
    if "experience_years" in updates:
        try:
            updates["experience_years"] = int(updates["experience_years"])
            if updates["experience_years"] < 0 or updates["experience_years"] > 100:
                raise ValueError
        except (TypeError, ValueError):
            return response(400, {"message": "experience_years must be between 0 and 100"})

    columns = ", ".join(f"{column} = %s" for column in updates)

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE doctors
            SET {columns}, updated_at = CURRENT_TIMESTAMP(3)
            WHERE doctor_id = %s
            """,
            list(updates.values()) + [doctor_id],
        )

        if cursor.rowcount == 0:
            cursor.execute(
                "SELECT doctor_id FROM doctors WHERE doctor_id = %s",
                (doctor_id,),
            )
            if not cursor.fetchone():
                connection.rollback()
                return response(404, {"message": "Doctor profile not found"})

    connection.commit()
    return response(200, {"message": "Doctor profile updated"})


def consultation_types(connection, doctor_id, method, data, item_id=None):
    with connection.cursor() as cursor:
        if method == "GET":
            if item_id:
                cursor.execute(
                    """
                    SELECT *
                    FROM consultation_types
                    WHERE consultation_type_id = %s
                      AND (doctor_id = %s OR doctor_id IS NULL)
                    """,
                    (item_id, doctor_id),
                )
                row = cursor.fetchone()
                return response(200, row) if row else response(404, {"message": "Consultation type not found"})
            cursor.execute(
                """
                SELECT *
                FROM consultation_types
                WHERE is_active = TRUE
                  AND (doctor_id = %s OR doctor_id IS NULL)
                ORDER BY type_name
                """,
                (doctor_id,),
            )
            return response(200, cursor.fetchall())

        if method == "POST":
            required = ["type_name", "duration_minutes", "consultation_fee"]
            missing = [key for key in required if data.get(key) in (None, "")]
            if missing:
                return response(400, {"message": "Missing required fields", "fields": missing})
            try:
                type_name = str(data["type_name"]).strip()
                duration = int(data["duration_minutes"])
                capacity = int(data.get("capacity", 1))
                fee = float(data["consultation_fee"])
            except (TypeError, ValueError):
                return response(400, {"message": "duration_minutes, capacity and consultation_fee must be numeric"})
            if not type_name or duration <= 0 or capacity <= 0 or fee < 0:
                return response(400, {"message": "Type name is required; duration/capacity must be positive and fee cannot be negative"})
            if type_name.lower() == "health camp session":
                if duration != 60 or not 2 <= capacity <= 10:
                    return response(400, {"message": "Health Camp Session must be 60 minutes with capacity 2 to 10"})
            elif capacity != 1:
                return response(400, {"message": "Only Health Camp Session may have capacity greater than 1"})
            cursor.execute(
                """
                INSERT INTO consultation_types
                    (doctor_id, type_name, duration_minutes, capacity, description, is_active, consultation_fee)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (doctor_id, type_name, duration, capacity, data.get("description"),
                 bool(data.get("is_active", True)), fee),
            )
            connection.commit()
            return response(201, {"message": "Consultation type created", "consultation_type_id": int(cursor.lastrowid)})

        if method == "PUT" and item_id:
            allowed = {"type_name", "duration_minutes", "capacity", "description", "is_active", "consultation_fee"}
            updates = {key: value for key, value in data.items() if key in allowed}
            if not updates:
                return response(400, {"message": "No valid fields"})
            cursor.execute(
                f"UPDATE consultation_types SET {', '.join(f'{key} = %s' for key in updates)} WHERE consultation_type_id = %s AND doctor_id = %s",
                list(updates.values()) + [item_id, doctor_id],
            )
            connection.commit()
            if cursor.rowcount == 0:
                return response(404, {"message": "Consultation type not found"})
            return response(200, {"message": "Consultation type updated"})

        if method == "DELETE" and item_id:
            cursor.execute(
                "UPDATE consultation_types SET is_active = FALSE WHERE consultation_type_id = %s AND doctor_id = %s",
                (item_id, doctor_id),
            )
            connection.commit()
            if cursor.rowcount == 0:
                return response(404, {"message": "Consultation type not found"})
            return response(200, {"message": "Consultation type deactivated"})

    return response(405, {"message": "Method not allowed"})


def availability(connection, doctor_id, method, data, item_id=None):
    with connection.cursor() as cursor:
        if method == "GET":
            cursor.execute(
                """
                SELECT *
                FROM doctor_availability
                WHERE doctor_id = %s
                ORDER BY day_of_week, start_time
                """,
                (doctor_id,),
            )
            return response(200, cursor.fetchall())

        if method in {"POST", "PUT"} and item_id is None:
            required = [
                "hospital_id",
                "consultation_type_id",
                "day_of_week",
                "start_time",
                "end_time",
                "effective_from",
            ]
            missing = [
                key for key in required if data.get(key) in (None, "")
            ]
            if missing:
                return response(
                    400,
                    {"message": "Missing required fields", "fields": missing},
                )

            try:
                hospital_id = int(data["hospital_id"])
                consultation_type_id = int(data["consultation_type_id"])
                day = int(data["day_of_week"])
            except (TypeError, ValueError):
                return response(400, {"message": "hospital_id, consultation_type_id and day_of_week must be integers"})
            if day < 0 or day > 6:
                return response(400, {"message": "day_of_week must be between 0 (Sunday) and 6 (Saturday)"})
            if data["end_time"] <= data["start_time"]:
                return response(400, {"message": "end_time must be later than start_time"})
            if data.get("effective_until") and data["effective_until"] < data["effective_from"]:
                return response(400, {"message": "effective_until cannot be before effective_from"})
            cursor.execute(
                "SELECT 1 FROM doctor_hospitals WHERE doctor_id = %s AND hospital_id = %s AND is_active = TRUE LIMIT 1",
                (doctor_id, hospital_id),
            )
            if not cursor.fetchone():
                return response(400, {"message": "Doctor is not assigned to this hospital"})

            cursor.execute(
                """
                SELECT 1
                FROM consultation_types
                WHERE consultation_type_id = %s
                  AND (doctor_id = %s OR doctor_id IS NULL)
                  AND is_active = TRUE
                LIMIT 1
                """,
                (consultation_type_id, doctor_id),
            )
            if not cursor.fetchone():
                return response(400, {"message": "Consultation type does not belong to this doctor"})
            cursor.execute(
                """
                INSERT INTO doctor_availability
                    (doctor_id, hospital_id, consultation_type_id, day_of_week,
                     start_time, end_time, effective_from, effective_until, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (doctor_id, hospital_id, consultation_type_id, day,
                 data["start_time"], data["end_time"], data["effective_from"],
                 data.get("effective_until"), bool(data.get("is_active", True))),
            )
            connection.commit()
            return response(201, {
                "message": "Availability created",
                "availability_id": int(cursor.lastrowid),
            })

        if method == "PUT" and item_id:
            allowed = {
                "hospital_id",
                "consultation_type_id",
                "day_of_week",
                "start_time",
                "end_time",
                "effective_from",
                "effective_until",
                "is_active",
            }
            updates = {key: value for key, value in data.items() if key in allowed}
            if not updates:
                return response(400, {"message": "No valid fields"})

            cursor.execute(
                f"""
                UPDATE doctor_availability
                SET {", ".join(f"{key} = %s" for key in updates)}
                WHERE availability_id = %s AND doctor_id = %s
                """,
                list(updates.values()) + [item_id, doctor_id],
            )
            connection.commit()
            return response(200, {"message": "Availability updated"})

        if method == "DELETE" and item_id:
            cursor.execute(
                """
                DELETE FROM doctor_availability
                WHERE availability_id = %s AND doctor_id = %s
                """,
                (item_id, doctor_id),
            )
            connection.commit()
            return response(200, {"message": "Availability deleted"})

    return response(405, {"message": "Method not allowed"})


def blackouts(connection, doctor_id, method, data, item_id=None):
    with connection.cursor() as cursor:
        if method == "GET":
            cursor.execute(
                """
                SELECT *
                FROM doctor_blackouts
                WHERE doctor_id = %s
                ORDER BY start_at_ist
                """,
                (doctor_id,),
            )
            return response(200, cursor.fetchall())

        if method == "POST":
            required = ["start_at_ist", "end_at_ist"]
            missing = [
                key for key in required if data.get(key) in (None, "")
            ]
            if missing:
                return response(
                    400,
                    {"message": "Missing required fields", "fields": missing},
                )

            if not data.get("hospital_id"):
                return response(400, {"message": "hospital_id is required"})
            if data["end_at_ist"] <= data["start_at_ist"]:
                return response(400, {"message": "end_at_ist must be later than start_at_ist"})
            try:
                hospital_id = int(data["hospital_id"])
            except (TypeError, ValueError):
                return response(400, {"message": "hospital_id must be an integer"})
            cursor.execute(
                "SELECT 1 FROM doctor_hospitals WHERE doctor_id = %s AND hospital_id = %s AND is_active = TRUE LIMIT 1",
                (doctor_id, hospital_id),
            )
            if not cursor.fetchone():
                return response(400, {"message": "Doctor is not assigned to this hospital"})
            cursor.execute(
                """
                INSERT INTO doctor_blackouts
                    (doctor_id, hospital_id, start_at_ist, end_at_ist, reason)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (doctor_id, hospital_id, data["start_at_ist"],
                 data["end_at_ist"], data.get("reason")),
            )
            connection.commit()
            return response(201, {
                "message": "Blackout created",
                "blackout_id": int(cursor.lastrowid),
            })

        if method == "DELETE" and item_id:
            cursor.execute(
                """
                DELETE FROM doctor_blackouts
                WHERE blackout_id = %s AND doctor_id = %s
                """,
                (item_id, doctor_id),
            )
            connection.commit()
            return response(200, {"message": "Blackout deleted"})

    return response(405, {"message": "Method not allowed"})



def create_photo_upload(connection, doctor_id, data):
    """Create a short-lived S3 PUT URL and persist the doctor-owned object key."""
    bucket = os.environ.get("PROFILE_PHOTOS_BUCKET")
    if not bucket:
        return response(500, {"message": "PROFILE_PHOTOS_BUCKET is not configured"})

    filename = str(data.get("fileName") or "").strip()
    content_type = str(data.get("contentType") or "").strip().lower()
    allowed_types = {"image/jpeg", "image/png", "image/webp"}
    if not filename or content_type not in allowed_types:
        return response(400, {
            "message": "fileName is required and contentType must be image/jpeg, image/png, or image/webp"
        })

    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }[content_type]
    safe_stem = os.path.splitext(os.path.basename(filename))[0]
    safe_stem = "".join(ch for ch in safe_stem if ch.isalnum() or ch in "-_")[:60] or "profile"
    object_key = f"doctors/{doctor_id}/profile/{uuid.uuid4().hex}{extension}"

    try:
        upload_url = s3_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket,
                "Key": object_key,
                "ContentType": content_type,
            },
            ExpiresIn=300,
        )
    except Exception:
        logger.exception("Could not generate doctor photo upload URL")
        return response(500, {"message": "Could not generate photo upload URL"})

    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE doctors
            SET profile_photo_url = %s, updated_at = CURRENT_TIMESTAMP(3)
            WHERE doctor_id = %s
            """,
            (object_key, doctor_id),
        )
        if cursor.rowcount == 0:
            connection.rollback()
            return response(404, {"message": "Doctor profile not found"})
    connection.commit()

    return response(200, {
        "message": "Pre-signed upload URL generated",
        "uploadUrl": upload_url,
        "objectKey": object_key,
        "contentType": content_type,
        "expiresIn": 300,
    })


def doctor_schedule(connection, doctor_id, query_params):
    """Return this doctor's generated slots for one IST calendar date."""
    date_value = (query_params or {}).get("date")
    if not date_value:
        return response(400, {"message": "date query parameter is required (YYYY-MM-DD)"})
    try:
        target_date = datetime.strptime(date_value, "%Y-%m-%d").date()
    except ValueError:
        return response(400, {"message": "date must use YYYY-MM-DD format"})

    table_name = os.environ.get("SLOTS_TABLE_NAME")
    if not table_name:
        return response(500, {"message": "SLOTS_TABLE_NAME is not configured"})

    try:
        table = dynamodb.Table(table_name)
        filter_expression = (
            Attr("doctorId").eq(int(doctor_id))
            & Attr("slotTime").begins_with(target_date.isoformat())
        )
        result = table.scan(FilterExpression=filter_expression)
        slots = result.get("Items", [])
        while result.get("LastEvaluatedKey"):
            result = table.scan(
                FilterExpression=filter_expression,
                ExclusiveStartKey=result["LastEvaluatedKey"],
            )
            slots.extend(result.get("Items", []))
        slots.sort(key=lambda item: item.get("slotTime", ""))

        # RDS is the system of record for booked appointments and patient details.
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    a.appointment_id, a.slot_id, a.patient_id,
                    p.first_name AS patient_first_name,
                    p.last_name AS patient_last_name,
                    p.phone AS patient_phone,
                    a.hospital_id, a.consultation_type_id,
                    a.start_at_ist, a.end_at_ist, a.status
                FROM appointments a
                JOIN patients p ON p.patient_id = a.patient_id
                WHERE a.doctor_id = %s
                  AND DATE(a.start_at_ist) = %s
                  AND a.status = 'BOOKED'
                ORDER BY a.start_at_ist
                """,
                (int(doctor_id), target_date),
            )
            appointments = cursor.fetchall()

        return response(200, {
            "date": target_date.isoformat(),
            "timezone": "Asia/Kolkata",
            "slots": slots,
            "appointments": appointments,
        })
    except Exception:
        logger.exception("Doctor schedule lookup failed")
        return response(500, {"message": "Could not retrieve doctor schedule"})


def lambda_handler(event, context):
    try:
        identity, role = get_auth(event)
        if not identity:
            return response(401, {"message": "Unauthorized"})

        method = (event.get("httpMethod") or "").upper()
        request_path = get_request_path(event)
        parts = [part for part in request_path.split("/") if part]
        data = get_request_body(event) if method in {"POST", "PUT", "PATCH"} else {}

        # GET /doctors and GET /doctors/{doctorId} are available to
        # authenticated patients, doctors, and admins.
        if method == "GET" and request_path == "/doctors/me":
            if role != "doctor":
                return response(403, {"message": "Doctor access required"})
            with get_db_connection() as connection:
                doctor_id = doctor_id_from_auth(connection, identity)
                if not doctor_id:
                    return response(404, {"message": "Doctor profile not found"})
                profile = get_profile(connection, doctor_id)
                return response(200, profile) if profile else response(404, {"message": "Profile not found"})

        if method == "GET" and (
            request_path == "/doctors"
            or (len(parts) == 2 and parts[0] == "doctors" and parts[1] != "me")
        ):
            if role not in {"patient", "doctor", "admin"}:
                return response(403, {"message": "Valid account role required"})

            with get_db_connection() as connection:
                if request_path == "/doctors":
                    return response(200, list_doctors(connection))

                doctor_id = parts[1]
                if not doctor_id.isdigit() or int(doctor_id) <= 0:
                    return response(400, {"message": "doctor_id must be a positive integer"})
                profile = get_profile(connection, int(doctor_id))
                if not profile or str(profile.get("profile_status", "")).upper() != "ACTIVE":
                    return response(404, {"message": "Active doctor not found"})

                return response(200, profile)

        # The remaining endpoints modify or manage a doctor's own records.
        if role != "doctor":
            return response(403, {"message": "Doctor access required"})

        with get_db_connection() as connection:
            doctor_id = doctor_id_from_auth(connection, identity)
            if not doctor_id:
                return response(404, {"message": "Doctor profile not found"})

            # GET/PUT /doctors/me refer to
            # the currently authenticated doctor's own profile.
            if request_path == "/doctors/me" and method == "GET":
                profile = get_profile(connection, doctor_id)
                return (
                    response(200, profile)
                    if profile
                    else response(404, {"message": "Profile not found"})
                )

            if request_path in {"/doctors/me", "/doctors"} and method == "PUT":
                return update_profile(connection, doctor_id, data)

            if request_path == "/doctors/me/photo" and method == "POST":
                return create_photo_upload(connection, doctor_id, data)

            if request_path == "/doctors/me/schedule" and method == "GET":
                return doctor_schedule(
                    connection, doctor_id, event.get("queryStringParameters") or {}
                )

            if len(parts) >= 2 and parts[0] == "doctors":
                resource_parts = parts[1:]
                # Support /doctors/me/<resource> plus legacy /doctors/<resource>.
                if resource_parts and resource_parts[0] == "me":
                    resource_parts = resource_parts[1:]
                resource = resource_parts[0] if resource_parts else ""
                item_id = resource_parts[1] if len(resource_parts) > 1 else None

                if item_id is not None and (not item_id.isdigit() or int(item_id) <= 0):
                    return response(400, {"message": "Resource ID must be a positive integer"})

                if resource == "consultation-types":
                    return consultation_types(connection, doctor_id, method, data, item_id)
                if resource == "availability":
                    return availability(connection, doctor_id, method, data, item_id)
                if resource == "blackouts":
                    return blackouts(connection, doctor_id, method, data, item_id)

        return response(404, {"message": "Route not found"})

    except ValueError as error:
        return response(400, {"message": str(error)})
    except Exception:
        logger.exception("Doctor request failed")
        return response(500, {"message": "Internal server error"})
