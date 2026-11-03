import json
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
import pymysql
from botocore.exceptions import ClientError


secrets_client = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")


def get_db_connection():
    secret_arn = os.environ["DB_SECRET_ARN"]
    db_host = os.environ["DB_HOST"]
    db_name = os.environ["DB_NAME"]

    secret_response = secrets_client.get_secret_value(
        SecretId=secret_arn
    )

    secret = json.loads(secret_response["SecretString"])

    return pymysql.connect(
        host=db_host,
        user=secret["username"],
        password=secret["password"],
        database=db_name,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True
    )


def get_date_range(event):
    """
    Default: rolling 30-day window.

    For testing, EventBridge/manual invocation can send:
    {
        "start_date": "2026-11-02",
        "days": 1
    }
    """

    start_date_value = event.get("start_date")

    if start_date_value:
        start_day = date.fromisoformat(start_date_value)
    else:
        start_day = datetime.now(timezone.utc).date()

    days = int(event.get(
        "days",
        os.environ.get("SLOT_WINDOW_DAYS", "30")
    ))
    if days < 1 or days > 90:
        raise ValueError("days must be between 1 and 90")

    return start_day, days


def get_availability(connection, day_of_week, target_date):
    sql = """
        SELECT
            da.availability_id,
            da.doctor_id,
            da.hospital_id,
            da.consultation_type_id,
            da.start_time,
            da.end_time,
            da.timezone,
            ct.duration_minutes
        FROM doctor_availability da
        JOIN consultation_types ct
            ON ct.consultation_type_id = da.consultation_type_id
        WHERE da.day_of_week = %s
          AND da.is_active = TRUE
          AND ct.is_active = TRUE
          AND da.effective_from <= %s
          AND (
              da.effective_until IS NULL
              OR da.effective_until >= %s
          )
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, (day_of_week, target_date, target_date))
        return cursor.fetchall()


def is_blackout(connection, doctor_id, hospital_id, start_utc, end_utc):
    sql = """
        SELECT blackout_id
        FROM doctor_blackouts
        WHERE doctor_id = %s
          AND (
              hospital_id IS NULL
              OR hospital_id = %s
          )
          AND start_at_utc < %s
          AND end_at_utc > %s
        LIMIT 1
    """

    with connection.cursor() as cursor:
        cursor.execute(
            sql,
            (
                doctor_id,
                hospital_id,
                end_utc.replace(tzinfo=None),
                start_utc.replace(tzinfo=None)
            )
        )

        return cursor.fetchone() is not None


def create_slot(
    slots_table,
    availability,
    start_local,
    end_local,
    connection
):
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    if is_blackout(
        connection,
        availability["doctor_id"],
        availability["hospital_id"],
        start_utc,
        end_utc
    ):
        return "blackout"

    # Deterministic ID = same slot always generates the same ID
    slot_id = (
        f"{availability['doctor_id']}"
        f"#{availability['hospital_id']}"
        f"#{availability['consultation_type_id']}"
        f"#{start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )

    item = {
        "slotId": slot_id,
        "doctorId": availability["doctor_id"],
        "hospitalId": availability["hospital_id"],
        "consultationTypeId": availability["consultation_type_id"],
        "slotTime": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "AVAILABLE",
        "version": 1,
        "timezone": availability["timezone"]
    }

    try:
        slots_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(slotId)"
        )

        return "created"

    except ClientError as error:
        if (
            error.response["Error"]["Code"]
            == "ConditionalCheckFailedException"
        ):
            return "duplicate"

        raise


def materialize_day(connection, slots_table, target_date):
    day_of_week = target_date.weekday()

    availability_records = get_availability(
        connection,
        day_of_week,
        target_date
    )

    results = {
        "created": 0,
        "duplicates": 0,
        "blackouts": 0
    }

    for availability in availability_records:

        timezone_name = availability["timezone"]

        if not timezone_name:
            timezone_name = os.environ.get(
                "DEFAULT_TIMEZONE",
                "UTC"
            )

        local_timezone = ZoneInfo(timezone_name)

        start_local = datetime.combine(
            target_date,
            availability["start_time"]
        ).replace(tzinfo=local_timezone)

        end_local = datetime.combine(
            target_date,
            availability["end_time"]
        ).replace(tzinfo=local_timezone)

        duration_minutes = int(
            availability["duration_minutes"]
        )

        # Option 3: add a 15-minute buffer after each consultation.
        # The buffer is not a bookable slot and must fit inside the doctor's
        # availability window before another appointment can start.
        buffer_minutes = int(os.environ.get("SLOT_BUFFER_MINUTES", "15"))
        if duration_minutes <= 0 or buffer_minutes < 0:
            raise ValueError("Consultation duration must be positive and buffer cannot be negative")

        current_start = start_local

        while current_start + timedelta(minutes=duration_minutes) <= end_local:

            current_end = current_start + timedelta(
                minutes=duration_minutes
            )

            result = create_slot(
                slots_table,
                availability,
                current_start,
                current_end,
                connection
            )

            if result == "created":
                results["created"] += 1

            elif result == "duplicate":
                results["duplicates"] += 1

            elif result == "blackout":
                results["blackouts"] += 1

            current_start = current_end + timedelta(minutes=buffer_minutes)

    return results


def lambda_handler(event, context):
    event = event or {}

    slots_table = dynamodb.Table(
        os.environ["SLOTS_TABLE_NAME"]
    )

    try:
        start_day, days = get_date_range(event)
    except (TypeError, ValueError) as exc:
        return {"statusCode": 400, "body": json.dumps({"message": str(exc)})}

    connection = get_db_connection()

    total_results = {
        "created": 0,
        "duplicates": 0,
        "blackouts": 0
    }

    try:
        for offset in range(days):

            target_date = start_day + timedelta(days=offset)

            result = materialize_day(
                connection,
                slots_table,
                target_date
            )

            for key in total_results:
                total_results[key] += result[key]

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Slot materialization completed",
                "start_date": str(start_day),
                "days": days,
                "results": total_results
            })
        }

    finally:
        connection.close()