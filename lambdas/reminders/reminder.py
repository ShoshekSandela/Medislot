"""Reminder dispatcher, every five minutes.

Each confirmed upcoming appointment gets at most one 24-hour reminder and one
2-hour reminder. Within two hours only the 2-hour reminder is sent, so a
patient is not reminded twice for the same slot.
"""

import logging
from datetime import datetime, timedelta

from lambdas.appointment import appointment as appt
from lambdas.notification import notification as notify

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def due_interval(start, now):
    remaining = start - now
    if remaining <= timedelta(0):
        return None
    if remaining <= timedelta(hours=2):
        return "2H"
    if remaining <= timedelta(hours=24):
        return "24H"
    return None


def run(rows, now, *, dispatch_log=None, publish=None, now_epoch=None):
    sent = 0
    for row in rows:
        interval = due_interval(row["start_at_ist"], now)
        if interval is None:
            continue
        sent += notify.dispatch(
            "ReminderDue",
            {"appointment_id": int(row["appointment_id"]), "patient_id": int(row["patient_id"]),
             "interval": interval, "start_at_ist": row["start_at_ist"].isoformat()},
            dispatch_log=dispatch_log, publish=publish, now_epoch=now_epoch,
        )
    return sent


def lambda_handler(event, context):
    connection = appt.connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT appointment_id, patient_id, start_at_ist FROM appointments
                   WHERE status = 'CONFIRMED' AND start_at_ist > NOW()
                     AND start_at_ist <= DATE_ADD(NOW(), INTERVAL 24 HOUR)"""
            )
            rows = cursor.fetchall()
    finally:
        connection.close()
    now = datetime.now(appt.IST).replace(tzinfo=None)
    return {"reminders_sent": run(rows, now)}
