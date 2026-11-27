"""Notification dispatcher.

Input: SQS batch whose bodies are EventBridge event envelopes. Each event is
mapped to recipients (the table in the brief), then each recipient gets one SNS
publish guarded by a DispatchLog claim. The dispatch id is
<reference>#<EVENT>#<interval>#<role>, so duplicates and redeliveries are skipped.
"""

import json
import logging
import os
import time

import boto3

from lambdas.appointment import appointment as appt
from lambdas.common import repos

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = appt.REGION
DISPATCH_TABLE_NAME = os.environ.get("DISPATCH_LOG_TABLE_NAME", "medislot-dev-dispatch-log")
PATIENT_TOPIC_ARN = os.environ.get("PATIENT_TOPIC_ARN", "")
DOCTOR_TOPIC_ARN = os.environ.get("DOCTOR_TOPIC_ARN", "")
DISPATCH_TTL_SECONDS = 7 * 24 * 60 * 60

_log = None
_sns = None


def roles_for(detail_type, detail):
    """Recipients from the Frozen Rule Set notification table."""
    if detail_type in {"AppointmentBooked", "AppointmentRescheduled"}:
        return ["patient", "doctor"]
    if detail_type == "AppointmentCancelled":
        if str(detail.get("status", "")).upper() == "CANCELLED_BY_DOCTOR":
            return ["patient"]
        return ["patient", "doctor"]
    if detail_type in {"WaitlistOfferIssued", "WaitlistOfferExpired", "ReminderDue"}:
        return ["patient"]
    return []


def reference_for(detail_type, detail):
    if detail_type in {"WaitlistOfferIssued", "WaitlistOfferExpired"}:
        return f"offer-{detail['offer_id']}"
    return f"appt-{detail['appointment_id']}"


def interval_for(detail_type, detail):
    return str(detail.get("interval", "EVENT")).upper()


def _topic_for(role):
    return PATIENT_TOPIC_ARN if role == "patient" else DOCTOR_TOPIC_ARN


def dispatch(detail_type, detail, *, dispatch_log=None, publish=None, now_epoch=None):
    """Send each recipient's notification once. Returns the number actually sent."""
    dispatch_log = dispatch_log or get_log()
    publish = publish or _sns_publish
    now_epoch = now_epoch or int(time.time())
    sent = 0
    for role in roles_for(detail_type, detail):
        dispatch_id = f"{reference_for(detail_type, detail)}#{detail_type.upper()}#{interval_for(detail_type, detail)}#{role}"
        if not dispatch_log.claim(dispatch_id, now_epoch, now_epoch + DISPATCH_TTL_SECONDS):
            logger.info("Duplicate notification skipped dispatch_id=%s", dispatch_id)
            continue
        publish(_topic_for(role), {"event": detail_type, "role": role, "detail": detail}, detail_type)
        dispatch_log.mark_sent(dispatch_id)
        sent += 1
    if sent:
        appt.emit_metric("NotificationsDispatched", sent)
    return sent


def _sns_publish(topic_arn, message, event_type):
    global _sns
    if _sns is None:
        _sns = boto3.client("sns", region_name=REGION)
    _sns.publish(
        TopicArn=topic_arn,
        Message=json.dumps(message, default=str),
        Subject=event_type[:99],
        MessageAttributes={"eventType": {"DataType": "String", "StringValue": event_type}},
    )


def get_log():
    global _log
    if _log is None:
        _log = repos.DynamoDispatchLog(boto3.resource("dynamodb", region_name=REGION).Table(DISPATCH_TABLE_NAME))
    return _log


def lambda_handler(event, context):
    failures = []
    for record in event.get("Records", []):
        try:
            envelope = json.loads(record["body"])
            detail_type = envelope["detail-type"]
            detail = envelope.get("detail") or {}
            if detail_type == "AppointmentCancelled" and "patient_id" not in detail:
                detail = {**detail, **appt_parties(detail["appointment_id"])}
            dispatch(detail_type, detail)
        except Exception:
            logger.exception("Notification failed messageId=%s", record.get("messageId"))
            failures.append({"itemIdentifier": record.get("messageId")})
    return {"batchItemFailures": failures}


def appt_parties(appointment_id):
    connection = appt.connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT patient_id, doctor_id FROM appointments WHERE appointment_id = %s",
                           (int(appointment_id),))
            row = cursor.fetchone() or {}
            return {"patient_id": row.get("patient_id"), "doctor_id": row.get("doctor_id")}
    finally:
        connection.close()
