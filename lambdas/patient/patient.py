"""MediSlot Patient Lambda: authenticated patient profile operations.

Supported routes:
  GET    /patients  - retrieve the authenticated patient's profile
  PUT    /patients  - update the authenticated patient's profile

Account deactivation is intentionally not implemented here because the current
patients table may not contain an account_status column. No schema migration or
account_status column is required for profile retrieval and updates.
"""

import os
import json
import base64
import logging
from datetime import datetime, timedelta, timezone

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets_client = boto3.client("secretsmanager")
IST = timezone(timedelta(hours=5, minutes=30))


def connect_db():
    logger.info("STEP START: patient database connection")
    try:
        secret_response = secrets_client.get_secret_value(
            SecretId=os.environ["DB_SECRET_ARN"]
        )
        secret = json.loads(secret_response["SecretString"])

        connection = pymysql.connect(
            host=os.environ["DB_HOST"],
            user=secret.get("username") or secret.get("user"),
            password=secret["password"],
            database=os.environ.get("DB_NAME", "medislot"),
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
            init_command="SET time_zone = '+05:30'",
            read_timeout=10,
            write_timeout=10,
            autocommit=False,
        )
        logger.info("STEP COMPLETE: patient database connection")
        return connection
    except Exception:
        logger.exception("STEP FAILED: patient database connection")
        raise


def response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body, default=str),
    }


def parse_body(event):
    raw = event.get("body") or "{}"

    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")

    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise ValueError("Request body must contain valid JSON")

    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")

    return data


def get_auth_context(event):
    request_context = event.get("requestContext") or {}
    authorizer = request_context.get("authorizer") or {}

    # HTTP API Lambda authorizers may nest custom context under "lambda".
    lambda_context = authorizer.get("lambda")
    if isinstance(lambda_context, dict):
        authorizer = {**authorizer, **lambda_context}

    # REST API Cognito authorizers may put claims under "claims".
    claims = authorizer.get("claims")
    if isinstance(claims, dict):
        authorizer = {**authorizer, **claims}

    return authorizer


def get_patient_identity(event):
    auth = get_auth_context(event)

    patient_id = (
        auth.get("profileId")
        or auth.get("principalId")
        or auth.get("sub")
        or auth.get("patient_id")
    )
    role = str(auth.get("role") or auth.get("custom:role") or "").strip().lower()

    if not patient_id:
        return None, role

    try:
        patient_id = int(patient_id)
    except (TypeError, ValueError):
        return None, role

    if patient_id <= 0:
        return None, role

    return patient_id, role


def validate_profile_updates(body):
    allowed_fields = {
        "first_name",
        "last_name",
        "phone",
        "date_of_birth",
        "gender",
    }

    unknown_fields = set(body) - allowed_fields
    if unknown_fields:
        raise ValueError(
            "Unsupported fields: " + ", ".join(sorted(unknown_fields))
        )

    if not body:
        raise ValueError("Provide at least one field to update")

    updates = {}
    for field, value in body.items():
        if field in {"first_name", "last_name", "phone", "gender"}:
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field} must be a string")
            if isinstance(value, str):
                value = value.strip()
        updates[field] = value

    if "first_name" in updates:
        first_name = updates["first_name"]
        if not first_name:
            raise ValueError("first_name cannot be empty")
        if len(first_name) > 100:
            raise ValueError("first_name is too long")

    if "last_name" in updates:
        last_name = updates["last_name"]
        if last_name is not None and len(last_name) > 100:
            raise ValueError("last_name is too long")

    if "phone" in updates:
        phone = updates["phone"]
        if phone and len(phone) > 20:
            raise ValueError("Phone number must be at most 20 characters")

    if "gender" in updates:
        valid_genders = {
            None,
            "",
            "male",
            "female",
            "other",
            "prefer_not_to_say",
        }
        if updates["gender"] not in valid_genders:
            raise ValueError("Invalid gender")

    if "date_of_birth" in updates:
        dob = updates["date_of_birth"]
        if dob:
            if not isinstance(dob, str):
                raise ValueError("date_of_birth must use YYYY-MM-DD format")
            try:
                parsed_dob = datetime.strptime(dob, "%Y-%m-%d").date()
            except ValueError:
                raise ValueError("date_of_birth must use YYYY-MM-DD format")

            if parsed_dob > datetime.now(IST).date():
                raise ValueError("Date of birth cannot be in the future")
        else:
            updates["date_of_birth"] = None

    return updates


def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    event = event or {}
    request_context = event.get("requestContext") or {}
    http_context = request_context.get("http") or {}
    method = (event.get("httpMethod") or http_context.get("method") or "").upper()
    path = event.get("path") or event.get("rawPath") or request_context.get("path") or ""

    logger.info(
        "PATIENT REQUEST START request_id=%s method=%s path=%s",
        request_id,
        method,
        path,
    )

    connection = None

    try:
        logger.info("Patient step 1: resolving authenticated patient identity")
        patient_id, role = get_patient_identity(event)
        logger.info(
            "Patient identity resolved request_id=%s role=%s authenticated=%s",
            request_id,
            role,
            bool(patient_id),
        )

        if not patient_id:
            return response(401, {"message": "Unauthorized"})

        if role != "patient":
            return response(403, {"message": "Patient access required"})

        if method == "DELETE":
            return response(501, {
                "message": "Patient account deactivation is not available until the database supports it."
            })

        if method not in {"GET", "PUT"}:
            return response(405, {"message": "Method not allowed"})

        body = parse_body(event) if method == "PUT" else {}
        updates = validate_profile_updates(body) if method == "PUT" else None

        logger.info("Patient step 2: connecting to database request_id=%s", request_id)
        connection = connect_db()

        with connection.cursor() as cursor:
            if method == "GET":
                logger.info("Patient step 3: retrieving profile patient_id=%s", patient_id)
                cursor.execute(
                    """
                    SELECT
                        patient_id,
                        first_name,
                        last_name,
                        email,
                        phone,
                        date_of_birth,
                        gender,
                        created_at,
                        updated_at
                    FROM patients
                    WHERE patient_id = %s
                    LIMIT 1
                    """,
                    (patient_id,),
                )
                patient = cursor.fetchone()

                if not patient:
                    return response(404, {"message": "Patient profile not found"})

                logger.info(
                    "PATIENT PROFILE RETRIEVED patient_id=%s request_id=%s",
                    patient_id,
                    request_id,
                )
                return response(200, {
                    "message": "Patient profile retrieved successfully",
                    "patient": patient,
                })

            if method == "PUT":
                logger.info("Patient step 3: updating profile patient_id=%s", patient_id)
                set_clause = ", ".join(f"{field} = %s" for field in updates)
                values = list(updates.values()) + [patient_id]

                cursor.execute(
                    f"""
                    UPDATE patients
                    SET {set_clause}, updated_at = CURRENT_TIMESTAMP
                    WHERE patient_id = %s
                    """,
                    values,
                )

                # rowcount can be zero when the submitted values equal existing values.
                cursor.execute(
                    "SELECT patient_id FROM patients WHERE patient_id = %s LIMIT 1",
                    (patient_id,),
                )
                existing_patient = cursor.fetchone()

                if not existing_patient:
                    connection.rollback()
                    return response(404, {"message": "Patient profile not found"})

                connection.commit()
                logger.info(
                    "PATIENT PROFILE UPDATED patient_id=%s request_id=%s",
                    patient_id,
                    request_id,
                )
                return response(200, {
                    "message": "Patient profile updated successfully",
                    "patient_id": patient_id,
                })

    except ValueError as exc:
        if connection:
            connection.rollback()
        logger.warning(
            "Patient request validation failed request_id=%s error=%s",
            request_id,
            exc,
        )
        return response(400, {"message": str(exc)})

    except pymysql.err.IntegrityError:
        if connection:
            connection.rollback()
        logger.exception("Patient database integrity error request_id=%s", request_id)
        return response(409, {"message": "Database constraint violation"})

    except Exception:
        if connection:
            connection.rollback()
        logger.exception("Patient Lambda error request_id=%s", request_id)
        return response(500, {"message": "Internal server error"})

    finally:
        if connection:
            connection.close()
        logger.info("PATIENT REQUEST END request_id=%s", request_id)
