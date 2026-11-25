"""MediSlot appointment booking, cancellation and refund API.

Routes:
  POST /appointments                       Patient booking
  GET  /appointments                       Caller-scoped appointment list
  GET  /appointments/{appointment_id}      Caller-scoped appointment detail
  POST /appointments/{appointment_id}/cancel
  POST /appointments/doctor-cancel-slot    Doctor cancels one generated slot
  GET  /appointments/refunds                Patient refund ledger

Slot identity is the DynamoDB composite key: doctorId + slotTime.
All appointment wall-clock values are stored in IST (Asia/Kolkata).
"""

import base64
import hashlib
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import boto3
import pymysql
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
IST = timezone(timedelta(hours=5, minutes=30))
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
MAX_RETRIES = int(os.environ.get("BOOKING_MAX_RETRIES", "5"))
MAX_ACTIVE_APPOINTMENTS = int(os.environ.get("MAX_ACTIVE_APPOINTMENTS_PER_PATIENT", "5"))
MAX_UPCOMING_APPOINTMENTS_PER_DOCTOR = int(
    os.environ.get("MAX_UPCOMING_APPOINTMENTS_PER_DOCTOR", "3")
)
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60

secrets_client = boto3.client("secretsmanager", region_name=REGION)
ssm_client = boto3.client("ssm", region_name=REGION)
events_client = boto3.client("events", region_name=REGION)
cloudwatch_client = boto3.client("cloudwatch", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)

slots_table = dynamodb.Table(os.environ.get("SLOTS_TABLE_NAME", "medislot-dev-slots"))
idempotency_table = dynamodb.Table(
    os.environ.get("IDEMPOTENCY_TABLE_NAME", "medislot-dev-idempotency")
)


class ApiError(Exception):
    def __init__(self, status_code, message, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.details = details


def response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(payload, default=str),
    }


def connect_db():
    secret_arn = os.environ["DB_SECRET_ARN"]
    secret_response = secrets_client.get_secret_value(SecretId=secret_arn)
    secret = json.loads(secret_response["SecretString"])
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        user=secret.get("username") or secret.get("user"),
        password=secret["password"],
        database=os.environ.get("DB_NAME", "medislot"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=5,
        read_timeout=15,
        write_timeout=15,
        init_command="SET time_zone = '+05:30'",
    )


def parse_body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        body = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise ApiError(400, "Request body must contain valid JSON")
    if not isinstance(body, dict):
        raise ApiError(400, "Request body must be a JSON object")
    return body


def get_auth(event):
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    if isinstance(authorizer.get("lambda"), dict):
        authorizer = {**authorizer, **authorizer["lambda"]}
    claims = authorizer.get("claims")
    if isinstance(claims, dict):
        authorizer = {**authorizer, **claims}

    identity = authorizer.get("profileId") or authorizer.get("principalId") or authorizer.get("sub")
    role = str(authorizer.get("role") or authorizer.get("custom:role") or "").strip().lower()
    try:
        identity = int(identity)
    except (TypeError, ValueError):
        identity = None
    if not identity or identity <= 0:
        return None, role
    return identity, role


def get_path(event):
    path = event.get("path") or event.get("rawPath") or "/"
    stage = (event.get("requestContext") or {}).get("stage")
    if stage and path.startswith(f"/{stage}/"):
        path = path[len(stage) + 1:]
    return path.rstrip("/") or "/"


def normalise_slot_time(value):
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "slot_time is required in ISO-8601 format")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(400, "slot_time must be a valid ISO-8601 date and time")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    parsed = parsed.astimezone(IST).replace(microsecond=0)
    return parsed.isoformat(timespec="seconds"), parsed.replace(tzinfo=None)


def emit_metric(name, value=1, dimensions=None):
    try:
        metric_dimensions = [{"Name": "Environment", "Value": ENVIRONMENT}]
        for key, item in (dimensions or {}).items():
            metric_dimensions.append({"Name": key, "Value": str(item)})
        cloudwatch_client.put_metric_data(
            Namespace="MediSlot/Bookings",
            MetricData=[{
                "MetricName": name,
                "Dimensions": metric_dimensions,
                "Value": float(value),
                "Unit": "Count",
            }],
        )
    except Exception:
        # Metrics must never break a booking or cancellation.
        logger.exception("Could not publish CloudWatch metric %s", name)


def publish_event(detail_type, detail):
    entry = {
        "EventBusName": "default",
        "Source": "medislot.appointments",
        "DetailType": detail_type,
        "Time": datetime.now(timezone.utc),
        "Detail": json.dumps(detail, default=str),
    }
    for attempt in range(3):
        try:
            result = events_client.put_events(Entries=[entry])
            if not result.get("FailedEntryCount", 0):
                return True
            logger.warning(
                "EventBridge rejected %s attempt=%s entries=%s",
                detail_type, attempt + 1, result.get("Entries"),
            )
        except Exception:
            logger.exception(
                "Could not publish EventBridge event %s attempt=%s",
                detail_type, attempt + 1,
            )
        if attempt < 2:
            time.sleep(0.1 * (attempt + 1))
    logger.error("EventBridge publish failed after retries detail_type=%s", detail_type)
    return False


def get_slot(doctor_id, slot_time):
    result = slots_table.get_item(
        Key={"doctorId": str(doctor_id), "slotTime": slot_time},
        ConsistentRead=True,
    )
    slot = result.get("Item")
    if not slot:
        raise ApiError(404, "Appointment slot was not found")
    return slot


def reserve_slot(doctor_id, slot_time):
    """Reserve exactly one seat using optimistic concurrency and bounded retry."""
    retries = 0
    for attempt in range(MAX_RETRIES):
        slot = get_slot(doctor_id, slot_time)
        if str(slot.get("status", "")).upper() != "AVAILABLE" or slot.get("isBlackout") is True:
            raise ApiError(409, "This appointment slot is not available")

        capacity = int(slot.get("capacity", 1))
        booked = booked_count_value(slot.get("bookedCount", 0))
        revision = int(slot.get("seatRevision", 0) or 0)
        if booked >= capacity:
            emit_metric("BookingConflictRetries", retries)
            raise ApiError(409, "This appointment slot is full")

        try:
            slots_table.update_item(
                Key={"doctorId": str(doctor_id), "slotTime": slot_time},
                UpdateExpression="SET #booked = :next_booked, #revision = :next_revision",
                ConditionExpression=(
                    "(attribute_not_exists(#revision) OR #revision = :revision) "
                    "AND #status = :available AND #blackout = :false"
                ),
                ExpressionAttributeNames={
                    "#booked": "bookedCount",
                    "#revision": "seatRevision",
                    "#status": "status",
                    "#blackout": "isBlackout",
                },
                ExpressionAttributeValues={
                    ":next_booked": booked + 1,
                    ":revision": revision,
                    ":next_revision": revision + 1,
                    ":available": "AVAILABLE",
                    ":false": False,
                },
            )
            emit_metric("BookingConflictRetries", retries)
            return slot, retries
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            retries += 1
            logger.info(
                "Booking slot contention doctor_id=%s slot_time=%s retry=%s",
                doctor_id, slot_time, retries,
            )
            if attempt + 1 < MAX_RETRIES:
                time.sleep(min(0.025 * (2 ** attempt) + random.uniform(0, 0.025), 0.25))

    emit_metric("BookingConflictRetries", retries)
    latest = get_slot(doctor_id, slot_time)
    if booked_count_value(latest.get("bookedCount", 0)) >= int(latest.get("capacity", 1)):
        raise ApiError(409, "This appointment slot is full")
    raise ApiError(409, "Booking could not be completed due to concurrent requests. Please retry.")


def release_reserved_seat(doctor_id, slot_time, slot_id):
    """Compensate a failed RDS insert using the same numeric CAS as booking."""
    slot = get_slot(doctor_id, slot_time)
    booked = booked_count_value(slot.get("bookedCount", 0))
    revision = int(slot.get("seatRevision", 0) or 0)
    if booked <= 0:
        return
    slots_table.update_item(
        Key={"doctorId": str(doctor_id), "slotTime": slot_time},
        UpdateExpression="SET #booked = :next_booked, #revision = :next_revision",
        ConditionExpression=(
            "slotId = :slot_id AND "
            "(attribute_not_exists(#revision) OR #revision = :revision)"
        ),
        ExpressionAttributeNames={"#booked": "bookedCount", "#revision": "seatRevision"},
        ExpressionAttributeValues={
            ":next_booked": booked - 1,
            ":next_revision": revision + 1,
            ":revision": revision,
            ":slot_id": slot_id,
        },
    )


def booked_count_value(value):
    """Read current numeric counters and legacy map/list counters safely."""
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("bookedCount must not be a boolean")
    if isinstance(value, (int, float, Decimal)):
        return max(0, int(value))
    if isinstance(value, dict):
        # Support common legacy map shapes before falling back to a map of
        # appointment IDs, where each entry represents one occupied seat.
        for key in ("count", "booked", "bookedCount", "booked_count", "total"):
            if key in value:
                return booked_count_value(value[key])
        for key in ("appointments", "bookings", "appointmentIds", "bookingIds"):
            if key in value:
                return booked_count_value(value[key])
        return len(value)
    if isinstance(value, (list, tuple, set)):
        return len(value)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        raise ValueError("Unsupported bookedCount value")


def release_cancelled_seat_once(appointment_id, doctor_id, slot_time, slot_id, cancellation_type="PATIENT"):
    """Release one cancelled appointment seat exactly once.

    Always replace bookedCount with a numeric value. This supports both current
    numeric counters and legacy DynamoDB Map/List counters. In particular, do
    not use a numeric comparison (bookedCount > 0): DynamoDB rejects that
    expression when an older item stores bookedCount as a Map.

    The slot update and idempotency marker are committed in one transaction.
    seatRevision is a dedicated numeric compare-and-swap token shared by booking
    and cancellation. It avoids arithmetic on legacy version Map attributes.
    """
    marker = f"SEAT_RELEASE#{appointment_id}"
    client = dynamodb.meta.client

    for attempt in range(5):
        marker_result = idempotency_table.get_item(
            Key={"idemKey": marker}, ConsistentRead=True
        )
        if marker_result.get("Item"):
            return False

        slot_result = slots_table.get_item(
            Key={"doctorId": str(doctor_id), "slotTime": slot_time},
            ConsistentRead=True,
        )
        slot = slot_result.get("Item")
        if not slot:
            raise ApiError(409, "The appointment slot no longer exists")

        has_counter = "bookedCount" in slot
        raw_booked = slot.get("bookedCount", 0)
        booked = booked_count_value(raw_booked)
        revision = int(slot.get("seatRevision", 0) or 0)
        current_status = str(slot.get("status", "AVAILABLE")).upper()
        # Some older slot records persist a full-state label. Once a seat is
        # released, make those slots bookable again. Never reopen blackout,
        # inactive, or doctor-cancelled slots.
        reopenable_statuses = {"AVAILABLE", "FULL", "FULLY_BOOKED", "BOOKED"}
        if str(cancellation_type).upper() == "DOCTOR":
            next_status = "SLOT_CANCELLED_BY_DOCTOR"
        else:
            next_status = "AVAILABLE" if current_status in reopenable_statuses else current_status

        marker_put = {
            "Put": {
                "TableName": idempotency_table.name,
                "Item": {
                    "idemKey": {"S": marker},
                    "status": {"S": "COMPLETE"},
                },
                "ConditionExpression": "attribute_not_exists(idemKey)",
            }
        }

        # Even when the numeric count is already zero, write the normalised
        # counter/status with the idempotency marker. This repairs legacy
        # FULL/FULLY_BOOKED labels without ever decrementing below zero.
        new_booked = max(0, booked - 1)
        released = booked > 0

        # Detect whether the stored bookedCount is a legacy DynamoDB Map/List.
        # TransactWriteItems rejects type changes (Map -> Number) with a
        # ValidationError cancellation reason. For those items we must REMOVE
        # the old Map attribute and SET the new numeric value in one expression.
        # Regular update_item allows type changes but transactional writes do not.
        is_legacy_map = isinstance(raw_booked, dict) or isinstance(raw_booked, list)

        # CAS uses a dedicated numeric attribute. Legacy `version` and
        # `bookedCount` may be Maps; neither participates in arithmetic.
        revision_condition = (
            "attribute_not_exists(#revision)" if "seatRevision" not in slot
            else "#revision = :revision"
        )
        condition_values = {
            ":new_count": {"N": str(new_booked)},
            ":next_revision": {"N": str(revision + 1)},
        }
        if "seatRevision" in slot:
            condition_values[":revision"] = {"N": str(revision)}

        if is_legacy_map:
            # Convert the legacy Map/List counter directly to a Number in the
            # same transactional Update as the idempotency marker. DynamoDB
            # SET replaces the existing attribute value, including its type.
            # Keeping the counter write in this transaction prevents a crash
            # after marking the release COMPLETE from leaving the slot stale.
            update_expr = (
                "SET #booked = :new_count, #status = :new_status, "
                "#revision = :next_revision"
            )
            cond_expr = f"attribute_exists(#booked) AND {revision_condition}"
        else:
            counter_condition = (
                "attribute_exists(#booked)" if has_counter
                else "attribute_not_exists(#booked)"
            )
            update_expr = (
                "SET #booked = :new_count, #status = :new_status, "
                "#revision = :next_revision"
            )
            cond_expr = f"{counter_condition} AND {revision_condition}"

        # NOTE: slotId is intentionally NOT included in the condition.
        # The DynamoDB primary key (doctorId + slotTime) uniquely identifies
        # the slot. Including slotId caused TransactionCanceledException when
        # the value stored in RDS appointments.slot_id drifted from the
        # current DynamoDB slotId (e.g. after a slot was regenerated), making
        # all 5 retries fail and preventing the seat from ever being released.
        # seatRevision provides the compare-and-swap safety needed to prevent
        # lost updates without requiring slotId equality.
        update_item_values = {
            **condition_values,
            ":new_status": {"S": next_status},
        }
        # Build the transaction key from the canonical key values returned by
        # the strongly-consistent read of this exact DynamoDB item. This avoids
        # using appointment/RDS values (which may be formatted differently)
        # when constructing the low-level TransactWriteItems key.
        canonical_doctor_id = slot.get("doctorId")
        canonical_slot_time = slot.get("slotTime")
        if canonical_doctor_id is None or canonical_slot_time is None:
            raise ApiError(409, "The appointment slot has an invalid DynamoDB key")

        transaction_key = {
            "doctorId": {"S": str(canonical_doctor_id)},
            "slotTime": {"S": str(canonical_slot_time)},
        }
        update = {
            "Update": {
                "TableName": slots_table.name,
                "Key": transaction_key,
                "UpdateExpression": update_expr,
                "ConditionExpression": cond_expr,
                "ExpressionAttributeNames": {
                    "#booked": "bookedCount",
                    "#status": "status",
                    "#revision": "seatRevision",
                },
                "ExpressionAttributeValues": update_item_values,
            }
        }

        try:
            logger.info(
                "Submitting cancellation seat-release transaction "
                "appointment_id=%s slots_table=%s key_attributes=%s "
                "doctor_id_type=%s slot_time_type=%s",
                appointment_id,
                slots_table.name,
                sorted(transaction_key.keys()),
                type(canonical_doctor_id).__name__,
                type(canonical_slot_time).__name__,
            )
            client.transact_write_items(TransactItems=[update, marker_put])
            logger.info(
                "Cancelled appointment seat released appointment_id=%s doctor_id=%s "
                "booked_count=%s->%s status=%s->%s released=%s counter_type=%s",
                appointment_id, doctor_id, booked, new_booked,
                current_status, next_status, released, type(raw_booked).__name__,
            )
            return released
        except ClientError as error:
            error_response = error.response or {}
            error_code = (error_response.get("Error") or {}).get("Code")
            cancellation_reasons = error_response.get("CancellationReasons") or []

            # Keep the complete DynamoDB cancellation reasons in CloudWatch.
            # The top-level TransactionCanceledException alone does not tell
            # us whether this was a CAS conflict, a duplicate marker, or an
            # invalid transaction request.
            logger.error(
                "Seat release DynamoDB transaction failed "
                "appointment_id=%s error_code=%s error_message=%s "
                "cancellation_reasons=%s",
                appointment_id,
                error_code,
                (error_response.get("Error") or {}).get("Message"),
                json.dumps(cancellation_reasons, default=str),
            )

            if error_code == "TransactionCanceledException":
                marker_result = idempotency_table.get_item(
                    Key={"idemKey": marker}, ConsistentRead=True
                )
                if marker_result.get("Item"):
                    return False

                reason_codes = {
                    reason.get("Code")
                    for reason in cancellation_reasons
                    if isinstance(reason, dict)
                }

                # ValidationError is a malformed/invalid transaction
                # operation, not a concurrency conflict. Retrying the same
                # request cannot repair it; surface it with its full reason.
                if "ValidationError" in reason_codes:
                    raise

                # Retry only conditions that can legitimately change after
                # re-reading the slot. Do not retry validation or other
                # permanent transaction failures.
                retryable_codes = {
                    "ConditionalCheckFailed",
                    "TransactionConflict",
                    "ProvisionedThroughputExceeded",
                    "ThrottlingError",
                }
                if reason_codes.intersection(retryable_codes) and attempt < 4:
                    logger.info(
                        "Retrying seat release after retryable DynamoDB "
                        "transaction conflict appointment_id=%s attempt=%s "
                        "reasons=%s",
                        appointment_id,
                        attempt + 1,
                        sorted(reason_codes),
                    )
                    continue
            raise

    raise ApiError(409, "Slot availability changed during cancellation. Please refresh and retry.")


def appointment_slot_time(appointment_row):
    """Return the DynamoDB slotTime key for a persisted appointment row.

    The slotId stored in RDS encodes the slot's composite identity as
    ``doctorId#hospitalId#consultationTypeId#slotTime`` (e.g.
    ``42#7#3#2026-12-01T09:00:00+05:30``).  Extracting the last segment gives
    the exact string stored as the DynamoDB sort key, avoiding any risk of
    timezone rounding when reconstructing it from start_at_ist.

    For legacy rows whose slot_id is an opaque identifier that does not embed
    the time, the function falls back to deriving the key from start_at_ist
    (the MySQL DATETIME column, which pymysql returns without tzinfo).
    """
    slot_id = str(appointment_row.get("slot_id") or "")
    if "#" in slot_id:
        candidate = slot_id.rsplit("#", 1)[-1]
        try:
            # Validate the candidate is a parseable ISO-8601 datetime.
            datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            return candidate
        except ValueError:
            pass
    # Fallback: reconstruct from the MySQL DATETIME (naive → attach IST).
    start = appointment_row["start_at_ist"]
    if start.tzinfo is None:
        start = start.replace(tzinfo=IST)
    slot_time, _ = normalise_slot_time(start.isoformat(timespec="seconds"))
    return slot_time


def request_hash(patient_id, doctor_id, slot_time):
    value = f"{patient_id}|{doctor_id}|{slot_time}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def start_idempotency(idem_key, patient_id, body_hash, request_id):
    expires_at = int(time.time()) + IDEMPOTENCY_TTL_SECONDS
    try:
        idempotency_table.put_item(
            Item={
                "idemKey": idem_key,
                "patientId": str(patient_id),
                "requestHash": body_hash,
                "status": "PROCESSING",
                "requestId": request_id,
                "expiresAt": expires_at,
            },
            ConditionExpression="attribute_not_exists(idemKey)",
        )
        return None
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise

    existing = idempotency_table.get_item(
        Key={"idemKey": idem_key}, ConsistentRead=True
    ).get("Item")
    if not existing:
        raise ApiError(409, "The booking request is already being processed. Please retry.")

    if str(existing.get("patientId")) != str(patient_id) or existing.get("requestHash") != body_hash:
        raise ApiError(409, "This Idempotency-Key was already used for a different request")

    if existing.get("status") == "COMPLETE" and existing.get("response"):
        return json.loads(existing["response"])

    # Recover safely if the Lambda committed the RDS appointment but stopped
    # before it could mark the DynamoDB idempotency record COMPLETE.
    if existing.get("status") == "PROCESSING":
        connection = connect_db()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT appointment_id, patient_id, doctor_id, hospital_id,
                              consultation_type_id, slot_id, start_at_ist, end_at_ist,
                              consultation_fee, status
                       FROM appointments WHERE idempotency_key = %s LIMIT 1""",
                    (idem_key,),
                )
                row = cursor.fetchone()
        finally:
            connection.close()
        if row and int(row["patient_id"]) == int(patient_id):
            recovered = {
                "appointment_id": int(row["appointment_id"]),
                "patient_id": int(row["patient_id"]),
                "doctor_id": int(row["doctor_id"]),
                "hospital_id": int(row["hospital_id"]),
                "consultation_type_id": int(row["consultation_type_id"]),
                "slot_id": str(row["slot_id"]),
                "start_at_ist": row["start_at_ist"].isoformat(),
                "end_at_ist": row["end_at_ist"].isoformat(),
                "consultation_fee": str(row["consultation_fee"]),
                "status": row["status"],
            }
            complete_idempotency(idem_key, recovered)
            return recovered

    raise ApiError(409, "The booking request is still being processed. Please retry shortly.")


def complete_idempotency(idem_key, payload):
    idempotency_table.update_item(
        Key={"idemKey": idem_key},
        UpdateExpression="SET #status = :complete, #response = :response",
        ExpressionAttributeNames={"#status": "status", "#response": "response"},
        ExpressionAttributeValues={
            ":complete": "COMPLETE",
            ":response": json.dumps(payload, default=str),
        },
    )


def remove_idempotency(idem_key, request_id):
    try:
        idempotency_table.delete_item(
            Key={"idemKey": idem_key},
            ConditionExpression="requestId = :request_id",
            ExpressionAttributeValues={":request_id": request_id},
        )
    except ClientError:
        logger.exception("Could not remove failed idempotency record key=%s", idem_key)


def book_appointment(event, patient_id, request_id):
    body = parse_body(event)
    try:
        doctor_id = int(body.get("doctor_id"))
    except (TypeError, ValueError):
        raise ApiError(400, "doctor_id must be a positive integer")
    if doctor_id <= 0:
        raise ApiError(400, "doctor_id must be a positive integer")

    slot_time, mysql_start = normalise_slot_time(body.get("slot_time"))
    idem_key = (
        (event.get("headers") or {}).get("Idempotency-Key")
        or (event.get("headers") or {}).get("idempotency-key")
        or body.get("idempotency_key")
    )
    if not isinstance(idem_key, str) or not idem_key.strip() or len(idem_key) > 128:
        raise ApiError(400, "Provide an Idempotency-Key header (maximum 128 characters)")
    idem_key = idem_key.strip()
    body_hash = request_hash(patient_id, doctor_id, slot_time)
    replay = start_idempotency(idem_key, patient_id, body_hash, request_id)
    if replay is not None:
        return response(200, {
            "message": "Original appointment returned for idempotent replay",
            "appointment": replay,
            "idempotent_replay": True,
        })

    reserved = False
    rds_committed = False
    slot_id = None
    try:
        # Check the current slot only after idempotency replay detection. A
        # replay must still return its original appointment even if the slot
        # has since become full or was later deactivated.
        slot = get_slot(doctor_id, slot_time)
        if slot.get("slotId") and body.get("slot_id") and str(body["slot_id"]) != str(slot["slotId"]):
            raise ApiError(400, "slot_id does not match the selected doctor and appointment time")
        if slot.get("endTime"):
            _, mysql_end = normalise_slot_time(slot["endTime"])
        else:
            raise ApiError(409, "The selected slot has no end time")
        if mysql_start <= datetime.now(IST).replace(tzinfo=None):
            raise ApiError(409, "Appointments in the past cannot be booked")
        if mysql_end <= mysql_start:
            raise ApiError(409, "The selected slot has an invalid end time")

        slot_id = str(slot.get("slotId") or "")
        if not slot_id:
            raise ApiError(409, "The selected slot is missing its slot identifier")
        slot, retry_count = reserve_slot(doctor_id, slot_time)
        reserved = True
        connection = connect_db()
        try:
            with connection.cursor() as cursor:
                # Serialize bookings for one patient so the patient limit is exact.
                cursor.execute(
                    "SELECT patient_id, account_status FROM patients WHERE patient_id = %s FOR UPDATE",
                    (patient_id,),
                )
                patient = cursor.fetchone()
                if not patient or str(patient.get("account_status", "ACTIVE")).upper() != "ACTIVE":
                    raise ApiError(403, "An active patient account is required to book appointments")

                cursor.execute(
                    """SELECT d.profile_status,
                              h.is_active AS hospital_active,
                              ct.is_active AS consultation_active
                       FROM doctors d
                       JOIN hospitals h ON h.hospital_id = %s
                       JOIN consultation_types ct ON ct.consultation_type_id = %s
                       WHERE d.doctor_id = %s
                         AND (ct.doctor_id IS NULL OR ct.doctor_id = d.doctor_id)
                       LIMIT 1""",
                    (int(slot["hospitalId"]), int(slot["consultationTypeId"]), doctor_id),
                )
                service = cursor.fetchone()
                if (
                    not service
                    or str(service["profile_status"]).upper() != "ACTIVE"
                    or not service["hospital_active"]
                    or not service["consultation_active"]
                ):
                    raise ApiError(409, "The doctor, hospital or consultation type is not active")

                cursor.execute(
                    """SELECT COUNT(*) AS active_count
                       FROM appointments
                       WHERE patient_id = %s
                         AND status = 'BOOKED'
                         AND start_at_ist >= NOW()""",
                    (patient_id,),
                )
                active_count = int(cursor.fetchone()["active_count"])
                if active_count >= MAX_ACTIVE_APPOINTMENTS:
                    raise ApiError(409, "You have reached the maximum number of active appointments")

                cursor.execute(
                    """SELECT COUNT(*) AS doctor_active_count
                       FROM appointments
                       WHERE patient_id = %s
                         AND doctor_id = %s
                         AND status = 'BOOKED'
                         AND start_at_ist >= NOW()""",
                    (patient_id, doctor_id),
                )
                doctor_active_count = int(cursor.fetchone()["doctor_active_count"])
                if doctor_active_count >= MAX_UPCOMING_APPOINTMENTS_PER_DOCTOR:
                    raise ApiError(
                        409,
                        "You can have at most 3 upcoming appointments with the same doctor",
                    )

                cursor.execute(
                    """SELECT appointment_id FROM appointments
                       WHERE patient_id = %s AND slot_id = %s AND status = 'BOOKED'
                       LIMIT 1""",
                    (patient_id, slot_id),
                )
                if cursor.fetchone():
                    raise ApiError(409, "You already have an active booking for this slot")

                # RDS is the source of truth for consultation pricing.
                # DynamoDB slot fee is a cached value and may be stale.
                cursor.execute(
                    """SELECT consultation_fee
                       FROM consultation_types
                       WHERE consultation_type_id = %s
                         AND (doctor_id IS NULL OR doctor_id = %s)
                         AND is_active = TRUE
                       FOR UPDATE""",
                    (int(slot["consultationTypeId"]), doctor_id),
                )
                consultation_type = cursor.fetchone()
                if not consultation_type:
                    raise ApiError(409, "The selected consultation type is no longer available")

                fee = Decimal(str(consultation_type["consultation_fee"] or "0.00")).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                cursor.execute(
                    """INSERT INTO appointments
                       (patient_id, doctor_id, hospital_id, consultation_type_id,
                        slot_id, start_at_ist, end_at_ist, timezone, status,
                        consultation_fee, idempotency_key)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, 'Asia/Kolkata',
                               'BOOKED', %s, %s)""",
                    (
                        patient_id, doctor_id, int(slot["hospitalId"]),
                        int(slot["consultationTypeId"]), slot_id,
                        mysql_start, mysql_end, str(fee), idem_key,
                    ),
                )
                appointment_id = int(cursor.lastrowid)
            connection.commit()
            rds_committed = True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        appointment = {
            "appointment_id": appointment_id,
            "patient_id": patient_id,
            "doctor_id": doctor_id,
            "hospital_id": int(slot["hospitalId"]),
            "consultation_type_id": int(slot["consultationTypeId"]),
            "slot_id": slot_id,
            "start_at_ist": mysql_start.isoformat(),
            "end_at_ist": mysql_end.isoformat(),
            "consultation_fee": str(fee),
            "status": "BOOKED",
        }
        try:
            complete_idempotency(idem_key, appointment)
        except Exception:
            # RDS is the system of record. A replay can recover this appointment
            # from appointments.idempotency_key if this status write fails.
            logger.exception("Could not mark idempotency record COMPLETE key=%s", idem_key)
        publish_event("AppointmentBooked", appointment)
        logger.info(
            "APPOINTMENT BOOKED appointment_id=%s patient_id=%s doctor_id=%s slot_id=%s conflict_retries=%s",
            appointment_id, patient_id, doctor_id, slot_id, retry_count,
        )
        return response(201, {
            "message": "Appointment booked successfully",
            "appointment": appointment,
            "idempotent_replay": False,
        })
    except Exception:
        if reserved and not rds_committed:
            try:
                release_reserved_seat(doctor_id, slot_time, slot_id)
            except Exception:
                logger.exception("CRITICAL: failed to compensate slot reservation slot_id=%s", slot_id)
        if not rds_committed:
            remove_idempotency(idem_key, request_id)
        raise


def get_refund_tiers():
    parameter_name = os.environ.get(
        "REFUND_TIERS_PARAMETER",
        f"/medislot/{ENVIRONMENT}/refund/tiers",
    )
    result = ssm_client.get_parameter(Name=parameter_name)
    config = json.loads(result["Parameter"]["Value"])
    tiers = config.get("tiers") if isinstance(config, dict) else None
    if not isinstance(tiers, list) or not tiers:
        raise RuntimeError("Refund tier configuration must contain a non-empty tiers array")

    normalised = []
    for tier in tiers:
        name = str(tier.get("name", "")).strip().upper()
        try:
            threshold = Decimal(str(tier["minimum_hours_before"]))
            percentage = Decimal(str(tier["refund_percentage"]))
        except (KeyError, InvalidOperation, TypeError):
            raise RuntimeError("Each refund tier needs minimum_hours_before and refund_percentage")
        if not name or threshold < 0 or percentage < 0 or percentage > 100:
            raise RuntimeError("Refund tier configuration contains an invalid tier")
        normalised.append({
            "name": name,
            "minimum_hours_before": threshold,
            "refund_percentage": percentage,
        })
    normalised.sort(key=lambda item: item["minimum_hours_before"], reverse=True)
    if normalised[-1]["minimum_hours_before"] != 0:
        raise RuntimeError("Refund tier configuration must include a 0-hour tier")
    return normalised


def calculate_refund_tier(appointment_start, now, tiers):
    """The threshold is inclusive: the exact cutoff receives that tier's percentage."""
    if appointment_start.tzinfo is None:
        appointment_start = appointment_start.replace(tzinfo=IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    seconds_before = Decimal(str((appointment_start - now).total_seconds()))
    hours_before = seconds_before / Decimal("3600")
    for tier in tiers:
        if hours_before >= tier["minimum_hours_before"]:
            return tier
    return tiers[-1]


def cancel_appointment(appointment_id, caller_id, role):
    connection = connect_db()
    already_cancelled = False
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM appointments WHERE appointment_id = %s FOR UPDATE",
                (appointment_id,),
            )
            appointment = cursor.fetchone()
            if not appointment:
                raise ApiError(404, "Appointment was not found")
            if role == "patient" and int(appointment["patient_id"]) != caller_id:
                raise ApiError(404, "Appointment was not found")
            if role == "doctor" and int(appointment["doctor_id"]) != caller_id:
                raise ApiError(404, "Appointment was not found")
            if role not in {"patient", "doctor"}:
                raise ApiError(403, "Only the patient or treating doctor can cancel this appointment")

            if appointment["status"] == "CANCELLED":
                cursor.execute(
                    """SELECT cancellation_type, original_fee, refund_percentage,
                              refund_amount, refund_tier
                       FROM refund_ledger WHERE appointment_id = %s
                       ORDER BY refund_id DESC LIMIT 1""",
                    (appointment_id,),
                )
                existing_refund = cursor.fetchone()
                if not existing_refund:
                    raise ApiError(409, "Appointment is cancelled but its refund record is missing")
                cancellation_type = existing_refund["cancellation_type"]
                tier = {
                    "name": existing_refund.get("refund_tier") or (
                        "DOCTOR_FULL" if cancellation_type == "DOCTOR" else "PREVIOUSLY_APPLIED"
                    ),
                    "refund_percentage": Decimal(str(existing_refund["refund_percentage"])),
                }
                original_fee = Decimal(str(existing_refund["original_fee"]))
                refund_amount = Decimal(str(existing_refund["refund_amount"]))
                already_cancelled = True
            elif appointment["status"] != "BOOKED":
                raise ApiError(409, "Only booked appointments can be cancelled")
            else:
                now = datetime.now(IST)
                appointment_start = appointment["start_at_ist"].replace(tzinfo=IST)
                if appointment_start <= now:
                    raise ApiError(409, "An appointment that has already started or passed cannot be cancelled")
                if role == "doctor":
                    tier = {"name": "DOCTOR_FULL", "refund_percentage": Decimal("100")}
                    cancellation_type = "DOCTOR"
                else:
                    tiers = get_refund_tiers()
                    tier = calculate_refund_tier(appointment_start, now, tiers)
                    cancellation_type = "PATIENT"

                original_fee = Decimal(str(appointment.get("consultation_fee", "0.00")))
                refund_percentage = tier["refund_percentage"]
                refund_amount = (original_fee * refund_percentage / Decimal("100")).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )

                cursor.execute(
                    """INSERT INTO refund_ledger
                       (appointment_id, cancellation_type, original_fee,
                        refund_percentage, refund_amount, refund_tier)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        appointment_id, cancellation_type, str(original_fee),
                        str(refund_percentage), str(refund_amount), tier["name"],
                    ),
                )
                cursor.execute(
                    """UPDATE appointments
                       SET status = 'CANCELLED', cancelled_at_ist = %s,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE appointment_id = %s AND status = 'BOOKED'""",
                    (now.replace(tzinfo=None), appointment_id),
                )
                connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    slot_time = appointment_slot_time(appointment)
    try:
        released_now = release_cancelled_seat_once(
            appointment_id,
            int(appointment["doctor_id"]),
            slot_time,
            str(appointment["slot_id"]),
            cancellation_type=cancellation_type,
        )
    except ApiError:
        # Re-raise domain errors (4xx) directly so the caller receives the
        # correct status code and message. The original bare `except Exception`
        # was catching ApiError (a subclass of Exception) and replacing it with
        # a 503, which the frontend displayed as "MediSlot is temporarily
        # unavailable" even for recoverable slot-contention errors.
        raise
    except Exception:
        logger.exception(
            "Appointment cancellation is in RDS but DynamoDB seat release failed appointment_id=%s",
            appointment_id,
        )
        raise ApiError(503, "Cancellation was recorded, but slot capacity could not be updated. Retry the cancellation request.")

    result = {
        "appointment_id": appointment_id,
        "status": "CANCELLED",
        "cancellation_type": cancellation_type,
        "refund_tier": tier["name"],
        "original_fee": str(original_fee),
        "refund_percentage": str(tier["refund_percentage"]),
        "refund_amount": str(refund_amount),
        "already_cancelled": already_cancelled,
    }
    if not already_cancelled:
        emit_metric(
            "CancellationsByTier", 1,
            {"Tier": tier["name"], "CancellationType": cancellation_type},
        )
    # Re-publishing on a retry is intentional: consumers should be idempotent.
    # It allows recovery when the first invocation committed cancellation but
    # failed before publishing the events.
    publish_event("AppointmentCancelled", result)
    publish_event("SlotSeatFreed", {
        "slot_id": str(appointment["slot_id"]),
        "doctor_id": int(appointment["doctor_id"]),
        "slot_time": slot_time,
        "appointment_id": appointment_id,
        "freed_seats": 1 if released_now else 0,
    })
    return result


def cancel_doctor_slot(event, doctor_id):
    body = parse_body(event)
    slot_time, mysql_slot_time = normalise_slot_time(body.get("slot_time"))
    if mysql_slot_time <= datetime.now(IST).replace(tzinfo=None):
        raise ApiError(409, "A slot in the past cannot be cancelled")

    slot = get_slot(doctor_id, slot_time)
    slot_id = str(slot.get("slotId") or "")
    original_status = str(slot.get("status", "AVAILABLE"))
    already_finalized = original_status == "SLOT_CANCELLED_BY_DOCTOR"

    if not already_finalized and original_status != "CANCELLING":
        try:
            slots_table.update_item(
                Key={"doctorId": str(doctor_id), "slotTime": slot_time},
                UpdateExpression="SET #status = :cancelling",
                ConditionExpression="#status = :available",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":cancelling": "CANCELLING",
                    ":available": original_status,
                },
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ApiError(409, "This slot is already being changed")
            raise

    connection = None
    newly_cancelled = []
    all_cancelled = []
    try:
        connection = connect_db()
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT * FROM appointments
                   WHERE doctor_id = %s AND start_at_ist = %s AND status = 'BOOKED'
                   FOR UPDATE""",
                (doctor_id, mysql_slot_time),
            )
            appointments = cursor.fetchall()
            now = datetime.now(IST).replace(tzinfo=None)

            for appointment in appointments:
                fee = Decimal(str(appointment.get("consultation_fee", "0.00")))
                cursor.execute(
                    """INSERT INTO refund_ledger
                       (appointment_id, cancellation_type, original_fee,
                        refund_percentage, refund_amount, refund_tier)
                       VALUES (%s, 'DOCTOR', %s, 100, %s, 'DOCTOR_FULL')""",
                    (appointment["appointment_id"], str(fee), str(fee)),
                )
                cursor.execute(
                    """UPDATE appointments
                       SET status = 'CANCELLED', cancelled_at_ist = %s,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE appointment_id = %s AND status = 'BOOKED'""",
                    (now, appointment["appointment_id"]),
                )
                newly_cancelled.append(int(appointment["appointment_id"]))

            # Also load previously committed doctor cancellations. This makes
            # a retry safe if the first invocation stopped before publishing events.
            cursor.execute(
                """SELECT a.appointment_id, a.patient_id, r.refund_amount
                   FROM appointments a
                   JOIN refund_ledger r ON r.appointment_id = a.appointment_id
                   WHERE a.doctor_id = %s
                     AND a.start_at_ist = %s
                     AND a.status = 'CANCELLED'
                     AND r.cancellation_type = 'DOCTOR'""",
                (doctor_id, mysql_slot_time),
            )
            all_cancelled = [
                {
                    "appointment_id": int(row["appointment_id"]),
                    "patient_id": int(row["patient_id"]),
                    "refund_amount": str(row["refund_amount"]),
                }
                for row in cursor.fetchall()
            ]
            connection.commit()
    except Exception:
        if connection:
            connection.rollback()
        if not already_finalized:
            try:
                slots_table.update_item(
                    Key={"doctorId": str(doctor_id), "slotTime": slot_time},
                    UpdateExpression="SET #status = :status",
                    ConditionExpression="#status = :cancelling",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":status": original_status, ":cancelling": "CANCELLING",
                    },
                )
            except Exception:
                logger.exception("Could not restore slot after failed doctor cancellation")
        raise
    finally:
        if connection:
            connection.close()

    try:
        if already_finalized:
            slots_table.update_item(
                Key={"doctorId": str(doctor_id), "slotTime": slot_time},
                UpdateExpression="SET #booked = :zero",
                ConditionExpression="#status = :cancelled",
                ExpressionAttributeNames={
                    "#status": "status", "#booked": "bookedCount",
                },
                ExpressionAttributeValues={":cancelled": "SLOT_CANCELLED_BY_DOCTOR", ":zero": 0},
            )
        else:
            slots_table.update_item(
                Key={"doctorId": str(doctor_id), "slotTime": slot_time},
                UpdateExpression="SET #status = :cancelled, #booked = :zero",
                ConditionExpression="#status = :cancelling",
                ExpressionAttributeNames={
                    "#status": "status", "#booked": "bookedCount",
                },
                ExpressionAttributeValues={
                    ":cancelled": "SLOT_CANCELLED_BY_DOCTOR", ":cancelling": "CANCELLING",
                    ":zero": 0,
                },
            )
    except Exception:
        logger.exception(
            "Doctor cancellations committed but slot could not be finalized doctor_id=%s slot_time=%s",
            doctor_id, slot_time,
        )
        raise ApiError(503, "Appointments were cancelled, but the slot is not finalized. Retry this request.")

    newly_cancelled_ids = set(newly_cancelled)
    for item in all_cancelled:
        detail = {
            **item,
            "doctor_id": doctor_id,
            "slot_id": slot_id,
            "slot_time": slot_time,
            "status": "CANCELLED",
            "cancellation_type": "DOCTOR",
            "refund_percentage": "100",
            "refund_tier": "DOCTOR_FULL",
        }
        if item["appointment_id"] in newly_cancelled_ids:
            emit_metric("CancellationsByTier", 1, {
                "Tier": "DOCTOR_FULL", "CancellationType": "DOCTOR",
            })
        publish_event("AppointmentCancelled", detail)

    publish_event("SlotSeatFreed", {
        "slot_id": slot_id,
        "doctor_id": doctor_id,
        "slot_time": slot_time,
        "freed_seats": len(all_cancelled),
        "reason": "DOCTOR_SLOT_CANCELLATION",
    })
    return {
        "message": "Doctor slot cancelled successfully",
        "slot_id": slot_id,
        "cancelled_appointments": len(all_cancelled),
        "appointments": all_cancelled,
        "already_cancelled": already_finalized,
    }


def list_appointments(caller_id, role):
    if role == "patient":
        where, value = "a.patient_id = %s", caller_id
    elif role == "doctor":
        where, value = "a.doctor_id = %s", caller_id
    else:
        raise ApiError(403, "Only patients and doctors can view appointments")

    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            now_ist = datetime.now(IST).replace(tzinfo=None)
            cursor.execute(
                f"""UPDATE appointments a
                    SET status = 'COMPLETED'
                    WHERE {where}
                      AND status IN ('BOOKED', 'CONFIRMED')
                      AND end_at_ist <= %s""",
                (value, now_ist),
            )
            connection.commit()
            cursor.execute(
                f"""SELECT a.appointment_id, a.patient_id, a.doctor_id,
                           a.hospital_id, a.consultation_type_id, a.slot_id,
                           a.start_at_ist, a.end_at_ist, a.status,
                           COALESCE(NULLIF(a.consultation_fee, 0), ct.consultation_fee) AS consultation_fee,
                           a.created_at,
                           (SELECT r.cancellation_type FROM refund_ledger r
                            WHERE r.appointment_id = a.appointment_id
                            ORDER BY r.refund_id DESC LIMIT 1) AS cancellation_type,
                           d.first_name AS doctor_first_name,
                           d.last_name AS doctor_last_name,
                           p.first_name AS patient_first_name,
                           p.last_name AS patient_last_name,
                           p.phone AS patient_phone,
                           p.email AS patient_email,
                           h.hospital_name, h.branch_name, h.city,
                           ct.type_name AS consultation_type
                    FROM appointments a
                    JOIN doctors d ON d.doctor_id = a.doctor_id
                    JOIN patients p ON p.patient_id = a.patient_id
                    JOIN hospitals h ON h.hospital_id = a.hospital_id
                    JOIN consultation_types ct
                      ON ct.consultation_type_id = a.consultation_type_id
                    WHERE {where}
                    ORDER BY a.start_at_ist DESC, a.appointment_id DESC
                    LIMIT 100""",
                (value,),
            )
            return cursor.fetchall()
    finally:
        connection.close()


def appointment_detail(appointment_id, caller_id, role):
    if role == "patient":
        where, value = "a.patient_id = %s", caller_id
    elif role == "doctor":
        where, value = "a.doctor_id = %s", caller_id
    else:
        raise ApiError(403, "Only patients and doctors can view appointment details")

    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""SELECT a.appointment_id, a.patient_id, a.doctor_id,
                           a.hospital_id, a.consultation_type_id, a.slot_id,
                           a.start_at_ist, a.end_at_ist, a.timezone, a.status,
                           a.consultation_fee, a.reschedule_count,
                           a.cancelled_at_ist, a.created_at, a.updated_at,
                           d.first_name AS doctor_first_name,
                           d.last_name AS doctor_last_name,
                           h.hospital_name, h.branch_name, h.city,
                           ct.type_name AS consultation_type
                    FROM appointments a
                    JOIN doctors d ON d.doctor_id = a.doctor_id
                    JOIN hospitals h ON h.hospital_id = a.hospital_id
                    JOIN consultation_types ct
                      ON ct.consultation_type_id = a.consultation_type_id
                    WHERE a.appointment_id = %s AND {where}
                    LIMIT 1""",
                (appointment_id, value),
            )
            item = cursor.fetchone()
            if not item:
                raise ApiError(404, "Appointment was not found")
            return item
    finally:
        connection.close()


def list_refunds(caller_id, role):
    if role == "patient":
        where, value = "a.patient_id = %s", caller_id
    elif role == "doctor":
        where, value = "a.doctor_id = %s", caller_id
    else:
        raise ApiError(403, "Only patients and doctors can view refund records")

    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""SELECT r.refund_id, r.appointment_id, r.cancellation_type,
                           COALESCE(
                               NULLIF(r.original_fee, 0),
                               NULLIF(a.consultation_fee, 0),
                               ct.consultation_fee
                           ) AS original_fee,
                           r.refund_percentage,
                           CASE
                               WHEN r.refund_amount > 0 THEN r.refund_amount
                               ELSE ROUND(
                                   COALESCE(
                                       NULLIF(r.original_fee, 0),
                                       NULLIF(a.consultation_fee, 0),
                                       ct.consultation_fee
                                   ) * r.refund_percentage / 100,
                                   2
                               )
                           END AS refund_amount,
                           r.created_at, a.start_at_ist, a.end_at_ist, a.slot_id,
                           a.status,
                           p.first_name AS patient_first_name,
                           p.last_name AS patient_last_name,
                           p.email AS patient_email,
                           d.first_name AS doctor_first_name,
                           d.last_name AS doctor_last_name
                    FROM refund_ledger r
                    JOIN appointments a ON a.appointment_id = r.appointment_id
                    JOIN consultation_types ct
                      ON ct.consultation_type_id = a.consultation_type_id
                    JOIN patients p ON p.patient_id = a.patient_id
                    JOIN doctors d ON d.doctor_id = a.doctor_id
                    WHERE {where}
                    ORDER BY r.created_at DESC, r.refund_id DESC""",
                (value,),
            )
            return cursor.fetchall()
    finally:
        connection.close()


def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    method = str(event.get("httpMethod") or "").upper()
    path = get_path(event)
    caller_id, role = get_auth(event)

    logger.info(
        "APPOINTMENT REQUEST START request_id=%s method=%s path=%s role=%s",
        request_id, method, path, role,
    )

    try:
        if not caller_id:
            raise ApiError(401, "Authentication is required")

        if method == "POST" and path == "/appointments":
            if role != "patient":
                raise ApiError(403, "Only patients can book appointments")
            return book_appointment(event, caller_id, request_id)

        if method == "GET" and path == "/appointments":
            return response(200, {
                "message": "Appointments retrieved successfully",
                "appointments": list_appointments(caller_id, role),
            })

        if method == "GET" and path == "/appointments/refunds":
            return response(200, {
                "message": "Refund history retrieved successfully",
                "refunds": list_refunds(caller_id, role),
            })

        if method == "POST" and path == "/appointments/doctor-cancel-slot":
            if role != "doctor":
                raise ApiError(403, "Only doctors can cancel their own slots")
            return response(200, cancel_doctor_slot(event, caller_id))

        parts = [part for part in path.split("/") if part]
        if len(parts) == 2 and parts[0] == "appointments":
            try:
                appointment_id = int(parts[1])
            except ValueError:
                raise ApiError(400, "appointment_id must be an integer")
            if appointment_id <= 0:
                raise ApiError(400, "appointment_id must be positive")
            if method == "GET":
                return response(200, {
                    "message": "Appointment retrieved successfully",
                    "appointment": appointment_detail(appointment_id, caller_id, role),
                })

        if len(parts) == 3 and parts[0] == "appointments" and parts[2] == "cancel" and method == "POST":
            try:
                appointment_id = int(parts[1])
            except ValueError:
                raise ApiError(400, "appointment_id must be an integer")
            if appointment_id <= 0:
                raise ApiError(400, "appointment_id must be positive")
            result = cancel_appointment(appointment_id, caller_id, role)
            return response(200, {
                "message": "Appointment cancelled successfully",
                "cancellation": result,
            })

        raise ApiError(404, "Route not found")

    except ApiError as error:
        payload = {"message": error.message}
        if error.details:
            payload["details"] = error.details
        return response(error.status_code, payload)
    except pymysql.err.IntegrityError:
        logger.exception("Appointment database integrity error request_id=%s", request_id)
        return response(409, {"message": "This booking conflicts with an existing appointment"})
    except Exception:
        logger.exception("Appointment request failed request_id=%s", request_id)
        return response(500, {"message": "Internal server error"})
    finally:
        logger.info("APPOINTMENT REQUEST END request_id=%s", request_id)