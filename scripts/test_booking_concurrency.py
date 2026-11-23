#!/usr/bin/env python3
"""Live API concurrency check for a fresh capacity-3 test slot.

Required environment variables:
  MEDISLOT_API_URL  e.g. https://xxxx.execute-api.ap-southeast-2.amazonaws.com/dev
  PATIENT_TOKENS_JSON JSON array of at least 20 valid patient JWTs (distinct patients)
  DOCTOR_ID         doctor owning the fresh slot
  SLOT_TIME         ISO-8601 IST time, e.g. 2026-12-01T09:00:00+05:30

Use a fresh slot with capacity 3 and no existing bookings.
"""
import concurrent.futures
import json
import os
import uuid
import urllib.error
import urllib.request

api_url = os.environ["MEDISLOT_API_URL"].rstrip("/")
patient_tokens = json.loads(os.environ["PATIENT_TOKENS_JSON"])
doctor_id = int(os.environ["DOCTOR_ID"])
slot_time = os.environ["SLOT_TIME"]
request_count = int(os.environ.get("REQUEST_COUNT", "20"))


def send_booking(index):
    payload = json.dumps({"doctor_id": doctor_id, "slot_time": slot_time}).encode()
    token = patient_tokens[index % len(patient_tokens)]
    request = urllib.request.Request(
        api_url + "/appointments",
        data=payload,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Idempotency-Key": f"milestone5-{uuid.uuid4()}-{index}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


with concurrent.futures.ThreadPoolExecutor(max_workers=request_count) as executor:
    results = list(executor.map(send_booking, range(request_count)))

successes = [item for item in results if item[0] == 201]
conflicts = [item for item in results if item[0] == 409]
unexpected = [item for item in results if item[0] not in (201, 409)]

print("Requests:", request_count)
print("Successful bookings:", len(successes))
print("409 conflicts:", len(conflicts))
print("Unexpected responses:", len(unexpected))
for status, body in unexpected:
    print("Unexpected:", status, body)

if len(patient_tokens) < request_count:
    raise SystemExit("Provide one distinct patient token per request to avoid patient-level duplicate/limit rules")
if len(successes) != 3 or len(unexpected) != 0:
    raise SystemExit("FAILED: expected exactly 3 successful bookings and all other requests to return 409")
print("PASSED: slot capacity was enforced under concurrent requests")
