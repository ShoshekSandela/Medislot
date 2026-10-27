import os
import json
import logging
import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets = boto3.client("secretsmanager")
secret = json.loads(
    secrets.get_secret_value(
        SecretId=os.environ["DB_SECRET_ARN"]
    )["SecretString"]
)


def connect_db():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        user=secret["username"],
        password=secret["password"],
        database=os.environ["DB_NAME"],
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5
    )


def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str)
    }


def lambda_handler(event, context):
    try:
        # This function is a protected patient-profile API, not the login handler.
        # Direct Lambda console tests do not automatically include API Gateway's
        # requestContext.authorizer claims, so they will return 401 unless the
        # test event includes a simulated authorizer context.
        request_context = event.get("requestContext") or {}
        auth = request_context.get("authorizer") or {}

        # REST API Lambda authorizers commonly place custom claims under
        # authorizer.lambda; support both that shape and direct custom fields.
        if isinstance(auth.get("lambda"), dict):
            auth = {**auth, **auth["lambda"]}

        user_id = auth.get("userId") or auth.get("user_id") or auth.get("sub")
        role = str(auth.get("role", "")).lower()

        if not user_id:
            logger.warning(
                "Patient request rejected: no authorizer user identity. "
                "Test this protected endpoint through API Gateway with a valid "
                "Bearer token, or include requestContext.authorizer in a Lambda test event."
            )
            return response(401, {"message": "Unauthorized: valid authenticated user context is required"})

        if role != "patient":
            return response(403, {"message": "Patient access required"})

        method = (event.get("httpMethod") or (event.get("requestContext", {}).get("http", {}) or {}).get("method", "")).upper()
        body = json.loads(event.get("body") or "{}")

        with connect_db() as conn:
            with conn.cursor() as cur:

                if method == "GET":
                    cur.execute(
                        "SELECT patient_id, user_id, first_name, "
                        "last_name, phone, date_of_birth, gender "
                        "FROM patients WHERE user_id = %s",
                        (user_id,)
                    )
                    patient = cur.fetchone()

                    if not patient:
                        return response(
                            404, {"message": "Profile not found"}
                        )

                    return response(200, patient)

                if method == "PUT":
                    allowed = {
                        "first_name", "last_name", "phone",
                        "date_of_birth", "gender"
                    }
                    updates = {
                        key: value for key, value in body.items()
                        if key in allowed
                    }

                    if not updates:
                        return response(
                            400, {"message": "No valid fields"}
                        )

                    columns = ", ".join(
                        f"{key} = %s" for key in updates
                    )
                    values = list(updates.values()) + [user_id]

                    cur.execute(
                        f"UPDATE patients SET {columns}, "
                        "updated_at = UTC_TIMESTAMP(3) "
                        "WHERE user_id = %s",
                        values
                    )
                    conn.commit()

                    if cur.rowcount == 0:
                        cur.execute(
                            "SELECT patient_id FROM patients "
                            "WHERE user_id = %s",
                            (user_id,)
                        )
                        if not cur.fetchone():
                            return response(
                                404, {"message": "Profile not found"}
                            )

                    return response(
                        200, {"message": "Profile updated"}
                    )

                if method == "DELETE":
                    return response(
                        409,
                        {
                            "message": (
                                "Profile deletion requires a defined "
                                "data-retention policy"
                            )
                        }
                    )

                return response(405, {"message": "Method not allowed"})

    except Exception:
        logger.exception("Patient request failed")
        return response(500, {"message": "Internal server error"})