
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
                d.profile_status
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
