
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
from boto3.dynamodb.conditions import Attr, Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def log_execution(function):
    """Log entry, successful completion, duration, and exceptions."""
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

    return wrapper


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

    logger.info(
        "DB step 1/3: retrieving credentials from Secrets Manager"
    )

    secret_response = secrets_client.get_secret_value(
        SecretId=secret_arn
    )

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

    if isinstance(authorizer.get("lambda"), dict):
        authorizer = {
            **authorizer,
            **authorizer["lambda"]
        }

    identity = (
        authorizer.get("profileId")
        or authorizer.get("principalId")
        or authorizer.get("sub")
    )

    role = str(
        authorizer.get("role") or ""
    ).strip().lower()

    logger.info(
        "Auth result: identity_present=%s role=%s",
        bool(identity),
        role or "<missing>",
    )

    return str(identity) if identity else None, role


@log_execution
def get_request_body(event):

    logger.info("Request parsing step: decoding JSON body")

    try:
        body = json.loads(event.get("body") or "{}")

        logger.info(
            "Request parsing complete: body_type=%s",
            type(body).__name__,
        )

        return body

    except json.JSONDecodeError:
        raise ValueError("Request body must be valid JSON")


@log_execution
def get_request_path(event):

    logger.info("Routing step: resolving request path")

    request_path = (
        event.get("path")
        or event.get("rawPath")
        or ""
    )

    stage = event.get("requestContext", {}).get("stage")

    if stage and request_path.startswith(f"/{stage}/"):
        request_path = request_path[len(stage) + 1:]

    return request_path.rstrip("/") or "/"


@log_execution
def doctor_id_from_auth(connection, profile_id):
    """JWT subject is the numeric doctors.doctor_id."""

    try:
        value = int(profile_id)
        return value if value > 0 else None

    except (TypeError, ValueError):
        return None


@log_execution
def refresh_generated_slots(doctor_id, start_date=None, days=30):
    """Regenerate slots for this doctor."""

    function_name = os.environ.get(
        "SLOT_MATERIALIZER_FUNCTION_NAME"
    )

    if not function_name:
        logger.error(
            "SLOT_MATERIALIZER_FUNCTION_NAME is not configured"
        )

        return {
            "status": "not_configured",
            "message": "Slot materializer function name is missing",
        }

    payload = {
        "doctor_id": int(doctor_id),
        "days": int(days),
    }

    if start_date:
        payload["start_date"] = str(start_date)[:10]

    logger.info(
        "Materializer step 1/4: invoking function=%s doctor_id=%s",
        function_name,
        doctor_id,
    )

    try:
        result = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
        )

        logger.info(
            "Materializer step 2/4: status_code=%s function_error=%s",
            result.get("StatusCode"),
            result.get("FunctionError"),
        )

        raw = result["Payload"].read()

        logger.info(
            "Materializer step 3/4: response bytes=%s",
            len(raw),
        )

        body = json.loads(
            raw.decode("utf-8") or "{}"
        )

        logger.info(
            "Materializer step 4/4: response statusCode=%s",
            body.get("statusCode")
            if isinstance(body, dict)
            else "<non-object>",
        )

        if result.get("FunctionError"):
            logger.error("Slot materializer invocation failed")
            return {
                "status": "failed",
                "details": body,
            }

        if int(body.get("statusCode", 500)) >= 400:
            logger.error("Slot materializer returned an error")
            return {
                "status": "failed",
                "details": body,
            }

        logger.info(
            "Materializer completed successfully for doctor_id=%s",
            doctor_id,
        )

        return {
            "status": "completed",
            "details": body,
        }

    except Exception as exc:
        logger.exception("Could not invoke slot materializer")

        return {
            "status": "failed",
            "message": str(exc),
        }


@log_execution
def refresh_doctors_for_consultation_type(
    connection,
    consultation_type_id,
    start_date=None,
    days=30,
):
    """Refresh slots for doctors linked to this consultation type."""

    with connection.cursor() as cursor:

        cursor.execute(
            """
            SELECT DISTINCT doctor_id
            FROM doctor_availability
            WHERE consultation_type_id = %s
            """,
            (int(consultation_type_id),),
        )

        doctor_ids = [
            int(row["doctor_id"])
            for row in cursor.fetchall()
        ]

    results = []

    for linked_doctor_id in doctor_ids:

        results.append({
            "doctor_id": linked_doctor_id,
            "slot_generation": refresh_generated_slots(
                linked_doctor_id,
                start_date,
                days,
            ),
        })

    return results


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

    if profile and profile.get("profile_photo_url"):

        bucket = os.environ.get("PROFILE_PHOTOS_BUCKET")
        object_key = profile["profile_photo_url"]

        if bucket:

            try:
                s3_client.head_object(
                    Bucket=bucket,
                    Key=object_key,
                )

                profile["profile_photo_display_url"] = (
                    s3_client.generate_presigned_url(
                        "get_object",
                        Params={
                            "Bucket": bucket,
                            "Key": object_key,
                        },
                        ExpiresIn=3600,
                    )
                )

            except Exception:
                logger.exception(
                    "Could not create/read doctor profile photo URL"
                )

                profile["profile_photo_display_url"] = None

        else:
            logger.error(
                "PROFILE_PHOTOS_BUCKET is not configured"
            )

            profile["profile_photo_display_url"] = None

    elif profile:
        profile["profile_photo_display_url"] = None

    if profile:
        _, selected_specialisations = get_doctor_specialisations(
            connection,
            doctor_id,
        )

        profile["specialisations"] = selected_specialisations

    return profile


@log_execution
def get_doctor_specialisations(connection, doctor_id):
    """Return active specialisations and doctor's selections."""

    with connection.cursor() as cursor:

        cursor.execute(
            """
            SELECT
                specialisation_id,
                specialisation_name,
                description
            FROM specialisations
            WHERE is_active = TRUE
            ORDER BY specialisation_name
            """
        )

        catalog = cursor.fetchall()

        cursor.execute(
            """
            SELECT
                s.specialisation_id,
                s.specialisation_name
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
def update_doctor_specialisations(
    connection,
    doctor_id,
    specialisation_ids,
):

    if not isinstance(specialisation_ids, list):
        return response(
            400,
            {"message": "specialisation_ids must be an array"},
        )

    try:
        ids = sorted({
            int(value)
            for value in specialisation_ids
        })

    except (TypeError, ValueError):
        return response(
            400,
            {"message": "Every specialisation ID must be an integer"},
        )

    if any(value <= 0 for value in ids):
        return response(
            400,
            {"message": "Specialisation IDs must be positive integers"},
        )

    with connection.cursor() as cursor:

        if ids:

            placeholders = ", ".join(
                ["%s"] * len(ids)
            )

            cursor.execute(
                f"""
                SELECT specialisation_id
                FROM specialisations
                WHERE is_active = TRUE
                  AND specialisation_id IN ({placeholders})
                """,
                ids,
            )

            valid_ids = {
                int(row["specialisation_id"])
                for row in cursor.fetchall()
            }

            if valid_ids != set(ids):
                connection.rollback()

                return response(
                    400,
                    {
                        "message": (
                            "One or more selected specialisations "
                            "are invalid or inactive"
                        )
                    },
                )

        cursor.execute(
            """
            DELETE FROM doctor_specialisations
            WHERE doctor_id = %s
            """,
            (doctor_id,),
        )

        if ids:
            cursor.executemany(
                """
                INSERT INTO doctor_specialisations
                    (doctor_id, specialisation_id)
                VALUES (%s, %s)
                """,
                [
                    (doctor_id, specialisation_id)
                    for specialisation_id in ids
                ],
            )

    connection.commit()

    return None


@log_execution
def list_doctors(connection, query_params=None):
    """Search active doctors with filters and pagination."""

    query_params = query_params or {}

    name = str(query_params.get("name") or "").strip()
    hospital = str(query_params.get("hospital") or "").strip()
    specialisation = str(
        query_params.get("specialisation") or ""
    ).strip()
    city = str(query_params.get("city") or "").strip()

    try:
        page = max(1, int(query_params.get("page", 1)))

        page_size = min(
            100,
            max(1, int(query_params.get("page_size", 20))),
        )

    except (TypeError, ValueError):
        raise ValueError(
            "page and page_size must be positive integers"
        )

    offset = (page - 1) * page_size

    where = [
        "d.profile_status = 'ACTIVE'"
    ]

    params = []

    if name:

        name_parts = name.split(maxsplit=1)

        if len(name_parts) == 2:

            where.append(
                "(d.first_name LIKE %s AND d.last_name LIKE %s)"
            )

            params.extend([
                name_parts[0] + "%",
                name_parts[1] + "%",
            ])

        else:

            where.append(
                "(d.first_name LIKE %s OR d.last_name LIKE %s)"
            )

            params.extend([
                name + "%",
                name + "%",
            ])

    if hospital:
        where.append("h.hospital_name LIKE %s")
        params.append(hospital + "%")

    if specialisation:
        where.append("s.specialisation_name LIKE %s")
        params.append(specialisation + "%")

    if city:
        where.append("h.city LIKE %s")
        params.append(city + "%")

    joins = """
        FROM doctors d

        LEFT JOIN doctor_hospitals dh
            ON dh.doctor_id = d.doctor_id
            AND dh.is_active = TRUE

        LEFT JOIN hospitals h
            ON h.hospital_id = dh.hospital_id
            AND h.is_active = TRUE

        LEFT JOIN doctor_specialisations ds
            ON ds.doctor_id = d.doctor_id

        LEFT JOIN specialisations s
            ON s.specialisation_id = ds.specialisation_id
    """

    where_sql = " AND ".join(where)

    with connection.cursor() as cursor:

        cursor.execute(
            f"""
            SELECT COUNT(DISTINCT d.doctor_id) AS total
            {joins}
            WHERE {where_sql}
            """,
            tuple(params),
        )

        total = int(
            (cursor.fetchone() or {}).get("total", 0)
        )

        cursor.execute(
            f"""
            SELECT DISTINCT
                d.doctor_id,
                d.first_name,
                d.last_name,
                d.qualification,
                d.experience_years,
                d.profile_photo_url,
                d.profile_status,
                (
                    SELECT GROUP_CONCAT(DISTINCT s2.specialisation_name
                                        ORDER BY s2.specialisation_name SEPARATOR ', ')
                    FROM doctor_specialisations ds2
                    JOIN specialisations s2
                      ON s2.specialisation_id = ds2.specialisation_id
                     AND s2.is_active = TRUE
                    WHERE ds2.doctor_id = d.doctor_id
                ) AS specialisations,
                (
                    SELECT GROUP_CONCAT(DISTINCT h2.hospital_name
                                        ORDER BY h2.hospital_name SEPARATOR ', ')
                    FROM doctor_hospitals dh2
                    JOIN hospitals h2
                      ON h2.hospital_id = dh2.hospital_id
                     AND h2.is_active = TRUE
                    WHERE dh2.doctor_id = d.doctor_id
                      AND dh2.is_active = TRUE
                ) AS hospitals,
                (
                    SELECT GROUP_CONCAT(DISTINCT h3.city
                                        ORDER BY h3.city SEPARATOR ', ')
                    FROM doctor_hospitals dh3
                    JOIN hospitals h3
                      ON h3.hospital_id = dh3.hospital_id
                     AND h3.is_active = TRUE
                    WHERE dh3.doctor_id = d.doctor_id
                      AND dh3.is_active = TRUE
                ) AS city
            {joins}
            WHERE {where_sql}
            ORDER BY
                d.first_name,
                d.last_name,
                d.doctor_id
            LIMIT %s OFFSET %s
            """,
            tuple(params + [page_size, offset]),
        )

        doctors = cursor.fetchall()

    # Search results need a browser-readable, short-lived URL rather than the
    # private S3 object key stored in doctors.profile_photo_url.
    photo_bucket = os.environ.get("PROFILE_PHOTOS_BUCKET")
    for doctor in doctors:
        doctor["profile_photo_display_url"] = None
        photo_key = doctor.get("profile_photo_url")
        if photo_bucket and photo_key:
            try:
                doctor["profile_photo_display_url"] = s3_client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": photo_bucket, "Key": photo_key},
                    ExpiresIn=3600,
                )
            except Exception:
                logger.exception(
                    "Could not create public doctor photo URL doctor_id=%s",
                    doctor.get("doctor_id"),
                )

    return {
        "doctors": doctors,

        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (
                total + page_size - 1
            ) // page_size,
            "has_next": offset + len(doctors) < total,
            "has_previous": page > 1,
        },

        "filters": {
            "name": name or None,
            "hospital": hospital or None,
            "specialisation": specialisation or None,
            "city": city or None,
        },
    }


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
    refreshed_profile = get_profile(connection, doctor_id)
    return response(200, {
        "message": "Doctor profile updated",
        "profile": refreshed_profile,
    })



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
            consultation_type_id = int(cursor.lastrowid)
            affected_doctors = refresh_doctors_for_consultation_type(
                connection,
                consultation_type_id,
                datetime.now(IST).date().isoformat(),
                30,
            )
            return response(201, {
                "message": "Consultation type created",
                "consultation_type_id": consultation_type_id,
                "slot_generation": affected_doctors,
            })

        if method == "PUT" and item_id:
            cursor.execute(
                "SELECT DISTINCT doctor_id FROM doctor_availability WHERE consultation_type_id = %s",
                (item_id,),
            )
            affected_doctor_ids = [int(row["doctor_id"]) for row in cursor.fetchall()]
            logger.info("Consultation type update affects doctor_ids=%s", affected_doctor_ids)
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
            generation_results = [
                {
                    "doctor_id": linked_doctor_id,
                    "slot_generation": refresh_generated_slots(
                        linked_doctor_id,
                        datetime.now(IST).date().isoformat(),
                        30,
                    ),
                }
                for linked_doctor_id in affected_doctor_ids
            ]
            return response(200, {
                "message": "Consultation type updated",
                "slot_generation": generation_results,
            })

        if method == "DELETE" and item_id:
            cursor.execute(
                "SELECT DISTINCT doctor_id FROM doctor_availability WHERE consultation_type_id = %s",
                (item_id,),
            )
            affected_doctor_ids = [int(row["doctor_id"]) for row in cursor.fetchall()]
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
            generation_results = [
                {
                    "doctor_id": linked_doctor_id,
                    "slot_generation": refresh_generated_slots(
                        linked_doctor_id,
                        datetime.now(IST).date().isoformat(),
                        30,
                    ),
                }
                for linked_doctor_id in affected_doctor_ids
            ]
            return response(200, {
                "message": "Consultation type deactivated",
                "slot_generation": generation_results,
            })

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
            # consultation_types is a shared catalog in the deployed RDS schema;
            # it does not contain doctor_id. Validate only that the selected type exists and is active.
            cursor.execute(
                """
                SELECT 1
                FROM consultation_types
                WHERE consultation_type_id = %s
                  AND is_active = TRUE
                LIMIT 1
                """,
                (consultation_type_id,),
            )
            if not cursor.fetchone():
                return response(400, {"message": "Consultation type not found or inactive"})
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
            # consultation_types is shared in the deployed RDS schema; no doctor_id column exists.
            cursor.execute(
                """
                SELECT 1 FROM consultation_types
                WHERE consultation_type_id = %s AND is_active = TRUE
                LIMIT 1
                """,
                (merged["consultation_type_id"],),
            )
            if not cursor.fetchone():
                return response(400, {"message": "Consultation type not found or inactive"})

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
        # Old slots can remain in DynamoDB for audit/history after an availability
        # pattern is changed or deleted. They must not be shown as bookable schedule slots.
        slots = [
            item for item in slots
            if str(item.get("status", "")).upper() != "INACTIVE"
        ]
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
def get_public_doctor(connection, doctor_id):
    """Patient-facing doctor profile: specialisations, hospitals and fees."""
    profile = get_profile(connection, doctor_id)
    if not profile or str(profile.get("profile_status", "")).upper() != "ACTIVE":
        return None

    # Internal fields are not needed by patients.
    for private_field in ("registration_number", "created_at", "updated_at"):
        profile.pop(private_field, None)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT h.hospital_id, h.hospital_name, h.branch_name,
                   h.address_line1, h.address_line2, h.city, h.state,
                   h.postal_code, h.phone
            FROM doctor_hospitals dh
            JOIN hospitals h ON h.hospital_id = dh.hospital_id
            WHERE dh.doctor_id = %s AND dh.is_active = TRUE AND h.is_active = TRUE
            ORDER BY h.hospital_name
            """,
            (doctor_id,),
        )
        profile["hospitals"] = cursor.fetchall()

        cursor.execute(
            """
            SELECT consultation_type_id, type_name, duration_minutes,
                   capacity, description, consultation_fee
            FROM consultation_types
            WHERE doctor_id = %s AND is_active = TRUE
            ORDER BY type_name
            """,
            (doctor_id,),
        )
        profile["consultation_types"] = cursor.fetchall()

    return profile


@log_execution
def public_doctor_slots(connection, doctor_id, query_params):
    """Return free, bookable slots using the doctor-time GSI."""
    query_params = query_params or {}
    today_ist = datetime.now(IST).date()

    try:
        start_date = datetime.strptime(
            query_params.get("start_date") or today_ist.isoformat(),
            "%Y-%m-%d",
        ).date()
        end_date = datetime.strptime(
            query_params.get("end_date")
            or (today_ist + timedelta(days=7)).isoformat(),
            "%Y-%m-%d",
        ).date()
    except ValueError:
        raise ValueError("start_date and end_date must use YYYY-MM-DD format")

    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if (end_date - start_date).days > 31:
        raise ValueError("Date range cannot exceed 31 days")

    table_name = os.environ.get("SLOTS_TABLE_NAME")
    if not table_name:
        raise RuntimeError("SLOTS_TABLE_NAME is not configured")

    table = dynamodb.Table(table_name)
    # slotTime is stored as an ISO-8601 IST string. Use an exclusive
    # midnight boundary for the day after end_date so the whole final day
    # is included without relying on string comparisons at 23:59:59.
    start_key = start_date.isoformat() + "T00:00:00"
    end_exclusive = (end_date + timedelta(days=1)).isoformat() + "T00:00:00"

    logger.info(
        "Public slot lookup: table=%s index=doctor-time-index doctor_id=%s "
        "start=%s end_exclusive=%s",
        table_name, doctor_id, start_key, end_exclusive,
    )

    key_condition = (
        Key("doctorId").eq(str(doctor_id))
        & Key("slotTime").gte(start_key)
        & Key("slotTime").lt(end_exclusive)
    )
    items = []
    result = table.query(
        IndexName="doctor-time-index",
        KeyConditionExpression=key_condition,
    )
    items.extend(result.get("Items", []))

    while result.get("LastEvaluatedKey"):
        result = table.query(
            IndexName="doctor-time-index",
            KeyConditionExpression=key_condition,
            ExclusiveStartKey=result["LastEvaluatedKey"],
        )
        items.extend(result.get("Items", []))

    now_ist = datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")
    slots = []
    for item in items:
        try:
            capacity = int(item.get("capacity", 1))
            booked = int(item.get("bookedCount", 0))
        except (TypeError, ValueError):
            logger.warning(
                "Skipping malformed slot counters doctor_id=%s slot_id=%s",
                doctor_id, item.get("slotId"),
            )
            continue

        if str(item.get("status", "")).upper() != "AVAILABLE":
            continue
        if item.get("isBlackout") is True or booked >= capacity:
            continue
        if str(item.get("slotTime", ""))[:19] <= now_ist:
            continue

        item["freeSeats"] = capacity - booked
        slots.append(item)

    slots.sort(key=lambda item: item.get("slotTime", ""))

    logger.info(
        "Public slot lookup complete: doctor_id=%s returned_slots=%s "
        "scanned_items=%s",
        doctor_id, len(slots), len(items),
    )

    return {
        "doctor_id": int(doctor_id),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "timezone": "Asia/Kolkata",
        "slots": slots,
    }


@log_execution
def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    logger.info("DOCTOR LAMBDA REQUEST START request_id=%s", request_id)
    try:
        identity, role = get_auth(event)
        if not identity:
            return response(401, {"message": "Unauthorized"})

        method = (event.get("httpMethod") or "").upper()
        request_path = get_request_path(event)
        parts = [part for part in request_path.split("/") if part]
        query_params = event.get("queryStringParameters") or {}
        data = get_request_body(event) if method in {"POST", "PUT", "PATCH"} else {}
        logger.info(
            "Request details: request_id=%s method=%s path=%s role=%s",
            request_id, method, request_path, role,
        )

        reserved = {
            "profile", "photo", "schedule", "availability", "blackouts",
            "consultation-types", "appointments", "slots", "specialisations",
        }
        is_public_detail = (
            len(parts) == 2 and parts[0] == "doctors" and parts[1] not in reserved
        )
        is_public_slots = (
            len(parts) == 3 and parts[0] == "doctors"
            and parts[1] not in reserved and parts[2] == "slots"
        )

        # Doctor's own profile.
        if method == "GET" and request_path == "/doctors/profile":
            if role != "doctor":
                return response(403, {"message": "Doctor access required"})
            with get_db_connection() as connection:
                doctor_id = doctor_id_from_auth(connection, identity)
                if not doctor_id:
                    return response(404, {"message": "Doctor profile not found"})
                profile = get_profile(connection, doctor_id)
                return response(200, profile) if profile else response(404, {"message": "Profile not found"})

        # Patient-facing discovery: list, detail, free slots.
        if method == "GET" and (request_path == "/doctors" or is_public_detail or is_public_slots):
            if role not in {"patient", "doctor", "admin"}:
                return response(403, {"message": "Valid account role required"})

            with get_db_connection() as connection:
                if request_path == "/doctors":
                    return response(200, list_doctors(connection, query_params))

                doctor_id = parts[1]
                if not doctor_id.isdigit() or int(doctor_id) <= 0:
                    return response(400, {"message": "doctor_id must be a positive integer"})

                doctor = get_public_doctor(connection, int(doctor_id))
                if not doctor:
                    return response(404, {"message": "Active doctor not found"})

                if is_public_slots:
                    return response(200, public_doctor_slots(connection, int(doctor_id), query_params))
                return response(200, doctor)

        # Everything below manages a doctor's own records.
        if role != "doctor":
            return response(403, {"message": "Doctor access required"})

        with get_db_connection() as connection:
            doctor_id = doctor_id_from_auth(connection, identity)
            if not doctor_id:
                return response(404, {"message": "Doctor profile not found"})

            if request_path == "/doctors/specialisations" and method == "GET":
                catalog, _ = get_doctor_specialisations(connection, doctor_id)
                return response(200, catalog)

            if request_path in {"/doctors/profile", "/doctors"} and method == "PUT":
                return update_profile(connection, doctor_id, data)

            if request_path == "/doctors/photo" and method == "POST":
                return create_photo_upload(connection, doctor_id, data)

            if request_path == "/doctors/schedule" and method == "GET":
                return doctor_schedule(connection, doctor_id, query_params)

            if len(parts) >= 2 and parts[0] == "doctors":
                resource = parts[1]
                item_id = parts[2] if len(parts) > 2 else None
                if item_id is not None and (not item_id.isdigit() or int(item_id) <= 0):
                    return response(400, {"message": "Resource ID must be a positive integer"})

                if resource == "consultation-types":
                    return consultation_types(connection, doctor_id, method, data, item_id)
                if resource == "availability":
                    return availability(connection, doctor_id, method, data, item_id)
                if resource == "blackouts":
                    return blackouts(connection, doctor_id, method, data, item_id)

        logger.warning("No route matched method=%s path=%s", method, request_path)
        return response(404, {"message": "Route not found"})

    except ValueError as error:
        logger.warning("Request validation failed request_id=%s error=%s", request_id, error)
        return response(400, {"message": str(error)})
    except Exception as exc:
        logger.exception(
            "Doctor request failed request_id=%s method=%s path=%s error_type=%s",
            request_id,
            (event.get("httpMethod") or "").upper(),
            event.get("path") or event.get("rawPath") or "",
            type(exc).__name__,
        )
        return response(500, {"message": "Internal server error"})
    finally:
        logger.info("DOCTOR LAMBDA REQUEST END request_id=%s", request_id)
