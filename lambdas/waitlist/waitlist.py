"""Waitlist join, leave, list and offer acceptance.

Routes (added to the existing API under /waitlist and /waitlist-offers):
  POST   /waitlist                      body {slot_id}   join a full slot
  DELETE /waitlist                      body {slot_id}   leave
  GET    /patients/me/waitlists                           list own entries
  POST   /waitlist-offers/{offerId}/accept                accept a live offer

slot_id travels in the body rather than the path because the composite slot id
contains '#' and ':' characters that do not survive path routing reliably.
"""

import logging
import os
import time
import uuid
from datetime import datetime, timedelta

import boto3

from lambdas.appointment import appointment as appt
from lambdas.common import repos, seats
from lambdas.common.settings import get_setting

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = appt.REGION
SLOTS_TABLE_NAME = os.environ.get("SLOTS_TABLE_NAME", "medislot-dev-slots")
WAITLIST_TABLE_NAME = os.environ.get("WAITLIST_TABLE_NAME", "medislot-dev-waitlist")
HOLD_OFFERS_TABLE_NAME = os.environ.get("HOLD_OFFERS_TABLE_NAME", "medislot-dev-hold-offers")

_store = None
_repo = None
ACTIVE = repos.ACTIVE_WAITLIST_STATES


def get_store():
    global _store
    if _store is None:
        _store = seats.DynamoSlotStore(boto3.resource("dynamodb", region_name=REGION).Table(SLOTS_TABLE_NAME))
    return _store


def get_repo():
    global _repo
    if _repo is None:
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        _repo = repos.DynamoWaitlistRepo(
            dynamodb.Table(WAITLIST_TABLE_NAME), dynamodb.Table(HOLD_OFFERS_TABLE_NAME))
    return _repo


def set_backends(store, repo):
    """Test seam."""
    global _store, _repo
    _store, _repo = store, repo


def parse_slot_id(slot_id):
    """slotId is doctorId#hospitalId#consultationTypeId#slotTime."""
    if not isinstance(slot_id, str):
        raise appt.ApiError(400, "slot_id is required")
    parts = slot_id.split("#", 3)
    if len(parts) != 4 or not parts[0].isdigit():
        raise appt.ApiError(400, "slot_id is not a valid slot identifier")
    return {"doctorId": parts[0], "slotTime": parts[3]}


def _count(item, name):
    return int(item.get(name) or 0)


def join(patient_id, slot_id, now=None):
    now = now or datetime.now(appt.IST).replace(tzinfo=None)
    key = parse_slot_id(slot_id)
    slot = get_store().get(key)
    if slot is None or str(slot.get("slotId")) != slot_id:
        raise appt.ApiError(404, "Appointment slot was not found")
    capacity = max(1, _count(slot, "capacity") or 1)
    if _count(slot, "bookedCount") + _count(slot, "heldCount") < capacity:
        raise appt.ApiError(409, "This slot still has free seats. Book it instead of joining the waitlist.")

    repo = get_repo()
    existing = repo.waitlist_for_patient(patient_id)
    active = [entry for entry in existing if entry.get("status") in ACTIVE]
    if any(entry.get("slotId") == slot_id for entry in active):
        raise appt.ApiError(409, "You are already on the waitlist for this slot")
    max_active = get_setting("waitlist/max-active", 5)
    if len(active) >= max_active:
        raise appt.ApiError(409, f"You can be on at most {max_active} waitlists at once")

    joined_at = now.isoformat(timespec="microseconds")
    item = {
        "patientId": str(patient_id),
        "waitlistSortKey": f"{joined_at}#{slot_id}",
        "slotId": slot_id,
        "doctorId": key["doctorId"],
        "slotTime": key["slotTime"],
        "joinedAt": joined_at,
        "status": "WAITING",
    }
    if not repo.put_waitlist(item):
        raise appt.ApiError(409, "You are already on this waitlist")
    return item


def leave(patient_id, slot_id):
    repo = get_repo()
    for entry in repo.waitlist_for_patient(patient_id):
        if entry.get("slotId") != slot_id or entry.get("status") not in ACTIVE:
            continue
        if entry["status"] == "OFFERED":
            raise appt.ApiError(409, "Respond to your offer before leaving this waitlist")
        if repo.set_waitlist_status(patient_id, entry["waitlistSortKey"], "WAITING", "LEFT"):
            return {"slot_id": slot_id, "status": "LEFT"}
        raise appt.ApiError(409, "Your waitlist entry changed. Refresh and try again.")
    raise appt.ApiError(404, "You are not on the waitlist for this slot")


def list_for_patient(patient_id):
    repo = get_repo()
    entries = sorted(repo.waitlist_for_patient(patient_id), key=lambda e: e["joinedAt"])
    result = []
    for entry in entries:
        view = {k: entry[k] for k in ("slotId", "doctorId", "slotTime", "joinedAt", "status")}
        if entry.get("status") == "WAITING":
            queue = repo.waiting_for_slot(entry["slotId"])
            view["position"] = [q["waitlistSortKey"] for q in queue].index(entry["waitlistSortKey"]) + 1
        result.append(view)
    return result


def _find_appointment_by_offer(offer_id):
    connection = appt.connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT appointment_id, slot_id, start_at_ist, end_at_ist, consultation_fee, status "
                "FROM appointments WHERE idempotency_key = %s LIMIT 1",
                (f"offer:{offer_id}",),
            )
            return cursor.fetchone()
    finally:
        connection.close()


def _insert_appointment(offer, slot, offer_id):
    """Write the confirmed appointment. Same patient and doctor limits as booking."""
    slot_time, start = appt.normalise_slot_time(offer["slotTime"])
    _, end = appt.normalise_slot_time(slot.get("endTime"))
    patient_id = int(offer["patientId"])
    doctor_id = int(offer["doctorId"])
    connection = appt.connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT patient_id, account_status FROM patients WHERE patient_id = %s FOR UPDATE",
                (patient_id,),
            )
            patient = cursor.fetchone()
            if not patient or str(patient.get("account_status", "ACTIVE")).upper() != "ACTIVE":
                raise appt.ApiError(403, "An active patient account is required")
            cursor.execute(
                """SELECT COUNT(*) AS n FROM appointments WHERE patient_id = %s
                   AND status IN ('CONFIRMED') AND start_at_ist >= NOW()""",
                (patient_id,),
            )
            if int(cursor.fetchone()["n"]) >= appt.MAX_ACTIVE_APPOINTMENTS:
                raise appt.ApiError(409, "You have reached the maximum number of active appointments")
            cursor.execute(
                """SELECT COUNT(*) AS n FROM appointments WHERE patient_id = %s AND doctor_id = %s
                   AND status = 'CONFIRMED' AND start_at_ist >= NOW()""",
                (patient_id, doctor_id),
            )
            if int(cursor.fetchone()["n"]) >= appt.MAX_UPCOMING_APPOINTMENTS_PER_DOCTOR:
                raise appt.ApiError(409, "You can have at most 3 upcoming appointments with the same doctor")
            # RDS is the pricing source of truth, as in the booking path.
            cursor.execute(
                """SELECT consultation_fee FROM consultation_types
                   WHERE consultation_type_id = %s AND is_active = TRUE""",
                (int(slot["consultationTypeId"]),),
            )
            fee_row = cursor.fetchone()
            if not fee_row:
                raise appt.ApiError(409, "The selected consultation type is no longer available")
            cursor.execute(
                """INSERT INTO appointments
                   (patient_id, doctor_id, hospital_id, consultation_type_id, slot_id,
                    start_at_ist, end_at_ist, timezone, status, consultation_fee, idempotency_key)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'Asia/Kolkata', 'CONFIRMED', %s, %s)""",
                (patient_id, doctor_id, int(slot["hospitalId"]), int(slot["consultationTypeId"]),
                 offer["slotId"], start, end, str(fee_row["consultation_fee"]),
                 f"offer:{offer_id}"),
            )
            appointment_id = int(cursor.lastrowid)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "appointment_id": appointment_id,
        "patient_id": patient_id,
        "doctor_id": doctor_id,
        "slot_id": offer["slotId"],
        "start_at_ist": start.isoformat(),
        "end_at_ist": end.isoformat(),
        "status": "CONFIRMED",
    }


def accept(patient_id, offer_id, now_epoch=None):
    now_epoch = now_epoch or int(time.time())
    repo = get_repo()
    offer = repo.get_offer(offer_id)
    if offer is None or str(offer.get("patientId")) != str(patient_id):
        raise appt.ApiError(404, "Offer was not found")

    status = offer.get("offerStatus")
    if status == "ACCEPTED":
        return {"offer_id": offer_id, "appointment_id": int(offer["appointmentId"]), "replayed": True}
    if status == "EXPIRED":
        raise appt.ApiError(409, "This offer has expired")
    if status == "ACCEPTING":
        # Another attempt is in progress or crashed after writing the appointment.
        found = _find_appointment_by_offer(offer_id)
        if found:
            repo.set_offer_status(offer_id, "ACCEPTING", "ACCEPTED", {"appointmentId": int(found["appointment_id"])})
            return {"offer_id": offer_id, "appointment_id": int(found["appointment_id"]), "replayed": True}
        raise appt.ApiError(409, "This offer is being processed. Retry shortly.")
    if status != "PENDING":
        raise appt.ApiError(409, "This offer is no longer available")
    if int(offer["expiresAt"]) <= now_epoch:
        raise appt.ApiError(409, "This offer has expired")

    if not repo.set_offer_status(offer_id, "PENDING", "ACCEPTING"):
        raise appt.ApiError(409, "This offer changed. Refresh and try again.")

    key = {"doctorId": str(offer["doctorId"]), "slotTime": offer["slotTime"]}
    store = get_store()
    try:
        seats.convert_hold(store, key, f"accept:{offer_id}")
    except (seats.SeatUnavailable, seats.SlotMissing):
        repo.set_offer_status(offer_id, "ACCEPTING", "EXPIRED")
        raise appt.ApiError(409, "This seat is no longer available")

    slot = store.get(key)
    try:
        appointment = _insert_appointment(offer, slot, offer_id)
    except Exception:
        # The appointment was not written: give the seat back to the hold and reopen the offer.
        seats.convert_back(store, key, f"accept-undo:{offer_id}")
        repo.set_offer_status(offer_id, "ACCEPTING", "PENDING")
        logger.exception("Offer acceptance failed; hold restored offer_id=%s", offer_id)
        raise appt.ApiError(409, "The appointment could not be confirmed. Your offer is still open; try again.")

    repo.set_offer_status(offer_id, "ACCEPTING", "ACCEPTED", {"appointmentId": appointment["appointment_id"]})
    repo.set_waitlist_status(patient_id, offer["waitlistSortKey"], "OFFERED", "ACCEPTED")
    appt.publish_event("AppointmentBooked", {**appointment, "source": "waitlist"})
    return {"offer_id": offer_id, "appointment_id": appointment["appointment_id"], "replayed": False}


def lambda_handler(event, context):
    """Routes waitlist and offer requests. Patient identity comes only from the authorizer."""
    method = str(event.get("httpMethod") or "").upper()
    path = appt.get_path(event)
    patient_id, role = appt.get_auth(event)
    try:
        if not patient_id:
            raise appt.ApiError(401, "Authentication is required")
        if role != "patient":
            raise appt.ApiError(403, "Only patients can use waitlists")
        if method == "POST" and path == "/waitlist":
            body = appt.parse_body(event)
            return appt.response(201, {"message": "Joined the waitlist",
                                       "entry": join(patient_id, body.get("slot_id"))})
        if method == "DELETE" and path == "/waitlist":
            body = appt.parse_body(event)
            return appt.response(200, {"message": "Left the waitlist",
                                       "entry": leave(patient_id, body.get("slot_id"))})
        if method == "GET" and path == "/patients/me/waitlists":
            return appt.response(200, {"waitlists": list_for_patient(patient_id)})
        parts = [p for p in path.split("/") if p]
        if method == "POST" and len(parts) == 3 and parts[0] == "waitlist-offers" and parts[2] == "accept":
            return appt.response(200, {"message": "Offer accepted",
                                       "result": accept(patient_id, parts[1])})
        raise appt.ApiError(404, "Route not found")
    except appt.ApiError as error:
        payload = {"message": error.message}
        if error.details:
            payload["details"] = error.details
        return appt.response(error.status_code, payload)
