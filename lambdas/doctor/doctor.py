import json
import logging
import functools
import time
import os
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import boto3
import pymysql
from boto3.dynamodb.conditions import Attr

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def log_execution(function):
    """Log entry, successful completion, duration, and exceptions without logging sensitive arguments."""
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        logger.info("STEP START: %s", function.__name__)
        try:
            result = function(*args, **kwargs)
            logger.info(
                "STEP COMPLETE: %s duration_ms=%.2f",
                function.__name__,
                (time.perf_counter() - started) * 1000,
            )
            return result
        except Exception:
            logger.exception(
                "STEP FAILED: %s duration_ms=%.2f",
                function.__name__,
                (time.perf_counter() - started) * 1000,
            )
            raise


secrets_client = boto3.client("secretsmanager")
s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
lambda_client = boto3.client("lambda")
IST = ZoneInfo("Asia/Kolkata")


def response(status, data):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(data, default=str),
    }


@log_execution
def get_db_connection():
    secret_arn = os.environ.get("DB_SECRET_ARN")
    if not secret_arn:
        logger.error("DB_SECRET_ARN environment variable is missing")
        raise RuntimeError("DB_SECRET_ARN is not configured")

    logger.info("DB step 1/3: retrieving database credentials from Secrets Manager")
    secret_response = secrets_client.get_secret_value(SecretId=secret_arn)
    logger.info("DB step 2/3: parsing database secret")
    secret = json.loads(secret_response["SecretString"])

    logger.info(
        "DB step 3/3: connecting to MySQL host=%s database=%s",
        os.environ.get("DB_HOST", "<missing>"),
        os.environ.get("DB_NAME", "medislot"),
    )
    connection = pymysql.connect(
        host=os.environ["DB_HOST"],
        user=secret.get("username") or secret.get("user"),
        password=secret["password"],
        database=os.environ.get("DB_NAME", "medislot"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=5,
        init_command="SET time_zone = '+05:30'",
    )
    logger.info("Database connection established successfully")
    return connection


@log_execution
def get_auth(event):
    logger.info("Auth step: reading API Gateway authorizer context")
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
    logger.info(
        "Auth result: identity_present=%s role=%s",
        bool(identity),
        role or "<missing>",
    )
    return str(identity) if identity else None, role


@log_execution
def get_request_body(event):
    logger.info("Request parsing step: decoding JSON request body")
    try:
        body = json.loads(event.get("body") or "{}")
        logger.info("Request parsing complete: body_type=%s", type(body).__name__)
        return body
    except json.JSONDecodeError:
        raise ValueError("Request body must be valid JSON")


@log_execution
def get_request_path(event):
    logger.info("Routing step: resolving request path")
    request_path = event.get("path") or event.get("rawPath") or ""
    stage = event.get("requestContext", {}).get("stage")

    # REST API proxy events can include the stage in event.path.
    if stage and request_path.startswith(f"/{stage}/"):
        request_path = request_path[len(stage) + 1:]

    return request_path.rstrip("/") or "/"


@log_execution
def doctor_id_from_auth(connection, profile_id):
    """JWT subject is the numeric doctors.doctor_id; no users table is used."""
    try:
        value = int(profile_id)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None




@log_execution
def refresh_generated_slots(doctor_id, start_date=None, days=30):
    """Synchronously regenerate this doctor's slots for the requested IST date window."""
    function_name = os.environ.get("SLOT_MATERIALIZER_FUNCTION_NAME")
    if not function_name:
        logger.error("SLOT_MATERIALIZER_FUNCTION_NAME is not configured")
        return {"status": "not_configured", "message": "Slot materializer function name is missing"}

    payload = {"doctor_id": int(doctor_id), "days": int(days)}
    if start_date:
        payload["start_date"] = str(start_date)[:10]

    logger.info(
        "Materializer step 1/4: invoking function=%s doctor_id=%s start_date=%s days=%s",
        function_name, doctor_id, payload.get("start_date"), days,
    )
    try:
        result = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
        )
        logger.info(
            "Materializer step 2/4: invocation returned status_code=%s function_error=%s",
            result.get("StatusCode"), result.get("FunctionError"),
        )
        raw = result["Payload"].read()
        logger.info("Materializer step 3/4: response payload received bytes=%s", len(raw))
        body = json.loads(raw.decode("utf-8") or "{}")
        logger.info(
            "Materializer step 4/4: parsed response statusCode=%s",
            body.get("statusCode") if isinstance(body, dict) else "<non-object>",
        )
        if result.get("FunctionError"):
            logger.error("Slot materializer invocation failed: %s", body)
            return {"status": "failed", "details": body}
        if int(body.get("statusCode", 500)) >= 400:
            logger.error("Slot materializer returned an error: %s", body)
            return {"status": "failed", "details": body}
        logger.info("Materializer completed successfully for doctor_id=%s", doctor_id)
        return {"status": "completed", "details": body}
    except Exception as exc:
        logger.exception("Could not invoke slot materializer")
        return {"status": "failed", "message": str(exc)}


@log_execution
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
        profile = cursor.fetchone()

    # profile_photo_url stores the private S3 object key, not a browser URL.
    # Create a short-lived GET URL only after confirming that the object exists.
    if profile and profile.get("profile_photo_url"):
        bucket = os.environ.get("PROFILE_PHOTOS_BUCKET")
        object_key = profile["profile_photo_url"]
        if bucket:
            try:
                s3_client.head_object(Bucket=bucket, Key=object_key)
                profile["profile_photo_display_url"] = s3_client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": bucket, "Key": object_key},
                    ExpiresIn=3600,
                )
            except Exception:
                logger.exception("Could not create/read doctor profile photo URL")
                profile["profile_photo_display_url"] = None
        else:
            logger.error("PROFILE_PHOTOS_BUCKET is not configured")
            profile["profile_photo_display_url"] = None
    elif profile:
        profile["profile_photo_display_url"] = None

    if profile:
        _, selected_specialisations = get_doctor_specialisations(connection, doctor_id)
        profile["specialisations"] = selected_specialisations

    return profile


@log_execution
def get_doctor_specialisations(connection, doctor_id):
    """Return the active specialisation catalog and this doctor's selections."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT specialisation_id, specialisation_name, description
            FROM specialisations
            WHERE is_active = TRUE
            ORDER BY specialisation_name
            """
        )
        catalog = cursor.fetchall()
        cursor.execute(
            """
            SELECT s.specialisation_id, s.specialisation_name
            FROM doctor_specialisations ds
            JOIN specialisations s
              ON s.specialisation_id = ds.specialisation_id
            WHERE ds.doctor_id = %s
              AND s.is_active = TRUE
            ORDER BY s.specialisation_name
            """,
            (doctor_id,),
        )
        selected = cursor.fetchall()
    return catalog, selected


@log_execution
def update_doctor_specialisations(connection, doctor_id, specialisation_ids):
    """Replace the authenticated doctor's selected specialisations."""
    if not isinstance(specialisation_ids, list):
        return response(400, {"message": "specialisation_ids must be an array"})

    try:
        ids = sorted({int(value) for value in specialisation_ids})
    except (TypeError, ValueError):
        return response(400, {"message": "Every specialisation ID must be an integer"})

    if any(value <= 0 for value in ids):
        return response(400, {"message": "Specialisation IDs must be positive integers"})

    with connection.cursor() as cursor:
        if ids:
            placeholders = ", ".join(["%s"] * len(ids))
            cursor.execute(
                f"""
                SELECT specialisation_id
                FROM specialisations
                WHERE is_active = TRUE
                  AND specialisation_id IN ({placeholders})
                """,
                ids,
            )
            valid_ids = {int(row["specialisation_id"]) for row in cursor.fetchall()}
            if valid_ids != set(ids):
                connection.rollback()
                return response(400, {"message": "One or more selected specialisations are invalid or inactive"})

        cursor.execute(
            "DELETE FROM doctor_specialisations WHERE doctor_id = %s",
            (doctor_id,),
        )
        if ids:
            cursor.executemany(
                """
                INSERT INTO doctor_specialisations (doctor_id, specialisation_id)
                VALUES (%s, %s)
                """,
                [(doctor_id, specialisation_id) for specialisation_id in ids],
            )

    connection.commit()
    return None



@log_execution
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


@log_execution
def update_profile(connection, doctor_id, data):
    allowed = {
        "first_name",
        "last_name",
        "qualification",
        "experience_years",
        "profile_photo_url",
    }
    specialisation_ids = data.get("specialisation_ids")
    updates = {key: value for key, value in data.items() if key in allowed}

    if not updates and specialisation_ids is None:
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

    # Validate all selected specialisations before changing profile data.
    if specialisation_ids is not None:
        if not isinstance(specialisation_ids, list):
            return response(400, {"message": "specialisation_ids must be an array"})
        try:
            ids = sorted({int(value) for value in specialisation_ids})
        except (TypeError, ValueError):
            return response(400, {"message": "Every specialisation ID must be an integer"})
        if any(value <= 0 for value in ids):
            return response(400, {"message": "Specialisation IDs must be positive integers"})
        if ids:
            placeholders = ", ".join(["%s"] * len(ids))
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT specialisation_id
                    FROM specialisations
                    WHERE is_active = TRUE
                      AND specialisation_id IN ({placeholders})
                    """,
                    ids,
                )
                valid_ids = {int(row["specialisation_id"]) for row in cursor.fetchall()}
            if valid_ids != set(ids):
                return response(400, {"message": "One or more selected specialisations are invalid or inactive"})
    else:
        ids = None

    with connection.cursor() as cursor:
        cursor.execute("SELECT doctor_id FROM doctors WHERE doctor_id = %s", (doctor_id,))
        if not cursor.fetchone():
            connection.rollback()
            return response(404, {"message": "Doctor profile not found"})

        if updates:
            columns = ", ".join(f"{column} = %s" for column in updates)
            cursor.execute(
                f"""
                UPDATE doctors
                SET {columns}, updated_at = CURRENT_TIMESTAMP(3)
                WHERE doctor_id = %s
                """,
                list(updates.values()) + [doctor_id],
            )

        if ids is not None:
            cursor.execute(
                "DELETE FROM doctor_specialisations WHERE doctor_id = %s",
                (doctor_id,),
            )
            if ids:
                cursor.executemany(
                    """
                    INSERT INTO doctor_specialisations (doctor_id, specialisation_id)
                    VALUES (%s, %s)
                    """,
                    [(doctor_id, specialisation_id) for specialisation_id in ids],
                )

    connection.commit()
    return response(200, {"message": "Doctor profile updated"})



@log_execution
def consultation_types(connection, doctor_id, method, data, item_id=None):
    """Manage the shared consultation_types catalog.

    The current RDS table has no doctor_id column, so these records are
    shared across doctors. Doctor identity is still required by the API
    authorizer, but ownership cannot be enforced without a mapping/table.
    """
    with connection.cursor() as cursor:
        if method == "GET":
            if item_id:
                cursor.execute(
                    """
                    SELECT consultation_type_id, type_name, duration_minutes,
                           capacity, description, is_active, consultation_fee,
                           created_at
                    FROM consultation_types
                    WHERE consultation_type_id = %s
                    """,
                    (item_id,),
                )
                row = cursor.fetchone()
                return response(200, row) if row else response(
                    404, {"message": "Consultation type not found"}
                )

            cursor.execute(
                """
                SELECT consultation_type_id, type_name, duration_minutes,
                       capacity, description, is_active, consultation_fee,
                       created_at
                FROM consultation_types
                WHERE is_active = TRUE
                ORDER BY type_name
                """
            )
            return response(200, cursor.fetchall())

        if method == "POST":
            required = ["type_name", "duration_minutes", "consultation_fee"]
            missing = [key for key in required if data.get(key) in (None, "")]
            if missing:
                return response(400, {
                    "message": "Missing required fields", "fields": missing
                })
            try:
                type_name = str(data["type_name"]).strip()
                duration = int(data["duration_minutes"])
                capacity = int(data.get("capacity", 1))
                fee = float(data["consultation_fee"])
            except (TypeError, ValueError):
                return response(400, {
                    "message": "duration_minutes, capacity and consultation_fee must be numeric"
                })
            if not type_name or duration <= 0 or capacity <= 0 or fee < 0:
                return response(400, {
                    "message": "Type name is required; duration/capacity must be positive and fee cannot be negative"
                })
            if type_name.lower() == "health camp session":
                if duration != 60 or not 2 <= capacity <= 10:
                    return response(400, {
                        "message": "Health Camp Session must be 60 minutes with capacity 2 to 10"
                    })
            elif capacity != 1:
                return response(400, {
                    "message": "Only Health Camp Session may have capacity greater than 1"
                })

            # Convert MySQL duplicate-key errors into a clear HTTP 409.
            try:
                cursor.execute(
                    """
                    INSERT INTO consultation_types
                        (type_name, duration_minutes, capacity, description,
                         is_active, consultation_fee)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (type_name, duration, capacity, data.get("description"),
                     bool(data.get("is_active", True)), fee),
                )
            except pymysql.err.IntegrityError as exc:
                connection.rollback()
                if exc.args and exc.args[0] == 1062:
                    return response(409, {
                        "message": "A consultation type with this name already exists."
                    })
                raise

            connection.commit()
            return response(201, {
                "message": "Consultation type created",
                "consultation_type_id": int(cursor.lastrowid),
            })

        if method == "PUT" and item_id:
            logger.info("Availability update step 1/6: validating update fields availability_id=%s", item_id)
            allowed = {
                "type_name", "duration_minutes", "capacity", "description",
                "is_active", "consultation_fee",
            }
            updates = {key: value for key, value in data.items() if key in allowed}
            if not updates:
                return response(400, {"message": "No valid fields"})
            if "type_name" in updates:
                updates["type_name"] = str(updates["type_name"]).strip()
                if not updates["type_name"]:
                    return response(400, {"message": "type_name cannot be empty"})
            try:
                if "duration_minutes" in updates:
                    updates["duration_minutes"] = int(updates["duration_minutes"])
                    if updates["duration_minutes"] <= 0:
                        raise ValueError
                if "capacity" in updates:
                    updates["capacity"] = int(updates["capacity"])
                    if updates["capacity"] <= 0:
                        raise ValueError
                if "consultation_fee" in updates:
                    updates["consultation_fee"] = float(updates["consultation_fee"])
                    if updates["consultation_fee"] < 0:
                        raise ValueError
            except (TypeError, ValueError):
                return response(400, {
                    "message": "duration_minutes and capacity must be positive integers; consultation_fee must be non-negative"
                })

            try:
                cursor.execute(
                    f"UPDATE consultation_types SET {', '.join(f'{key} = %s' for key in updates)} WHERE consultation_type_id = %s",
                    list(updates.values()) + [item_id],
                )
            except pymysql.err.IntegrityError as exc:
                connection.rollback()
                if exc.args and exc.args[0] == 1062:
                    return response(409, {
                        "message": "A consultation type with this name already exists."
                    })
                raise

            connection.commit()
            if cursor.rowcount == 0:
                cursor.execute(
                    "SELECT consultation_type_id FROM consultation_types WHERE consultation_type_id = %s",
                    (item_id,),
                )
                if not cursor.fetchone():
                    return response(404, {"message": "Consultation type not found"})
            return response(200, {"message": "Consultation type updated"})

        if method == "DELETE" and item_id:
            cursor.execute(
                "UPDATE consultation_types SET is_active = FALSE WHERE consultation_type_id = %s",
                (item_id,),
            )
            connection.commit()
            if cursor.rowcount == 0:
                cursor.execute(
                    "SELECT consultation_type_id FROM consultation_types WHERE consultation_type_id = %s",
                    (item_id,),
                )
                if not cursor.fetchone():
                    return response(404, {"message": "Consultation type not found"})
            return response(200, {"message": "Consultation type deactivated"})

    return response(405, {"message": "Method not allowed"})


@log_execution
def availability(connection, doctor_id, method, data, item_id=None):
    logger.info(
        "Availability workflow started: doctor_id=%s method=%s item_id=%s",
        doctor_id, method, item_id,
    )
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
            logger.info("Availability create step 1/7: validating required fields")
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
            logger.info(
                "Availability create step 2/7: validating doctor-hospital assignment doctor_id=%s hospital_id=%s",
                doctor_id, hospital_id,
            )
            cursor.execute(
                "SELECT 1 FROM doctor_hospitals WHERE doctor_id = %s AND hospital_id = %s AND is_active = TRUE LIMIT 1",
                (doctor_id, hospital_id),
            )
            if not cursor.fetchone():
                return response(400, {"message": "Doctor is not assigned to this hospital"})

            logger.info(
                "Availability create step 3/7: validating consultation type doctor_id=%s consultation_type_id=%s",
                doctor_id, consultation_type_id,
            )
            cursor.execute(
                """
                SELECT 1
                FROM consultation_types
                WHERE consultation_type_id = %s
                  AND is_active = TRUE
                  AND (doctor_id = %s OR doctor_id IS NULL)
                LIMIT 1
                """,
                (consultation_type_id, doctor_id),
            )
            if not cursor.fetchone():
                return response(400, {"message": "Consultation type does not belong to this doctor"})
            logger.info("Availability create step 4/7: inserting availability record into RDS")
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
            availability_id = int(cursor.lastrowid)
            logger.info(
                "Availability create step 5/7: inserted availability_id=%s; committing RDS transaction",
                availability_id,
            )
            connection.commit()
            logger.info("Availability create step 6/7: RDS transaction committed")
            effective_start = max(
                datetime.now(IST).date(),
                datetime.strptime(str(data["effective_from"]), "%Y-%m-%d").date(),
            ).isoformat()
            logger.info(
                "Availability create step 7/7: starting synchronous slot refresh doctor_id=%s start_date=%s days=30",
                doctor_id, effective_start,
            )
            generation = refresh_generated_slots(doctor_id, effective_start, 30)
            logger.info("Availability slot refresh result: %s", generation.get("status"))
            return response(201, {
                "message": "Availability created",
                "availability_id": availability_id,
                "slot_generation": generation,
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

            logger.info("Availability update step 2/6: loading doctor-owned availability record")
            cursor.execute(
                "SELECT * FROM doctor_availability WHERE availability_id = %s AND doctor_id = %s",
                (item_id, doctor_id),
            )
            old_row = cursor.fetchone()
            if not old_row:
                return response(404, {"message": "Availability not found"})

            merged = {**old_row, **updates}
            try:
                merged["hospital_id"] = int(merged["hospital_id"])
                merged["consultation_type_id"] = int(merged["consultation_type_id"])
                merged["day_of_week"] = int(merged["day_of_week"])
            except (TypeError, ValueError):
                return response(400, {"message": "Hospital, consultation type and weekday must be integers"})
            if not 0 <= merged["day_of_week"] <= 6:
                return response(400, {"message": "day_of_week must be between 0 (Sunday) and 6 (Saturday)"})
            if str(merged["end_time"]) <= str(merged["start_time"]):
                return response(400, {"message": "end_time must be later than start_time"})
            if merged.get("effective_until") and str(merged["effective_until"]) < str(merged["effective_from"]):
                return response(400, {"message": "effective_until cannot be before effective_from"})

            cursor.execute(
                "SELECT 1 FROM doctor_hospitals WHERE doctor_id = %s AND hospital_id = %s AND is_active = TRUE LIMIT 1",
                (doctor_id, merged["hospital_id"]),
            )
            if not cursor.fetchone():
                return response(400, {"message": "Doctor is not assigned to this hospital"})
            cursor.execute(
                """
                SELECT 1 FROM consultation_types
                WHERE consultation_type_id = %s AND is_active = TRUE
                  AND (doctor_id = %s OR doctor_id IS NULL)
                LIMIT 1
                """,
                (merged["consultation_type_id"], doctor_id),
            )
            if not cursor.fetchone():
                return response(400, {"message": "Consultation type does not belong to this doctor"})

            updates = {key: merged[key] for key in updates}

            logger.info("Availability update step 3/6: updating RDS availability record")
            cursor.execute(
                f"""
                UPDATE doctor_availability
                SET {", ".join(f"{key} = %s" for key in updates)}
                WHERE availability_id = %s AND doctor_id = %s
                """,
                list(updates.values()) + [item_id, doctor_id],
            )
            affected_rows = cursor.rowcount
            if affected_rows == 0:
                cursor.execute(
                    "SELECT availability_id FROM doctor_availability WHERE availability_id = %s AND doctor_id = %s",
                    (item_id, doctor_id),
                )
                if not cursor.fetchone():
                    connection.rollback()
                    return response(404, {"message": "Availability not found"})
            logger.info("Availability update step 4/6: committing RDS transaction")
            connection.commit()
            logger.info("Availability update step 5/6: invoking slot materializer")
            generation = refresh_generated_slots(doctor_id, datetime.now(IST).date().isoformat(), 30)
            logger.info("Availability update step 6/6: materializer status=%s", generation.get("status"))
            return response(200, {"message": "Availability updated", "slot_generation": generation})

        if method == "DELETE" and item_id:
            logger.info("Availability delete step 1/4: deleting availability_id=%s for doctor_id=%s", item_id, doctor_id)
            cursor.execute(
                """
                DELETE FROM doctor_availability
                WHERE availability_id = %s AND doctor_id = %s
                """,
                (item_id, doctor_id),
            )
            deleted_rows = cursor.rowcount
            if deleted_rows == 0:
                connection.rollback()
                return response(404, {"message": "Availability not found"})
            logger.info("Availability delete step 2/4: committing RDS transaction")
            connection.commit()
            logger.info("Availability delete step 3/4: invoking slot materializer")
            generation = refresh_generated_slots(doctor_id, datetime.now(IST).date().isoformat(), 30)
            logger.info("Availability delete step 4/4: materializer status=%s", generation.get("status"))
            return response(200, {"message": "Availability deleted", "slot_generation": generation})

    return response(405, {"message": "Method not allowed"})


@log_execution
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
            blackout_id = int(cursor.lastrowid)
            connection.commit()
            start_day = str(data["start_at_ist"])[:10]
            end_day = str(data["end_at_ist"])[:10]
            try:
                span = (datetime.strptime(end_day, "%Y-%m-%d").date() - datetime.strptime(start_day, "%Y-%m-%d").date()).days + 1
            except ValueError:
                span = 1
            generation = refresh_generated_slots(doctor_id, start_day, max(1, min(span, 90)))
            return response(201, {
                "message": "Blackout created",
                "blackout_id": blackout_id,
                "slot_generation": generation,
            })

        if method == "DELETE" and item_id:
            cursor.execute(
                "SELECT start_at_ist, end_at_ist FROM doctor_blackouts WHERE blackout_id = %s AND doctor_id = %s",
                (item_id, doctor_id),
            )
            blackout_row = cursor.fetchone()
            if not blackout_row:
                connection.rollback()
                return response(404, {"message": "Blackout not found"})
            cursor.execute(
                "DELETE FROM doctor_blackouts WHERE blackout_id = %s AND doctor_id = %s",
                (item_id, doctor_id),
            )
            connection.commit()
            start_day = str(blackout_row["start_at_ist"])[:10]
            end_day = str(blackout_row["end_at_ist"])[:10]
            span = (datetime.strptime(end_day, "%Y-%m-%d").date() - datetime.strptime(start_day, "%Y-%m-%d").date()).days + 1
            generation = refresh_generated_slots(doctor_id, start_day, max(1, min(span, 90)))
            return response(200, {"message": "Blackout deleted", "slot_generation": generation})

    return response(405, {"message": "Method not allowed"})



@log_execution
def create_photo_upload(connection, doctor_id, data):
    """Create a short-lived S3 PUT URL and persist the doctor-owned object key."""
    bucket = os.environ.get("PROFILE_PHOTOS_BUCKET")
    if not bucket:
        return response(500, {"message": "PROFILE_PHOTOS_BUCKET is not configured"})

    filename = str(data.get("fileName") or data.get("file_name") or "").strip()
    content_type = str(data.get("contentType") or data.get("content_type") or "").strip().lower()
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


@log_execution
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
            Attr("doctorId").eq(str(doctor_id))
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

            # Include this doctor's blackout windows overlapping the selected IST date.
            day_start = datetime.combine(target_date, datetime.min.time())
            day_end = day_start + timedelta(days=1)
            cursor.execute(
                """
                SELECT blackout_id, hospital_id, start_at_ist, end_at_ist, reason
                FROM doctor_blackouts
                WHERE doctor_id = %s
                  AND start_at_ist < %s
                  AND end_at_ist > %s
                ORDER BY start_at_ist
                """,
                (int(doctor_id), day_end, day_start),
            )
            blackouts = cursor.fetchall()

        # Mark any previously materialized slots that overlap a blackout.
        for slot in slots:
            slot_start = str(slot.get("slotTime", ""))[:19].replace("T", " ")
            slot_end = str(slot.get("endTime", ""))[:19].replace("T", " ")
            slot["isBlackout"] = any(
                int(slot.get("hospitalId", 0)) == int(blackout.get("hospital_id", 0))
                and slot_start < str(blackout.get("end_at_ist", ""))
                and slot_end > str(blackout.get("start_at_ist", ""))
                for blackout in blackouts
            )
            if slot["isBlackout"]:
                slot["status"] = "BLACKOUT"

        return response(200, {
            "date": target_date.isoformat(),
            "timezone": "Asia/Kolkata",
            "slots": slots,
            "appointments": appointments,
            "blackouts": blackouts,
        })
    except Exception:
        logger.exception("Doctor schedule lookup failed")
        return response(500, {"message": "Could not retrieve doctor schedule"})


@log_execution
def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    logger.info("========== DOCTOR LAMBDA REQUEST START request_id=%s ==========", request_id)
    try:
        logger.info("Handler step 1/8: resolving authenticated identity")
        identity, role = get_auth(event)
        if not identity:
            return response(401, {"message": "Unauthorized"})

        logger.info("Handler step 2/8: parsing HTTP method, path, and route parameters")
        method = (event.get("httpMethod") or "").upper()
        request_path = get_request_path(event)
        parts = [part for part in request_path.split("/") if part]
        logger.info(
            "Request details: request_id=%s method=%s path=%s role=%s",
            request_id, method, request_path, role,
        )
        logger.info("Handler step 3/8: parsing request body when required")
        data = get_request_body(event) if method in {"POST", "PUT", "PATCH"} else {}

        # GET /doctors and GET /doctors/{doctorId} are available to
        # authenticated patients, doctors, and admins.
        if method == "GET" and request_path == "/doctors/profile":
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
            or (len(parts) == 2 and parts[0] == "doctors" and parts[1] not in {"profile", "photo", "schedule", "availability", "blackouts", "consultation-types", "appointments", "slots", "specialisations"})
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

        logger.info("Handler step 4/8: validating doctor role for protected endpoint")
        # The remaining endpoints modify or manage a doctor's own records.
        if role != "doctor":
            return response(403, {"message": "Doctor access required"})

        logger.info("Handler step 5/8: opening database connection and resolving doctor ID")
        with get_db_connection() as connection:
            doctor_id = doctor_id_from_auth(connection, identity)
            if not doctor_id:
                return response(404, {"message": "Doctor profile not found"})

            if request_path == "/doctors/specialisations" and method == "GET":
                catalog, _ = get_doctor_specialisations(connection, doctor_id)
                return response(200, catalog)

            # /doctors/profile refers to the authenticated doctor's own profile.
            if request_path == "/doctors/profile" and method == "GET":
                profile = get_profile(connection, doctor_id)
                return (
                    response(200, profile)
                    if profile
                    else response(404, {"message": "Profile not found"})
                )

            if request_path in {"/doctors/profile", "/doctors"} and method == "PUT":
                return update_profile(connection, doctor_id, data)

            if request_path == "/doctors/photo" and method == "POST":
                return create_photo_upload(connection, doctor_id, data)

            if request_path == "/doctors/schedule" and method == "GET":
                return doctor_schedule(
                    connection, doctor_id, event.get("queryStringParameters") or {}
                )

            logger.info("Handler step 6/8: dispatching doctor resource route")
            if len(parts) >= 2 and parts[0] == "doctors":
                resource_parts = parts[1:]
                # Doctor-owned resources use /doctors/<resource>; doctor identity
                # always comes from the verified authorizer context, never the body.
                resource = resource_parts[0] if resource_parts else ""
                item_id = resource_parts[1] if len(resource_parts) > 1 else None

                if item_id is not None and (not item_id.isdigit() or int(item_id) <= 0):
                    return response(400, {"message": "Resource ID must be a positive integer"})

                if resource == "consultation-types":
                    return consultation_types(connection, doctor_id, method, data, item_id)
                if resource == "availability":
                    logger.info("Handler step 7/8: dispatching availability resource")
                    return availability(connection, doctor_id, method, data, item_id)
                if resource == "blackouts":
                    return blackouts(connection, doctor_id, method, data, item_id)

        logger.warning(
            "Handler step 8/8: no route matched method=%s path=%s",
            method, request_path,
        )
        return response(404, {"message": "Route not found"})

    except ValueError as error:
        logger.warning("Request validation failed request_id=%s error=%s", request_id, error)
        return response(400, {"message": str(error)})
    except Exception:
        logger.exception(
            "Doctor request failed request_id=%s method=%s path=%s",
            request_id,
            (event.get("httpMethod") or "").upper(),
            event.get("path") or event.get("rawPath") or "",
        )
        return response(500, {"message": "Internal server error"})
    finally:
        logger.info("========== DOCTOR LAMBDA REQUEST END request_id=%s ==========", request_id)
