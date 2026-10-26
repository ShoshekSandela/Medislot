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
        auth = event.get("requestContext", {}).get("authorizer", {})
        user_id = auth.get("userId")
        role = str(auth.get("role", "")).lower()

        if not user_id:
            return response(401, {"message": "Unauthorized"})

        if role != "patient":
            return response(403, {"message": "Patient access required"})

        method = event.get("httpMethod", "")
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