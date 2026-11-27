"""No-show sweeper, every fifteen minutes.

Marks CONFIRMED appointments NO_SHOW once 30 minutes have passed since the slot
ended. Matches the outcome path in the Appointment Lambda: status and timestamp
only, no seat release and no refund row. Safe to run twice: the second run finds
no CONFIRMED rows past the grace period.
"""

import logging

from lambdas.appointment import appointment as appt
from lambdas.common.settings import get_setting

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    grace_minutes = get_setting("no-show/grace-minutes", 30)
    connection = appt.connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """UPDATE appointments
                   SET status = 'NO_SHOW', outcome_at_ist = NOW()
                   WHERE status = 'CONFIRMED'
                     AND end_at_ist <= DATE_SUB(NOW(), INTERVAL %s MINUTE)""",
                (int(grace_minutes),),
            )
            marked = cursor.rowcount
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    if marked:
        appt.emit_metric("NoShowRate", marked)
    return {"marked_no_show": marked}
