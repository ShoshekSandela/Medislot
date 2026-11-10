
import os
import json
import base64
import logging
from datetime import datetime

import boto3
import pymysql
from zoneinfo import ZoneInfo

logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets_client = boto3.client("secretsmanager")


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
            autocommit=False
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
            "Cache-Control": "no-store"
        },
        "body": json.dumps(body, default=str)
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
    authorizer = (
        event.get("requestContext", {}).get("authorizer", {}) or {}
    )

    # Supports REST API and HTTP API Lambda authorizer context.
    if isinstance(authorizer.get("lambda"), dict):
        authorizer = {
            **authorizer,
            **authorizer["lambda"]
        }

    return authorizer


def get_patient_identity(event):
    auth = get_auth_context(event)

    patient_id = (
        auth.get("profileId")
        or auth.get("principalId")
        or auth.get("sub")
    )

    role = str(auth.get("role") or "").strip().lower()

    if not patient_id:
        return None, role

    try:
        patient_id = int(patient_id)

        if patient_id <= 0:
            return None, role

    except (TypeError, ValueError):
        return None, role

    return patient_id, role


def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    event = event or {}
    request_context = event.get("requestContext") or {}
    method = (event.get("httpMethod") or (request_context.get("http") or {}).get("method") or "").upper()
    path = event.get("path") or event.get("rawPath") or request_context.get("path") or ""
    logger.info("PATIENT REQUEST START request_id=%s method=%s path=%s", request_id, method, path)
    connection = None

    try:
        logger.info("Patient step 1/5: resolving authenticated patient identity")
        patient_id, role = get_patient_identity(event)
        logger.info("Patient identity resolved request_id=%s role=%s authenticated=%s", request_id, role, bool(patient_id))

        if not patient_id:
            return response(401, {
                "message": "Unauthorized"
            })

        if role != "patient":
            return response(403, {
                "message": "Patient access required"
            })

        method = (
            event.get("httpMethod")
            or event.get("requestContext", {})
                .get("http", {})
                .get("method")
            or ""
        ).upper()

        if method not in ["GET", "PUT", "DELETE"]:
            return response(405, {
                "message": "Method not allowed"
            })

        body = parse_body(event) if method == "PUT" else {}

        logger.info("Patient step 2/5: connecting to database request_id=%s", request_id)
        connection = connect_db()
        logger.info("Patient step 3/5: executing patient route method=%s", method)

        with connection.cursor() as cursor:

            # --------------------------------
            # GET PATIENT PROFILE
            # GET /patients
            # --------------------------------

            if method == "GET":

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
                    (patient_id,)
                )

                patient = cursor.fetchone()

                if not patient:
                    return response(404, {
                        "message": "Patient profile not found"
                    })

                return response(200, {
                    "message": "Patient profile retrieved successfully",
                    "patient": patient
                })

            # --------------------------------
            # UPDATE PATIENT PROFILE
            # PUT /patients
            # --------------------------------

            if method == "PUT":

                allowed_fields = {
                    "first_name",
                    "last_name",
                    "phone",
                    "date_of_birth",
                    "gender"
                }

                unknown_fields = (
                    set(body.keys()) - allowed_fields
                )

                if unknown_fields:
                    return response(400, {
                        "message": "Unsupported fields",
                        "fields": sorted(unknown_fields)
                    })

                if not body:
                    return response(400, {
                        "message": "Provide at least one field to update"
                    })

                updates = {}

                for field in allowed_fields:

                    if field not in body:
                        continue

                    value = body[field]

                    if field in [
                        "first_name",
                        "last_name",
                        "phone",
                        "gender"
                    ]:

                        if value is not None and not isinstance(value, str):
                            return response(400, {
                                "message": f"{field} must be a string"
                            })

                        if isinstance(value, str):
                            value = value.strip()

                    updates[field] = value

                # First name validation
                if "first_name" in updates:
                    if not updates["first_name"]:
                        return response(400, {
                            "message": "first_name cannot be empty"
                        })

                    if len(updates["first_name"]) > 100:
                        return response(400, {
                            "message": "first_name is too long"
                        })

                # Last name validation
                if "last_name" in updates:
                    if updates["last_name"] is not None and len(updates["last_name"]) > 100:
                        return response(400, {
                            "message": "last_name is too long"
                        })

                # Phone validation
                if "phone" in updates:
                    phone = updates["phone"]

                    if phone and len(phone) > 20:
                        return response(400, {
                            "message": "Phone number must be at most 20 characters"
                        })

                # Gender validation
                if "gender" in updates:
                    gender = updates["gender"]

                    valid_genders = [
                        None,
                        "",
                        "male",
                        "female",
                        "other",
                        "prefer_not_to_say"
                    ]

                    if gender not in valid_genders:
                        return response(400, {
                            "message": "Invalid gender"
                        })

                # Date of birth validation
                if "date_of_birth" in updates:

                    dob = updates["date_of_birth"]

                    if dob:
                        try:
                            parsed_dob = datetime.strptime(
                                dob,
                                "%Y-%m-%d"
                            ).date()

                            if parsed_dob > datetime.now(ZoneInfo('Asia/Kolkata')).date():
                                return response(400, {
                                    "message": "Date of birth cannot be in the future"
                                })

                        except (ValueError, TypeError):
                            return response(400, {
                                "message": "date_of_birth must use YYYY-MM-DD format"
                            })

                columns = ", ".join(
                    f"{field} = %s"
                    for field in updates
                )

                values = list(updates.values())
                values.append(patient_id)

                cursor.execute(
                    f"""
                    UPDATE patients
                    SET {columns},
                        updated_at = CURRENT_TIMESTAMP()
                    WHERE patient_id = %s
                    """,
                    values
                )

                connection.commit()

                # Check whether patient exists if no values changed.
                if cursor.rowcount == 0:

                    cursor.execute(
                        """
                        SELECT patient_id
                        FROM patients
                        WHERE patient_id = %s
                        """,
                        (patient_id,)
                    )

                    if not cursor.fetchone():
                        return response(404, {
                            "message": "Patient profile not found"
                        })

                return response(200, {
                    "message": "Patient profile updated successfully",
                    "patient_id": patient_id
                })

            # --------------------------------
            # DELETE PATIENT
            # DELETE /patients
            # --------------------------------

            if method == "DELETE":

                # Do not hard-delete patient records because
                # appointments and medical history may reference them.

                return response(409, {
                    "message": "Patient deletion is disabled",
                    "details": "Contact support for account deactivation"
                })

    except ValueError as exc:
        logger.warning("Patient request validation failed request_id=%s error=%s", request_id, exc)
        return response(400, {
            "message": str(exc)
        })

    except pymysql.err.IntegrityError:
        if connection:
            connection.rollback()

        logger.exception("Patient database integrity error")

        return response(409, {
            "message": "Database constraint violation"
        })

    except Exception:
        if connection:
            connection.rollback()

        logger.exception("Patient Lambda error")

        return response(500, {
            "message": "Internal server error"
        })

    finally:
        if connection:
            connection.close()
        logger.info("PATIENT REQUEST END request_id=%s", request_id)
