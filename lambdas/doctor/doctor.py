import json
import logging
import os
import uuid

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets_client = boto3.client("secretsmanager")


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
    )


def get_auth(event):
    authorizer = (
        event.get("requestContext", {}).get("authorizer", {}) or {}
    )

    # Supports the normal REST API Lambda-authorizer context.
    if isinstance(authorizer.get("lambda"), dict):
        authorizer = {**authorizer, **authorizer["lambda"]}

    identity = (
        authorizer.get("userId")
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


def doctor_id_from_auth(connection, user_id):
    """
    Authentication issues a userId in the JWT. Resolve that userId
    to the doctor's separate doctor_id using the doctors.user_id FK.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT doctor_id
            FROM doctors
            WHERE user_id = %s
            LIMIT 1
            """,
            (user_id,),
        )
        row = cursor.fetchone()

    return row["doctor_id"] if row else None


def get_profile(connection, doctor_id):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                d.doctor_id,
                d.registration_number,
                d.first_name,
                d.last_name,
                u.email,
                d.qualification,
                d.experience_years,
                d.profile_photo_url,
                d.profile_status,
                d.created_at,
                d.updated_at
            FROM doctors AS d
            INNER JOIN users AS u ON u.user_id = d.user_id
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
                u.email,
                d.qualification,
                d.experience_years,
                d.profile_photo_url,
                d.profile_status
            FROM doctors AS d
            INNER JOIN users AS u ON u.user_id = d.user_id
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

    columns = ", ".join(f"{column} = %s" for column in updates)

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE doctors
            SET {columns}, updated_at = UTC_TIMESTAMP(3)
            WHERE doctor_id = %s
            """,
            list(updates.values()) + [doctor_id],
        )

        if cursor.rowcount == 0:
            connection.rollback()
            return response(404, {"message": "Doctor profile not found"})

    connection.commit()
    return response(200, {"message": "Doctor profile updated"})


def consultation_types(connection, method, data, item_id=None):
    with connection.cursor() as cursor:
        if method == "GET":
            if item_id:
                cursor.execute(
                    """
                    SELECT *
                    FROM consultation_types
                    WHERE consultation_type_id = %s
                    """,
                    (item_id,),
                )
                row = cursor.fetchone()
                return (
                    response(200, row)
                    if row
                    else response(404, {"message": "Consultation type not found"})
                )

            cursor.execute(
                """
                SELECT *
                FROM consultation_types
                WHERE is_active = TRUE
                ORDER BY type_name
                """
            )
            return response(200, cursor.fetchall())

        if method == "POST":
            required = ["type_name", "duration_minutes", "consultation_fee"]
            missing = [
                key for key in required if data.get(key) in (None, "")
            ]
            if missing:
                return response(
                    400,
                    {"message": "Missing required fields", "fields": missing},
                )

            consultation_id = str(uuid.uuid4())
            cursor.execute(
                """
                INSERT INTO consultation_types
                    (consultation_type_id, type_name, duration_minutes,
                     capacity, description, is_active, consultation_fee)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    consultation_id,
                    data["type_name"],
                    data["duration_minutes"],
                    data.get("capacity", 1),
                    data.get("description"),
                    data.get("is_active", True),
                    data["consultation_fee"],
                ),
            )
            connection.commit()
            return response(
                201,
                {
                    "message": "Consultation type created",
                    "consultation_type_id": consultation_id,
                },
            )

        if method == "PUT" and item_id:
            allowed = {
                "type_name",
                "duration_minutes",
                "capacity",
                "description",
                "is_active",
                "consultation_fee",
            }
            updates = {key: value for key, value in data.items() if key in allowed}
            if not updates:
                return response(400, {"message": "No valid fields"})

            cursor.execute(
                f"""
                UPDATE consultation_types
                SET {", ".join(f"{key} = %s" for key in updates)}
                WHERE consultation_type_id = %s
                """,
                list(updates.values()) + [item_id],
            )
            connection.commit()
            return response(200, {"message": "Consultation type updated"})

        if method == "DELETE" and item_id:
            cursor.execute(
                """
                UPDATE consultation_types
                SET is_active = FALSE
                WHERE consultation_type_id = %s
                """,
                (item_id,),
            )
            connection.commit()
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

        if method == "POST":
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

            availability_id = str(uuid.uuid4())
            cursor.execute(
                """
                INSERT INTO doctor_availability
                    (availability_id, doctor_id, hospital_id,
                     consultation_type_id, day_of_week, start_time,
                     end_time, effective_from, effective_until, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    availability_id,
                    doctor_id,
                    data["hospital_id"],
                    data["consultation_type_id"],
                    data["day_of_week"],
                    data["start_time"],
                    data["end_time"],
                    data["effective_from"],
                    data.get("effective_until"),
                    data.get("is_active", True),
                ),
            )
            connection.commit()
            return response(
                201,
                {
                    "message": "Availability created",
                    "availability_id": availability_id,
                },
            )

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
                ORDER BY start_at_utc
                """,
                (doctor_id,),
            )
            return response(200, cursor.fetchall())

        if method == "POST":
            required = ["start_at_utc", "end_at_utc"]
            missing = [
                key for key in required if data.get(key) in (None, "")
            ]
            if missing:
                return response(
                    400,
                    {"message": "Missing required fields", "fields": missing},
                )

            blackout_id = str(uuid.uuid4())
            cursor.execute(
                """
                INSERT INTO doctor_blackouts
                    (blackout_id, doctor_id, hospital_id,
                     start_at_utc, end_at_utc, reason)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    blackout_id,
                    doctor_id,
                    data.get("hospital_id"),
                    data["start_at_utc"],
                    data["end_at_utc"],
                    data.get("reason"),
                ),
            )
            connection.commit()
            return response(
                201,
                {"message": "Blackout created", "blackout_id": blackout_id},
            )

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
        if method == "GET" and (
            request_path == "/doctors"
            or (len(parts) == 2 and parts[0] == "doctors")
        ):
            if role not in {"patient", "doctor", "admin"}:
                return response(403, {"message": "Valid account role required"})

            with get_db_connection() as connection:
                if request_path == "/doctors":
                    return response(200, list_doctors(connection))

                doctor_id = parts[1]
                profile = get_profile(connection, doctor_id)
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

            # GET /doctors/me and PUT /doctors (existing route) refer to
            # the currently authenticated doctor's own profile.
            if request_path == "/doctors/me" and method == "GET":
                profile = get_profile(connection, doctor_id)
                return (
                    response(200, profile)
                    if profile
                    else response(404, {"message": "Profile not found"})
                )

            if request_path == "/doctors" and method == "PUT":
                return update_profile(connection, doctor_id, data)

            if len(parts) >= 2 and parts[0] == "doctors":
                resource = parts[1]
                item_id = parts[2] if len(parts) > 2 else None

                if resource == "consultation-types":
                    return consultation_types(connection, method, data, item_id)

                if resource == "availability":
                    return availability(
                        connection, doctor_id, method, data, item_id
                    )

                if resource == "blackouts":
                    return blackouts(connection, doctor_id, method, data, item_id)

        return response(404, {"message": "Route not found"})

    except ValueError as error:
        return response(400, {"message": str(error)})
    except Exception:
        logger.exception("Doctor request failed")
        return response(500, {"message": "Internal server error"})
