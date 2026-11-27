"""HTTP helpers for the deployed-environment integration tests (standard library only).

Required environment (tests are skipped when the base URL is missing):
  MEDISLOT_API_BASE_URL   e.g. https://<id>.execute-api.ap-southeast-2.amazonaws.com/dev
  MEDISLOT_PATIENT_TOKENS comma-separated bearer tokens for distinct test patients
  MEDISLOT_DOCTOR_TOKEN   bearer token for the test doctor
"""

import json
import os
import urllib.error
import urllib.request


def base_url():
    return os.environ.get("MEDISLOT_API_BASE_URL", "").rstrip("/")


def patient_tokens():
    return [token.strip() for token in os.environ.get("MEDISLOT_PATIENT_TOKENS", "").split(",") if token.strip()]


def call(method, path, token=None, body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(base_url() + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        raw = error.read() or b"{}"
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, {"raw": raw.decode("utf-8", "replace")}
