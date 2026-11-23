#!/usr/bin/env python3
"""Live API replay test: same key must return the same RDS appointment."""
import json
import os
import urllib.error
import urllib.request

api_url = os.environ["MEDISLOT_API_URL"].rstrip("/")
token = os.environ["PATIENT_TOKEN"]
doctor_id = int(os.environ["DOCTOR_ID"])
slot_time = os.environ["SLOT_TIME"]
idem_key = os.environ.get("IDEMPOTENCY_KEY", "medislot-m5-idempotency-live-test")


def send():
    payload = json.dumps({"doctor_id": doctor_id, "slot_time": slot_time}).encode()
    request = urllib.request.Request(
        api_url + "/appointments",
        data=payload,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Idempotency-Key": idem_key,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode())


first_status, first_body = send()
second_status, second_body = send()

first_id = first_body.get("appointment", {}).get("appointment_id")
second_id = second_body.get("appointment", {}).get("appointment_id")

print("First response:", first_status, first_body)
print("Replay response:", second_status, second_body)

if first_status not in (200, 201) or second_status != 200 or not first_id or first_id != second_id:
    raise SystemExit("FAILED: replay did not return the original appointment")
print("PASSED: both requests returned the same appointment ID:", first_id)
