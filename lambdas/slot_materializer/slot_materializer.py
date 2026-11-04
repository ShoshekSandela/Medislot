"""MediSlot slot materialiser.

Availability, blackout and generated slot times use IST (Asia/Kolkata).
Day-of-week follows the database convention:
0=Sunday, 1=Monday, ..., 6=Saturday.
"""

import json
import logging
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import boto3
import pymysql
from botocore.exceptions import ClientError


logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets_client = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")

IST = ZoneInfo("Asia/Kolkata")


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
        autocommit=True,
        init_command="SET time_zone = '+05:30'",
    )


def get_date_range(event):
    """Return requested IST date range."""

    start_date_value = event.get("start_date")

    start_day = (
        date.fromisoformat(start_date_value)
        if start_date_value
        else datetime.now(IST).date()
    )

    days = int(
        event.get(
            "days",
            os.environ.get("SLOT_WINDOW_DAYS", "30")
        )
    )

    if days < 1 or days > 90:
        raise ValueError("days must be between 1 and 90")

    return start_day, days


def mysql_time_to_time(value):
    """Convert MySQL TIME values safely."""

    if isinstance(value, time):
        return value.replace(tzinfo=None)

    if isinstance(value, timedelta):
        total_seconds = int(value.total_seconds())

        if total_seconds < 0 or total_seconds >= 86400:
            raise ValueError(
                "Availability time must be within one IST day"
            )

        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        return time(hours, minutes, seconds)

    if isinstance(value, str):
        return time.fromisoformat(value)

    raise ValueError(
        f"Unsupported MySQL TIME value: {type(value).__name__}"
    )


def get_availability(connection, day_of_week, target_date):

    sql = """
        SELECT
            da.availability_id,
            da.doctor_id,
            da.hospital_id,
            da.consultation_type_id,
            da.start_time,
            da.end_time,
            ct.duration_minutes,
            ct.consultation_fee,
            ct.capacity
        FROM doctor_availability da

        JOIN consultation_types ct
            ON ct.consultation_type_id =
               da.consultation_type_id

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
        cursor.execute(
            sql,
            (day_of_week, target_date, target_date)
        )

        return cursor.fetchall()


def is_blackout(
    connection,
    doctor_id,
    hospital_id,
    start_ist,
    end_ist
):

    sql = """
        SELECT blackout_id
        FROM doctor_blackouts
        WHERE doctor_id = %s
          AND hospital_id = %s
          AND start_at_ist < %s
          AND end_at_ist > %s
        LIMIT 1
    """

    with connection.cursor() as cursor:
        cursor.execute(
            sql,
            (
                doctor_id,
                hospital_id,
                end_ist.replace(tzinfo=None),
                start_ist.replace(tzinfo=None),
            ),
        )

        return cursor.fetchone() is not None


def create_slot(
    slots_table,
    availability,
    start_ist,
    end_ist,
    connection
):
    """
    Persist every generated availability slot.

    A blackout changes the slot status; it does not prevent the slot record
    from being written. Existing slots are not overwritten, and their
    bookedCount is never reset.
    """
    blackout = is_blackout(
        connection,
        availability["doctor_id"],
        availability["hospital_id"],
        start_ist,
        end_ist,
    )

    slot_id = (
        f"{availability['doctor_id']}#"
        f"{availability['hospital_id']}#"
        f"{availability['consultation_type_id']}#"
        f"{start_ist.isoformat(timespec='seconds')}"
    )

    desired_status = "BLACKOUT" if blackout else "AVAILABLE"

    item = {
        "slotId": slot_id,
        "doctorId": int(availability["doctor_id"]),
        "hospitalId": int(availability["hospital_id"]),
        "consultationTypeId": int(availability["consultation_type_id"]),
        "consultationFee": str(availability.get("consultation_fee", "0.00")),
        "capacity": int(availability.get("capacity", 1)),
        "bookedCount": 0,
        "slotTime": start_ist.isoformat(timespec="seconds"),
        "endTime": end_ist.isoformat(timespec="seconds"),
        "status": desired_status,
        "isBlackout": bool(blackout),
        "version": 1,
        "timezone": "Asia/Kolkata",
    }

    try:
        slots_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(slotId)",
        )
        return "created_blackout" if blackout else "created"

    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code != "ConditionalCheckFailedException":
            logger.exception(
                "DynamoDB put_item failed for slotId=%s table=%s",
                slot_id,
                slots_table.name,
            )
            raise

        # Existing slot: update only blackout/status metadata if it has no
        # bookings. Never reset capacity, bookedCount, or other booking data.
        try:
            slots_table.update_item(
                Key={"slotId": slot_id},
                UpdateExpression="SET #status = :status, isBlackout = :blackout",
                ConditionExpression="attribute_not_exists(bookedCount) OR bookedCount = :zero",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":status": desired_status,
                    ":blackout": bool(blackout),
                    ":zero": 0,
                },
            )
        except ClientError as update_error:
            update_code = update_error.response.get("Error", {}).get("Code")
            if update_code != "ConditionalCheckFailedException":
                logger.exception(
                    "DynamoDB update_item failed for existing slotId=%s",
                    slot_id,
                )
                raise

        return "duplicate_blackout" if blackout else "duplicate"


def materialize_day(connection, slots_table, target_date):

    # Python: Monday=0, Sunday=6.
    # Database: Sunday=0, Saturday=6.
    day_of_week = (target_date.weekday() + 1) % 7

    availability_records = get_availability(
        connection,
        day_of_week,
        target_date
    )
    logger.info(
        "Materializing date=%s day_of_week=%s availability_records=%s",
        target_date.isoformat(), day_of_week, len(availability_records)
    )

    results = {
        "created": 0,
        "duplicates": 0,
        "blackout_slots": 0,
        "availability_records": len(availability_records),
    }

    buffer_minutes = int(
        os.environ.get("SLOT_BUFFER_MINUTES", "15")
    )

    if buffer_minutes < 0:
        raise ValueError(
            "SLOT_BUFFER_MINUTES cannot be negative"
        )

    for availability in availability_records:

        start_time = mysql_time_to_time(
            availability["start_time"]
        )

        end_time = mysql_time_to_time(
            availability["end_time"]
        )

        duration_minutes = int(
            availability["duration_minutes"]
        )

        if duration_minutes <= 0:
            raise ValueError(
                "Consultation duration must be positive"
            )

        if end_time <= start_time:
            raise ValueError(
                "Availability end_time must be later than start_time"
            )

        # Availability uses IST local wall-clock time.
        window_start = datetime.combine(
            target_date,
            start_time,
            tzinfo=IST
        )

        window_end = datetime.combine(
            target_date,
            end_time,
            tzinfo=IST
        )

        current_start = window_start

        consultation_duration = timedelta(
            minutes=duration_minutes
        )

        buffer_duration = timedelta(
            minutes=buffer_minutes
        )

        while (
            current_start + consultation_duration
            <= window_end
        ):

            current_end = (
                current_start + consultation_duration
            )

            result = create_slot(
                slots_table,
                availability,
                current_start,
                current_end,
                connection
            )

            if result in {"created", "created_blackout"}:
                results["created"] += 1
                if result == "created_blackout":
                    results["blackout_slots"] += 1

            elif result in {"duplicate", "duplicate_blackout"}:
                results["duplicates"] += 1
                if result == "duplicate_blackout":
                    results["blackout_slots"] += 1

            current_start = (
                current_end + buffer_duration
            )

    logger.info("Materialized date=%s results=%s", target_date.isoformat(), results)
    return results


def lambda_handler(event, context):

    event = event or {}

    try:
        start_day, days = get_date_range(event)

    except (TypeError, ValueError) as exc:
        return {
            "statusCode": 400,
            "body": json.dumps({
                "message": str(exc)
            })
        }

    table_name = os.environ.get("SLOTS_TABLE_NAME")
    if not table_name:
        return {
            "statusCode": 500,
            "body": json.dumps({"message": "SLOTS_TABLE_NAME is not configured"})
        }

    slots_table = dynamodb.Table(table_name)
    logger.info(
        "Starting slot materialization start_date=%s days=%s table=%s",
        start_day.isoformat(), days, table_name
    )

    try:
        connection = get_db_connection()
    except Exception:
        logger.exception("Could not connect to RDS for slot materialization")
        raise

    total_results = {
        "created": 0,
        "duplicates": 0,
        "blackout_slots": 0,
        "availability_records": 0,
    }

    try:

        for offset in range(days):

            target_date = (
                start_day + timedelta(days=offset)
            )

            day_results = materialize_day(
                connection,
                slots_table,
                target_date
            )

            for key in total_results:
                total_results[key] += day_results[key]

        logger.info("Slot materialization completed: %s", total_results)
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "IST slot materialisation completed",
                "start_date": str(start_day),
                "days": days,
                "timezone": "Asia/Kolkata",
                "results": total_results
            })
        }

    finally:
        connection.close()