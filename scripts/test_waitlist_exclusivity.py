"""Mandatory test 4 (deployed): one freed seat, three waitlisted patients, one live offer.

Needs three patient tokens and a doctor token able to cancel a slot. Skipped otherwise.
"""

import os
import time

import pytest

from integration_helpers import base_url, call, patient_tokens

pytestmark = pytest.mark.skipif(
    not base_url() or len(patient_tokens()) < 3 or not os.environ.get("MEDISLOT_DOCTOR_TOKEN")
    or not os.environ.get("MEDISLOT_FULL_SLOT_ID"),
    reason="Deployed environment with three patients and a full slot is not configured",
)


def test_one_freed_seat_yields_one_live_offer():
    slot_id = os.environ["MEDISLOT_FULL_SLOT_ID"]
    tokens = patient_tokens()[:3]
    for token in tokens:
        status, body = call("POST", "/waitlist", token=token, body={"slot_id": slot_id})
        assert status == 201, body

    status, body = call("POST", "/appointments/doctor-cancel-slot", token=os.environ["MEDISLOT_DOCTOR_TOKEN"],
                        body={"slot_time": os.environ["MEDISLOT_FULL_SLOT_TIME"]})
    assert status == 200, body

    time.sleep(10)  # SlotSeatFreed -> SQS -> hold offers Lambda
    live = 0
    for token in tokens:
        status, body = call("GET", "/patients/me/waitlists", token=token)
        assert status == 200
        live += sum(1 for entry in body["waitlists"] if entry["status"] == "OFFERED")
    assert live == 1, f"expected exactly one live offer, found {live}"
