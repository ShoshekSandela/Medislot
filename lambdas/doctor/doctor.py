import json
import logging
import os
import uuid
import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)
secrets_client = boto3.client("secretsmanager")


def response(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body, default=str)}


def get_db_connection():
    secret = json.loads(secrets_client.get_secret_value(SecretId=os.environ["DB_SECRET_ARN"])["SecretString"])
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
    auth = event.get("requestContext", {}).get("authorizer", {}) or {}
    if isinstance(auth.get("lambda"), dict):
        auth = {**auth, **auth["lambda"]}
    return auth.get("userId") or auth.get("doctorId") or auth.get("sub"), str(auth.get("role", "")).lower()


def body(event):
    try:
        return json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        raise ValueError("Request body must be valid JSON")


def doctor_id_from_auth(connection, identity):
    """
    The authorizer normally provides the authenticated user's userId.
    The doctors table has a separate doctor_id and user_id, so resolve
    either value safely.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT doctor_id FROM doctors WHERE user_id = %s OR doctor_id = %s LIMIT 1",
            (identity, identity),
        )
        row = cursor.fetchone()
        return row["doctor_id"] if row else None


def path(event):
    return event.get("path") or event.get("rawPath") or ""


def get_profile(connection, doctor_id):
    with connection.cursor() as cursor:
        cursor.execute("SELECT doctor_id, registration_number, first_name, last_name, qualification, experience_years, profile_photo_url, profile_status, created_at, updated_at FROM doctors WHERE doctor_id = %s", (doctor_id,))
        return cursor.fetchone()


def update_profile(connection, doctor_id, data):
    allowed = {"first_name", "last_name", "qualification", "experience_years", "profile_photo_url"}
    updates = {key: value for key, value in data.items() if key in allowed}
    if not updates:
        return response(400, {"message": "No valid fields"})
    columns = ", ".join(f"{key} = %s" for key in updates)
    with connection.cursor() as cursor:
        cursor.execute(f"UPDATE doctors SET {columns}, updated_at = UTC_TIMESTAMP(3) WHERE doctor_id = %s", list(updates.values()) + [doctor_id])
    connection.commit()
    return response(200, {"message": "Profile updated"})


def consultation_types(connection, method, data, item_id=None):
    with connection.cursor() as cursor:
        if method == "GET":
            if item_id:
                cursor.execute("SELECT * FROM consultation_types WHERE consultation_type_id = %s", (item_id,))
                row = cursor.fetchone()
                return response(200, row) if row else response(404, {"message": "Consultation type not found"})
            cursor.execute("SELECT * FROM consultation_types ORDER BY type_name")
            return response(200, cursor.fetchall())
        if method == "POST":
            required = ["type_name", "duration_minutes", "consultation_fee"]
            missing = [key for key in required if data.get(key) in (None, "")]
            if missing:
                return response(400, {"message": "Missing required fields", "fields": missing})
            consultation_id = str(uuid.uuid4())
            cursor.execute("INSERT INTO consultation_types (consultation_type_id, type_name, duration_minutes, capacity, description, is_active, consultation_fee) VALUES (%s,%s,%s,%s,%s,%s,%s)", (consultation_id, data["type_name"], data["duration_minutes"], data.get("capacity", 1), data.get("description"), data.get("is_active", True), data["consultation_fee"]))
            connection.commit()
            return response(201, {"message": "Consultation type created", "consultation_type_id": consultation_id})
        if method == "PUT" and item_id:
            allowed = {"type_name", "duration_minutes", "capacity", "description", "is_active", "consultation_fee"}
            updates = {key: value for key, value in data.items() if key in allowed}
            if not updates:
                return response(400, {"message": "No valid fields"})
            cursor.execute(f"UPDATE consultation_types SET {', '.join(f'{key} = %s' for key in updates)} WHERE consultation_type_id = %s", list(updates.values()) + [item_id])
            connection.commit()
            return response(200, {"message": "Consultation type updated"})
        if method == "DELETE" and item_id:
            cursor.execute("UPDATE consultation_types SET is_active = FALSE WHERE consultation_type_id = %s", (item_id,))
            connection.commit()
            return response(200, {"message": "Consultation type deactivated"})
    return response(405, {"message": "Method not allowed"})


def availability(connection, doctor_id, method, data, item_id=None):
    with connection.cursor() as cursor:
        if method == "GET":
            cursor.execute("SELECT * FROM doctor_availability WHERE doctor_id = %s ORDER BY day_of_week, start_time", (doctor_id,))
            return response(200, cursor.fetchall())
        if method == "POST":
            required = ["hospital_id", "consultation_type_id", "day_of_week", "start_time", "end_time", "effective_from"]
            missing = [key for key in required if data.get(key) in (None, "")]
            if missing:
                return response(400, {"message": "Missing required fields", "fields": missing})
            availability_id = str(uuid.uuid4())
            cursor.execute("INSERT INTO doctor_availability (availability_id, doctor_id, hospital_id, consultation_type_id, day_of_week, start_time, end_time, effective_from, effective_until, is_active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (availability_id, doctor_id, data["hospital_id"], data["consultation_type_id"], data["day_of_week"], data["start_time"], data["end_time"], data["effective_from"], data.get("effective_until"), data.get("is_active", True)))
            connection.commit()
            return response(201, {"message": "Availability created", "availability_id": availability_id})
        if method == "PUT" and item_id:
            allowed = {"hospital_id", "consultation_type_id", "day_of_week", "start_time", "end_time", "effective_from", "effective_until", "is_active"}
            updates = {key: value for key, value in data.items() if key in allowed}
            if not updates:
                return response(400, {"message": "No valid fields"})
            cursor.execute(f"UPDATE doctor_availability SET {', '.join(f'{key} = %s' for key in updates)} WHERE availability_id = %s AND doctor_id = %s", list(updates.values()) + [item_id, doctor_id])
            connection.commit()
            return response(200, {"message": "Availability updated"})
        if method == "DELETE" and item_id:
            cursor.execute("DELETE FROM doctor_availability WHERE availability_id = %s AND doctor_id = %s", (item_id, doctor_id))
            connection.commit()
            return response(200, {"message": "Availability deleted"})
    return response(405, {"message": "Method not allowed"})


def blackouts(connection, doctor_id, method, data, item_id=None):
    with connection.cursor() as cursor:
        if method == "GET":
            cursor.execute("SELECT * FROM doctor_blackouts WHERE doctor_id = %s ORDER BY start_at_utc", (doctor_id,))
            return response(200, cursor.fetchall())
        if method == "POST":
            required = ["start_at_utc", "end_at_utc"]
            missing = [key for key in required if data.get(key) in (None, "")]
            if missing:
                return response(400, {"message": "Missing required fields", "fields": missing})
            blackout_id = str(uuid.uuid4())
            cursor.execute("INSERT INTO doctor_blackouts (blackout_id, doctor_id, hospital_id, start_at_utc, end_at_utc, reason) VALUES (%s,%s,%s,%s,%s,%s)", (blackout_id, doctor_id, data.get("hospital_id"), data["start_at_utc"], data["end_at_utc"], data.get("reason")))
            connection.commit()
            return response(201, {"message": "Blackout created", "blackout_id": blackout_id})
        if method == "DELETE" and item_id:
            cursor.execute("DELETE FROM doctor_blackouts WHERE blackout_id = %s AND doctor_id = %s", (item_id, doctor_id))
            connection.commit()
            return response(200, {"message": "Blackout deleted"})
    return response(405, {"message": "Method not allowed"})


def lambda_handler(event, context):
    try:
        identity, role = get_auth(event)
        if not identity:
            return response(401, {"message": "Unauthorized"})
        if role != "doctor":
            return response(403, {"message": "Doctor access required"})
        method = (event.get("httpMethod") or "").upper()
        request_path = path(event).rstrip("/")
        parts = request_path.split("/")
        data = body(event) if method in {"POST", "PUT", "PATCH"} else {}
        with get_db_connection() as connection:
            doctor_id = doctor_id_from_auth(connection, identity)
            if not doctor_id:
                return response(404, {"message": "Doctor profile not found"})
            if request_path == "/doctors":
                if method == "GET":
                    profile = get_profile(connection, doctor_id)
                    return response(200, profile) if profile else response(404, {"message": "Profile not found"})
                if method == "PUT":
                    return update_profile(connection, doctor_id, data)
            if len(parts) >= 3 and parts[1] == "doctors":
                resource = parts[2]
                item_id = parts[3] if len(parts) > 3 else None
                if resource == "consultation-types":
                    return consultation_types(connection, method, data, item_id)
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
