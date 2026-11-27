"""Hold offers: issue, expire, pass on.

SlotSeatFreed (EventBridge rule) -> issue_for_freed_seat.
Every minute (EventBridge Scheduler) -> sweep_expired.

A freed seat becomes a held seat first, so normal booking cannot take it, and only
then is the offer created for the first waiting patient. Hold tokens tie each hold
to the event that created it, so a redelivered SlotSeatFreed cannot create a second
offer for the same freed seat.
"""

import logging
import time
import uuid
from datetime import datetime

from lambdas.appointment import appointment as appt
from lambdas.common import repos, seats
from lambdas.common.settings import get_setting
from lambdas.waitlist import waitlist as wl

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _publish(detail_type, detail):
    appt.publish_event(detail_type, detail)


def issue_offer(repo, store, *, slot_id, doctor_id, slot_time, hold_token, now_epoch, publish=_publish):
    """Hold one seat and offer it to the first waiting patient who can be moved to OFFERED.

    Returns the offer, or None when the seat was already processed or nobody is waiting.
    """
    key = {"doctorId": str(doctor_id), "slotTime": slot_time}
    waiters = repo.waiting_for_slot(slot_id)
    if not waiters:
        return None
    try:
        applied = seats.hold_seat(store, key, hold_token)
    except seats.SeatUnavailable:
        return None
    if not applied:
        return None  # this freed seat has already been processed

    offer_id = uuid.uuid4().hex
    appt.emit_metric("WaitlistPromotions", 1)
    hold_seconds = get_setting("waitlist/hold-seconds", 900)
    for waiter in waiters:
        if not repo.set_waitlist_status(waiter["patientId"], waiter["waitlistSortKey"], "WAITING", "OFFERED"):
            continue
        offer = {
            "offerId": offer_id,
            "patientId": str(waiter["patientId"]),
            "slotId": slot_id,
            "doctorId": str(doctor_id),
            "slotTime": slot_time,
            "offerStatus": "PENDING",
            "expiresAt": int(now_epoch + hold_seconds),
            "waitlistSortKey": waiter["waitlistSortKey"],
            "issuedAt": datetime.fromtimestamp(now_epoch).isoformat(timespec="seconds"),
            "holdToken": hold_token,
        }
        if repo.put_offer(offer):
            publish("WaitlistOfferIssued", {
                "offer_id": offer_id, "patient_id": offer["patientId"], "slot_id": slot_id,
                "expires_at": offer["expiresAt"],
            })
            return offer
        repo.set_waitlist_status(waiter["patientId"], waiter["waitlistSortKey"], "OFFERED", "WAITING")
        break

    seats.release_hold(store, key, f"hold-release:{hold_token}")
    return None


def issue_for_freed_seat(detail, *, repo=None, store=None, now_epoch=None, publish=_publish):
    if int(detail.get("freed_seats") or 0) <= 0:
        return None
    repo = repo or wl.get_repo()
    store = store or wl.get_store()
    now_epoch = now_epoch or int(time.time())
    return issue_offer(
        repo, store,
        slot_id=str(detail["slot_id"]),
        doctor_id=int(detail["doctor_id"]),
        slot_time=str(detail["slot_time"]),
        hold_token=f"freed:{detail['appointment_id']}",
        now_epoch=now_epoch,
        publish=publish,
    )


def sweep_expired(*, repo=None, store=None, now_epoch=None, publish=_publish):
    """Expire overdue offers, release their holds, and pass each seat to the next patient."""
    repo = repo or wl.get_repo()
    store = store or wl.get_store()
    now_epoch = now_epoch or int(time.time())
    passed = []
    for offer in repo.expired_pending_offers(now_epoch):
        if not repo.set_offer_status(offer["offerId"], "PENDING", "EXPIRED"):
            continue  # accepted or expired by another run
        key = {"doctorId": str(offer["doctorId"]), "slotTime": offer["slotTime"]}
        seats.release_hold(store, key, f"hold-release:{offer['holdToken']}")
        repo.set_waitlist_status(offer["patientId"], offer["waitlistSortKey"], "OFFERED", "EXPIRED")
        appt.emit_metric("OffersExpired", 1)
        publish("WaitlistOfferExpired", {"offer_id": offer["offerId"], "patient_id": offer["patientId"],
                                         "slot_id": offer["slotId"]})
        next_offer = issue_offer(
            repo, store,
            slot_id=offer["slotId"], doctor_id=offer["doctorId"], slot_time=offer["slotTime"],
            hold_token=f"pass:{offer['offerId']}", now_epoch=now_epoch, publish=publish,
        )
        if next_offer:
            passed.append(next_offer["offerId"])
    return passed


def lambda_handler(event, context):
    """SQS batch (SlotSeatFreed envelopes) or a scheduled sweep of expired offers."""
    import json
    from lambdas.common.structured_log import correlation_from, log_json

    correlation_id = correlation_from(event, context)
    if "Records" in event:
        failures = []
        for record in event["Records"]:
            try:
                envelope = json.loads(record["body"])
                if envelope.get("detail-type") != "SlotSeatFreed":
                    continue
                offer = issue_for_freed_seat(envelope.get("detail") or {})
                log_json(logger, "hold_offers", "issue_for_freed_seat", correlation_id,
                         "SUCCESS", offer_issued=offer is not None)
            except Exception:
                logger.exception("Hold offer issue failed messageId=%s", record.get("messageId"))
                failures.append({"itemIdentifier": record.get("messageId")})
        return {"batchItemFailures": failures}

    if event.get("action") == "sweep-holds":
        passed = sweep_expired()
        log_json(logger, "hold_offers", "sweep_expired", correlation_id, "SUCCESS", passed_on=len(passed))
        return {"passed_on": len(passed)}
    raise ValueError("Unsupported hold-offer event")
