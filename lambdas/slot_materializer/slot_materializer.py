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
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError


logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets_client = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")

IST = ZoneInfo("Asia/Kolkata")


def get_db_connection():
    """Read DB credentials from Secrets Manager and connect to RDS."""
    logger.info("STEP 1/5: Starting RDS connection setup")

    secret_arn = os.environ["DB_SECRET_ARN"]
    db_host = os.environ["DB_HOST"]
    db_name = os.environ["DB_NAME"]

    try:
        logger.info("STEP 1A: Retrieving database credentials from Secrets Manager")
        secret_response = secrets_client.get_secret_value(SecretId=secret_arn)
        secret = json.loads(secret_response["SecretString"])
        logger.info("STEP 1B: Database credentials retrieved (values hidden)")

        connection = pymysql.connect(
            host=db_host,
            user=secret["username"],
            password=secret["password"],
            database=db_name,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
            init_command="SET time_zone = '+05:30'",
            connect_timeout=10,
            read_timeout=30,
            write_timeout=30,
        )
        logger.info("STEP 1 COMPLETE: Connected to RDS database=%s host=%s", db_name, db_host)
        return connection
    except Exception:
        logger.exception("STEP 1 FAILED: Unable to retrieve DB secret or connect to RDS")
        raise


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

    logger.info(
        "STEP 2 COMPLETE: Date range resolved start_date=%s days=%s end_date=%s timezone=Asia/Kolkata",
        start_day.isoformat(), days, (start_day + timedelta(days=days - 1)).isoformat()
    )
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


def get_availability(connection, day_of_week, target_date, doctor_id=None):

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
    params = [day_of_week, target_date, target_date]
    if doctor_id is not None:
        sql += " AND da.doctor_id = %s"
        params.append(int(doctor_id))

    logger.info(
        "STEP 3A: Querying active doctor availability date=%s db_day_of_week=%s doctor_id=%s",
        target_date.isoformat(), day_of_week, doctor_id
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()

    logger.info(
        "STEP 3B: Availability query completed date=%s records=%s",
        target_date.isoformat(), len(rows)
    )
    return rows


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
        blackout_found = cursor.fetchone() is not None

    if blackout_found:
        logger.info(
            "BLACKOUT MATCH: doctor_id=%s hospital_id=%s slot_start=%s slot_end=%s",
            doctor_id, hospital_id, start_ist.isoformat(), end_ist.isoformat()
        )
    return blackout_found


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
    logger.debug(
        "STEP 4A: Preparing slot doctor_id=%s hospital_id=%s consultation_type_id=%s slot_id=%s status=%s",
        availability["doctor_id"], availability["hospital_id"],
        availability["consultation_type_id"], slot_id, desired_status
    )

    item = {
        "slotId": slot_id,
        "doctorId": str(availability["doctor_id"]),
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
            ConditionExpression="attribute_not_exists(doctorId)",
        )
        logger.debug("STEP 4B: DynamoDB slot created slot_id=%s status=%s", slot_id, desired_status)
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

        logger.debug("STEP 4B: Slot key already exists; validating slot identity slot_id=%s", slot_id)
        existing_response = slots_table.get_item(
            Key={
                "doctorId": str(availability["doctor_id"]),
                "slotTime": start_ist.isoformat(timespec="seconds"),
            },
            ConsistentRead=True,
        )
        existing_item = existing_response.get("Item", {})
        if existing_item.get("slotId") != slot_id:
            raise RuntimeError(
                "Slots key collision: the same doctorId and slotTime are already "
                "used by a different hospital or consultation type. "
                f"doctorId={availability['doctor_id']} "
                f"slotTime={start_ist.isoformat(timespec='seconds')} "
                f"existingSlotId={existing_item.get('slotId')} requestedSlotId={slot_id}"
            )

        # Existing slot: update only blackout/status metadata if it has no
        # bookings. Never reset capacity, bookedCount, or other booking data.
        try:
            slots_table.update_item(
                Key={
                    "doctorId": str(availability["doctor_id"]),
                    "slotTime": start_ist.isoformat(timespec="seconds"),
                },
                UpdateExpression=(
                    "SET #status = :status, #is_blackout = :blackout, "
                    "#hospital_id = :hospital_id, "
                    "#consultation_type_id = :consultation_type_id, "
                    "#consultation_fee = :fee, #capacity = :capacity, "
                    "#end_time = :end_time, #timezone = :timezone"
                ),
                ConditionExpression=(
                    "slotId = :slot_id AND "
                    "(attribute_not_exists(bookedCount) OR bookedCount = :zero) "
                    "AND #status <> :doctor_cancelled "
                    "AND #status <> :legacy_cancelled "
                    "AND #status <> :cancelling"
                ),
                ExpressionAttributeNames={
                    "#status": "status",
                    "#is_blackout": "isBlackout",
                    "#hospital_id": "hospitalId",
                    "#consultation_type_id": "consultationTypeId",
                    "#consultation_fee": "consultationFee",
                    "#capacity": "capacity",
                    "#end_time": "endTime",
                    "#timezone": "timezone",
                },
                ExpressionAttributeValues={
                    ":status": desired_status,
                    ":blackout": bool(blackout),
                    ":hospital_id": int(availability["hospital_id"]),
                    ":consultation_type_id": int(availability["consultation_type_id"]),
                    ":fee": str(availability.get("consultation_fee", "0.00")),
                    ":capacity": int(availability.get("capacity", 1)),
                    ":end_time": end_ist.isoformat(timespec="seconds"),
                    ":timezone": "Asia/Kolkata",
                    ":zero": 0,
                    ":slot_id": slot_id,
                    ":doctor_cancelled": "SLOT_CANCELLED_BY_DOCTOR",
                    ":legacy_cancelled": "CANCELLED",
                    ":cancelling": "CANCELLING",
                },
            )
            logger.debug("STEP 4C: Existing unbooked slot metadata refreshed slot_id=%s", slot_id)
        except ClientError as update_error:
            update_code = update_error.response.get("Error", {}).get("Code")
            if update_code == "ConditionalCheckFailedException":
                logger.info(
                    "Existing slot was not changed because it has bookings or a doctor/time key collision slot_id=%s",
                    slot_id,
                )
            else:
                logger.exception(
                    "DynamoDB update_item failed for existing slotId=%s",
                    slot_id,
                )
                raise

        return "duplicate_blackout" if blackout else "duplicate"


def materialize_day(connection, slots_table, target_date, doctor_id=None, requested_weekdays=None):

    # Python: Monday=0, Sunday=6.
    # Database: Sunday=0, Saturday=6.
    day_of_week = (target_date.weekday() + 1) % 7
    if requested_weekdays is not None and day_of_week not in requested_weekdays:
        logger.info("Skipping date outside requested weekdays date=%s day_of_week=%s", target_date, day_of_week)
        return {"created": 0, "duplicates": 0, "blackout_slots": 0, "deactivated": 0, "availability_records": 0}

    availability_records = get_availability(
        connection,
        day_of_week,
        target_date,
        doctor_id=doctor_id
    )
    logger.info(
        "STEP 3 COMPLETE: Starting daily materialization date=%s day_of_week=%s availability_records=%s doctor_id=%s",
        target_date.isoformat(), day_of_week, len(availability_records), doctor_id
    )

    results = {
        "created": 0,
        "duplicates": 0,
        "blackout_slots": 0,
        "deactivated": 0,
        "availability_records": len(availability_records),
    }
    generated_slot_ids = set()
    generated_slot_keys = set()

    for availability_index, availability in enumerate(availability_records, start=1):
        logger.info(
            "STEP 3C: Processing availability %s/%s availability_id=%s doctor_id=%s hospital_id=%s consultation_type_id=%s",
            availability_index, len(availability_records),
            availability.get("availability_id"), availability.get("doctor_id"),
            availability.get("hospital_id"), availability.get("consultation_type_id")
        )

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

        logger.info(
            "AVAILABILITY WINDOW: doctor_id=%s date=%s start=%s end=%s duration_minutes=%s capacity=%s",
            availability["doctor_id"], target_date.isoformat(),
            window_start.isoformat(), window_end.isoformat(), duration_minutes,
            availability.get("capacity", 1)
        )

        current_start = window_start

        # Keep the configured gap between appointments. The consultation end
        # remains the actual appointment end; the next slot starts after buffer.
        consultation_duration = timedelta(minutes=duration_minutes)
        buffer_minutes = max(0, int(os.environ.get("SLOT_BUFFER_MINUTES", "15")))
        slot_interval = consultation_duration + timedelta(minutes=buffer_minutes)

        while current_start + consultation_duration <= window_end:

            current_end = (
                current_start + consultation_duration
            )

            slot_id = (
                f"{availability['doctor_id']}#"
                f"{availability['hospital_id']}#"
                f"{availability['consultation_type_id']}#"
                f"{current_start.isoformat(timespec='seconds')}"
            )
            generated_slot_ids.add(slot_id)
            generated_slot_keys.add((
                str(availability["doctor_id"]),
                current_start.isoformat(timespec="seconds"),
            ))
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

            current_start += slot_interval

    logger.info(
        "SLOT GENERATION COMPLETE: date=%s doctor_id=%s generated_slot_count=%s created=%s duplicates=%s blackout_slots=%s",
        target_date.isoformat(), doctor_id, len(generated_slot_ids),
        results["created"], results["duplicates"], results["blackout_slots"]
    )

    # For targeted doctor refreshes, deactivate obsolete unbooked slots for this date.
    # Existing booked slots are never changed or deleted.
    if doctor_id is not None:
        day_start = target_date.isoformat() + "T00:00:00"
        day_end = target_date.isoformat() + "T23:59:59"
        key_condition = (
            Key("doctorId").eq(str(doctor_id))
            & Key("slotTime").between(day_start, day_end)
        )
        logger.info(
            "STEP 4D: Querying existing slots for targeted refresh doctor_id=%s date=%s",
            doctor_id, target_date.isoformat()
        )
        page = slots_table.query(KeyConditionExpression=key_condition)
        existing_items = list(page.get("Items", []))
        while page.get("LastEvaluatedKey"):
            page = slots_table.query(
                KeyConditionExpression=key_condition,
                ExclusiveStartKey=page["LastEvaluatedKey"],
            )
            existing_items.extend(page.get("Items", []))

        logger.info(
            "STEP 4E: Existing slot scan completed doctor_id=%s date=%s existing_slots=%s newly_generated_slots=%s",
            doctor_id, target_date.isoformat(), len(existing_items), len(generated_slot_ids)
        )
        for existing in existing_items:
            existing_key = (
                str(existing.get("doctorId")),
                str(existing.get("slotTime")),
            )
            if existing_key in generated_slot_keys:
                continue
            try:
                slots_table.update_item(
                    Key={
                        "doctorId": str(existing["doctorId"]),
                        "slotTime": str(existing["slotTime"]),
                    },
                    UpdateExpression="SET #status = :inactive, #is_blackout = :false",
                    ConditionExpression=(
                        "(attribute_not_exists(bookedCount) OR bookedCount = :zero) "
                        "AND #status <> :doctor_cancelled "
                        "AND #status <> :legacy_cancelled "
                        "AND #status <> :cancelling"
                    ),
                    ExpressionAttributeNames={
                        "#status": "status",
                        "#is_blackout": "isBlackout",
                    },
                    ExpressionAttributeValues={
                        ":inactive": "INACTIVE",
                        ":false": False,
                        ":zero": 0,
                        ":doctor_cancelled": "SLOT_CANCELLED_BY_DOCTOR",
                        ":legacy_cancelled": "CANCELLED",
                        ":cancelling": "CANCELLING",
                    },
                )
                results["deactivated"] += 1
                logger.info("Obsolete unbooked slot deactivated slot_id=%s", existing["slotId"])
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    logger.info("Obsolete slot retained because it has bookings slot_id=%s", existing["slotId"])
                else:
                    logger.exception("Failed to deactivate obsolete slot slot_id=%s", existing.get("slotId"))
                    raise

    logger.info("STEP 4 COMPLETE: Daily materialization finished date=%s results=%s", target_date.isoformat(), results)
    return results


def lambda_handler(event, context):
    """Generate slots for a date range, optionally for one doctor only."""
    event = event or {}
    request_id = getattr(context, "aws_request_id", "unknown")
    logger.info("========== SLOT MATERIALIZER START request_id=%s ==========", request_id)
    logger.info(
        "Incoming event metadata start_date=%s days=%s doctor_id=%s",
        event.get("start_date"), event.get("days"), event.get("doctor_id")
    )

    try:
        start_day, days = get_date_range(event)

    except (TypeError, ValueError) as exc:
        return {
            "statusCode": 400,
            "body": json.dumps({
                "message": str(exc)
            })
        }

    logger.info("STEP 2 COMPLETE: Input validation passed")

    table_name = os.environ.get("SLOTS_TABLE_NAME")
    if not table_name:
        return {
            "statusCode": 500,
            "body": json.dumps({"message": "SLOTS_TABLE_NAME is not configured"})
        }

    logger.info("STEP 2B: DynamoDB slots table configured table_name=%s", table_name)
    slots_table = dynamodb.Table(table_name)
    doctor_id = event.get("doctor_id")
    requested_weekdays = event.get("day_of_week")
    if requested_weekdays is not None:
        try:
            if not isinstance(requested_weekdays, list):
                requested_weekdays = [requested_weekdays]
            requested_weekdays = sorted({int(day) for day in requested_weekdays})
            if not requested_weekdays or any(day < 0 or day > 6 for day in requested_weekdays):
                raise ValueError()
        except (TypeError, ValueError):
            return {"statusCode": 400, "body": json.dumps({"message": "day_of_week must contain values from 0 (Sunday) to 6 (Saturday)"})}
    if doctor_id is not None:
        try:
            doctor_id = int(doctor_id)
            if doctor_id <= 0:
                raise ValueError()
        except (TypeError, ValueError):
            return {"statusCode": 400, "body": json.dumps({"message": "doctor_id must be a positive integer"})}

    logger.info(
        "Starting slot materialization start_date=%s days=%s doctor_id=%s table=%s",
        start_day.isoformat(), days, doctor_id, table_name
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
        "deactivated": 0,
        "availability_records": 0,
    }

    try:

        for offset in range(days):
            target_date = (
                start_day + timedelta(days=offset)
            )

            logger.info(
                "PROCESSING DAY %s/%s date=%s doctor_id=%s",
                offset + 1, days, target_date.isoformat(), doctor_id
            )
            day_results = materialize_day(
                connection,
                slots_table,
                target_date,
                doctor_id=doctor_id,
                requested_weekdays=requested_weekdays
            )

            for key in total_results:
                total_results[key] += day_results[key]

            logger.info(
                "DAY COMPLETE %s/%s date=%s daily_results=%s running_totals=%s",
                offset + 1, days, target_date.isoformat(), day_results, total_results
            )

        logger.info("STEP 5 COMPLETE: Slot materialization completed request_id=%s totals=%s", request_id, total_results)
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "IST slot materialisation completed",
                "start_date": str(start_day),
                "days": days,
                "doctor_id": doctor_id,
                "timezone": "Asia/Kolkata",
                "results": total_results
            })
        }

    except Exception:
        logger.exception(
            "SLOT MATERIALIZER FAILED request_id=%s start_date=%s days=%s doctor_id=%s",
            request_id, start_day.isoformat(), days, doctor_id
        )
        raise
    finally:
        try:
            connection.close()
            logger.info("RDS connection closed request_id=%s", request_id)
        except Exception:
            logger.exception("Failed to close RDS connection request_id=%s", request_id)
        logger.info("========== SLOT MATERIALIZER END request_id=%s ==========", request_id)
