"""Reschedule saga operations, one per Step Functions task.

The state machine (statemachines/reschedule-saga.asl.json) calls lambda_handler
with {"operation": <name>, "input": <state>}. Order of operations:

  validate -> claim_target -> release_original -> commit_appointment
           -> publish_rescheduled

Compensation when release_original fails:  compensate_target.
Compensation when commit_appointment fails: reclaim_original, then compensate_target.
If the reclaim fails the run ends as ReconciliationRequired (seat counts need a repair).

Each seat change carries a token derived from appointment id and reschedule
count, so a retried task never applies the same change twice.
"""

import logging
import os
from datetime import datetime, timedelta

import boto3

from lambdas.appointment import appointment as appt
from lambdas.common import seats
from lambdas.common.settings import get_setting

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = appt.REGION
SLOTS_TABLE_NAME = os.environ.get("SLOTS_TABLE_NAME", "medislot-dev-slots")
_store = None


class RescheduleRejected(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def get_store():
    global _store
    if _store is None:
        _store = seats.DynamoSlotStore(boto3.resource("dynamodb", region_name=REGION).Table(SLOTS_TABLE_NAME))
    return _store


def set_store(store):
    """Test seam: inject an in-memory slot store."""
    global _store
    _store = store


def validate(state):
    patient_id = int(state["patient_id"])
    appointment_id = int(state["appointment_id"])
    target_slot_time, target_start = appt.normalise_slot_time(state.get("target_slot_time"))

    connection = appt.connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM appointments WHERE appointment_id = %s", (appointment_id,))
            row = cursor.fetchone()
    finally:
        connection.close()

    if not row or int(row["patient_id"]) != patient_id:
        raise RescheduleRejected(404, "Appointment was not found")
    if str(row["status"]).upper() != "CONFIRMED":
        raise RescheduleRejected(409, "Only confirmed appointments can be rescheduled")

    now = datetime.now(appt.IST).replace(tzinfo=None)
    cutoff_hours = get_setting("reschedule/cutoff-hours", 6)
    if row["start_at_ist"] - now < timedelta(hours=cutoff_hours):
        raise RescheduleRejected(409, f"Appointments within {cutoff_hours} hours cannot be rescheduled")
    max_attempts = get_setting("reschedule/max-attempts", 2)
    if int(row["reschedule_count"]) >= max_attempts:
        raise RescheduleRejected(409, "This appointment has reached its reschedule limit")
    if target_start <= now:
        raise RescheduleRejected(409, "The target slot is in the past")

    doctor_id = int(row["doctor_id"])
    original_slot_time = appt.appointment_slot_time(row)
    original_key = {"doctorId": str(doctor_id), "slotTime": original_slot_time}
    target_key = {"doctorId": str(doctor_id), "slotTime": target_slot_time}
    if target_key == original_key:
        raise RescheduleRejected(409, "The appointment is already booked into that slot")

    target = get_store().get(target_key)
    if target is None:
        raise RescheduleRejected(409, "The target slot was not found")
    if str(target.get("consultationTypeId")) != str(row["consultation_type_id"]):
        raise RescheduleRejected(409, "The target slot must use the same consultation type")
    if target.get("isBlackout") is True or str(target.get("status", "")).upper() in seats.BLOCKED_STATUSES:
        raise RescheduleRejected(409, "The target slot is unavailable")
    _, target_end = appt.normalise_slot_time(target.get("endTime"))

    return {
        "appointment_id": appointment_id,
        "patient_id": patient_id,
        "doctor_id": doctor_id,
        "reschedule_count": int(row["reschedule_count"]),
        "consultation_type_id": int(row["consultation_type_id"]),
        "original_slot_id": str(row["slot_id"]),
        "original_key": original_key,
        "target_key": target_key,
        "target_slot_id": str(target["slotId"]),
        "target_hospital_id": int(target["hospitalId"]),
        "target_start": target_start.isoformat(),
        "target_end": target_end.isoformat(),
        "consultation_fee": str(row["consultation_fee"]),
    }


def claim_target(record):
    hop = record["reschedule_count"] + 1
    applied = seats.book_seat(get_store(), record["target_key"],
                              f"claim:{record['appointment_id']}:{hop}")
    return {"claimed": True, "applied": applied}


def release_original(record):
    applied = seats.release_booked(get_store(), record["original_key"],
                                   f"release:{record['appointment_id']}:{record['reschedule_count']}")
    return {"released": True, "applied": applied}


def commit_appointment(record):
    appointment_id = record["appointment_id"]
    hop = record["reschedule_count"] + 1
    connection = appt.connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT slot_id, reschedule_count, status FROM appointments WHERE appointment_id = %s FOR UPDATE",
                (appointment_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise RescheduleRejected(404, "Appointment was not found")
            if str(row["slot_id"]) == record["target_slot_id"] and int(row["reschedule_count"]) == hop:
                connection.commit()
                return {"committed": True, "replayed": True}
            if (str(row["status"]).upper() != "CONFIRMED"
                    or str(row["slot_id"]) != record["original_slot_id"]
                    or int(row["reschedule_count"]) != record["reschedule_count"]):
                raise RescheduleRejected(409, "The appointment changed during rescheduling")
            cursor.execute(
                """UPDATE appointments
                   SET slot_id = %s, hospital_id = %s, start_at_ist = %s, end_at_ist = %s,
                       reschedule_count = reschedule_count + 1, updated_at = CURRENT_TIMESTAMP
                   WHERE appointment_id = %s AND status = 'CONFIRMED' AND reschedule_count = %s""",
                (
                    record["target_slot_id"], record["target_hospital_id"],
                    datetime.fromisoformat(record["target_start"]), datetime.fromisoformat(record["target_end"]),
                    appointment_id, record["reschedule_count"],
                ),
            )
            if cursor.rowcount != 1:
                raise RescheduleRejected(409, "The appointment changed during rescheduling")
        connection.commit()
        return {"committed": True, "replayed": False}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def publish_rescheduled(record):
    appt.emit_metric("ReschedulesSucceeded", 1)
    appt.publish_event("AppointmentRescheduled", {
        "appointment_id": record["appointment_id"],
        "patient_id": record["patient_id"],
        "doctor_id": record["doctor_id"],
        "slot_id": record["target_slot_id"],
        "previous_slot_id": record["original_slot_id"],
        "start_at_ist": record["target_start"],
        "end_at_ist": record["target_end"],
        "reschedule_count": record["reschedule_count"] + 1,
    })
    return {"published": True}


def compensate_target(record):
    hop = record["reschedule_count"] + 1
    applied = seats.release_booked(get_store(), record["target_key"],
                                   f"undo:{record['appointment_id']}:{hop}")
    appt.emit_metric("ReschedulesCompensated", 1)
    return {"compensated": True, "applied": applied}


def reclaim_original(record):
    applied = seats.book_seat(get_store(), record["original_key"],
                              f"reclaim:{record['appointment_id']}:{record['reschedule_count']}")
    return {"reclaimed": True, "applied": applied}


def mark_reconciliation_required(record):
    """Called by the state machine when compensation itself could not finish."""
    appt.emit_metric("ReschedulesNeedReconciliation", 1)
    logger.error("RESCHEDULE RECONCILIATION REQUIRED appointment_id=%s", record["appointment_id"])
    return {"reconciliation_required": True}


OPERATIONS = {
    "claim_target": claim_target,
    "release_original": release_original,
    "commit_appointment": commit_appointment,
    "publish_rescheduled": publish_rescheduled,
    "compensate_target": compensate_target,
    "reclaim_original": reclaim_original,
    "mark_reconciliation_required": mark_reconciliation_required,
}


def lambda_handler(event, context):
    if event.get("httpMethod"):
        # API Gateway entry point (PATCH /appointments/{id}). Identity comes from the authorizer.
        from lambdas.appointment.appointment import get_auth, ApiError, response
        patient_id, role = get_auth(event)
        try:
            if not patient_id:
                raise ApiError(401, "Authentication is required")
            if role != "patient":
                raise ApiError(403, "Only patients can reschedule their appointments")
            return start_reschedule(event, patient_id)
        except ApiError as error:
            return response(error.status_code, {"message": error.message})
    operation = event.get("operation")
    state = event.get("input") or {}
    logger.info("RESCHEDULE operation=%s correlation_id=%s",
                operation, state.get("correlation_id", "none"))
    if operation == "validate":
        return validate(state)
    if operation not in OPERATIONS:
        raise ValueError(f"Unknown reschedule operation: {operation}")
    record = state["validated"]["result"]
    return OPERATIONS[operation](record)


STATE_MACHINE_ARN_ENV = "RESCHEDULE_STATE_MACHINE_ARN"
_sfn = None


def get_sfn():
    global _sfn
    if _sfn is None:
        _sfn = boto3.client("stepfunctions", region_name=REGION)
    return _sfn


def start_reschedule(event, patient_id, *, client=None, state_machine_arn=None):
    """PATCH /appointments/{id}: validate the request shape and start the saga.

    The execution name is deterministic per appointment, reschedule count and
    target slot, so a retried request reuses the same execution instead of starting
    a second saga. Step Functions rejects the duplicate name, which is reported as
    202 Accepted with the existing execution.
    """
    import hashlib
    import json
    from botocore.exceptions import ClientError
    from lambdas.appointment.appointment import ApiError, get_path, parse_body, response

    path_parts = [part for part in get_path(event).split("/") if part]
    if len(path_parts) != 2 or path_parts[0] != "appointments":
        raise ApiError(404, "Route not found")
    try:
        appointment_id = int(path_parts[1])
    except ValueError:
        raise ApiError(400, "appointment_id must be an integer")
    body = parse_body(event)
    target = body.get("target_slot_time")
    appt.normalise_slot_time(target)

    arn = state_machine_arn or os.environ.get(STATE_MACHINE_ARN_ENV, "")
    if not arn:
        raise ApiError(503, "Rescheduling is not configured in this environment")
    client = client or get_sfn()
    digest = hashlib.sha256(f"{appointment_id}|{target}".encode("utf-8")).hexdigest()[:16]
    name = f"resched-{appointment_id}-{digest}"
    payload = {"patient_id": patient_id, "appointment_id": appointment_id,
               "target_slot_time": target, "correlation_id": event.get("correlation_id", name)}
    try:
        execution = client.start_execution(stateMachineArn=arn, name=name, input=json.dumps(payload))
        return response(202, {"message": "Reschedule started", "execution_arn": execution["executionArn"]})
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ExecutionAlreadyExists":
            return response(202, {"message": "Reschedule already in progress", "execution_name": name})
        raise
