"""Centralized HTTPS client for the MediSlot API Gateway."""
import json as jsonlib
import logging
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from flask import current_app, session, request as flask_request

log = logging.getLogger("medislot.api")


class APIError(Exception):
    def __init__(self, message, status=0, correlation_id=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.correlation_id = correlation_id


def _safe_json(response):
    try:
        value = response.json()
        if isinstance(value, (dict, list)):
            return value
        return {"message": "Unexpected API response"}
    except (ValueError, requests.exceptions.JSONDecodeError):
        body = (response.text or "").strip()
        return {"message": body[:300] if body else "Empty response from API"}


def _log(level, correlation_id, path, status, started):
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "correlation_id": correlation_id,
        "route": getattr(flask_request, "path", ""),
        "upstream_path": path,
        "upstream_status": status,
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }
    getattr(log, level)(jsonlib.dumps(record, separators=(",", ":")))


def request(method, path, *, json=None, params=None, auth=True, retry=True, files=None):
    """Call one documented API route; refresh once after a protected 401."""
    from .config import FEATURE_FLAGS

    if path == "/doctors/appointments":
        flag = "appointments"
    elif path.startswith("/doctors/slots/"):
        flag = "cancel_slot"
    elif path.endswith("/outcome"):
        flag = "appointment_outcome"
    else:
        flag = None

    if flag and not FEATURE_FLAGS.get(flag, False):
        raise APIError("This feature is not available yet.", 501)

    correlation_id = str(uuid.uuid4())
    started = time.perf_counter()
    headers = {
        "X-Correlation-Id": correlation_id,
        "Accept": "application/json",
    }
    token = session.get("accessToken") if auth else None
    if token:
        headers["Authorization"] = f"Bearer {token}"

    base_url = current_app.config.get("API_BASE_URL", "").rstrip("/")
    if not base_url:
        raise APIError("API_BASE_URL is not configured. Set it to your deployed API Gateway URL.", 0, correlation_id)
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise APIError("API_BASE_URL must be a valid HTTPS URL.", 0, correlation_id)

    try:
        response = requests.request(
            method,
            f"{base_url}{path}",
            params=params,
            json=json,
            headers=headers,
            timeout=current_app.config["REQUEST_TIMEOUT_SECONDS"],
            files=files,
        )
    except requests.RequestException as exc:
        _log("error", correlation_id, path, None, started)
        raise APIError("Could not reach MediSlot API. Check API_BASE_URL and your network connection.", 0, correlation_id) from exc

    # Keep the dashboard message generic, but record AWS request identifiers and
    # safe response metadata in the server log to make upstream failures traceable.
    _log(
        "error" if response.status_code >= 500 else "info",
        correlation_id,
        path,
        response.status_code,
        started,
    )
    if response.status_code >= 500:
        gateway_error = {
            "correlation_id": correlation_id,
            "method": method.upper(),
            "upstream_path": path,
            "upstream_status": response.status_code,
            "api_gateway_request_id": response.headers.get("x-amzn-RequestId"),
            "api_gateway_extended_request_id": response.headers.get("x-amz-apigw-id"),
            "lambda_request_id": response.headers.get("x-amzn-trace-id"),
        }
        log.error(jsonlib.dumps(gateway_error, separators=(",", ":")))

    if response.status_code == 401 and auth and retry:
        refresh_token = session.get("refreshToken")
        if refresh_token:
            try:
                refreshed = request(
                    "POST",
                    "/auth/refresh",
                    json={"refreshToken": refresh_token},
                    auth=False,
                    retry=False,
                )
                if isinstance(refreshed, dict) and refreshed.get("accessToken"):
                    session["accessToken"] = refreshed["accessToken"]
                    session["refreshToken"] = refreshed.get("refreshToken", refresh_token)
                    if refreshed.get("expiresIn"):
                        session["expiresAt"] = int(time.time()) + int(refreshed["expiresIn"])
                    return request(method, path, json=json, params=params, auth=True, retry=False, files=files)
            except APIError as refresh_error:
                if refresh_error.status not in (401, 403):
                    raise

        session.clear()
        raise APIError("Session expired, please sign in again.", 401, correlation_id)

    data = _safe_json(response)
    if not 200 <= response.status_code < 300:
        message = data.get("message") if isinstance(data, dict) else None
        if response.status_code >= 500:
            message = f"MediSlot is temporarily unavailable. Reference: {correlation_id}"
        raise APIError(message or f"Request failed ({response.status_code}).", response.status_code, correlation_id)
    return data


def get(path, params=None):
    return request("GET", path, params=params)


def post(path, data, auth=True):
    return request("POST", path, json=data, auth=auth)


def put(path, data):
    return request("PUT", path, json=data)


def delete(path):
    return request("DELETE", path)


def upload_to_presigned(url, content_type, blob):
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or not parsed.netloc:
        raise APIError("The photo upload URL is invalid or is not HTTPS.")
    try:
        response = requests.put(
            url,
            data=blob,
            headers={"Content-Type": content_type},
            timeout=current_app.config["REQUEST_TIMEOUT_SECONDS"],
        )
        if not 200 <= response.status_code < 300:
            raise APIError("Photo storage upload failed.", response.status_code)
        return True
    except requests.RequestException as exc:
        raise APIError("Photo storage upload failed.") from exc


# These documented future operations remain disabled until the backend exposes them.
def get_appointments(params=None):
    return get("/doctors/appointments", params=params)


def cancel_slot(slot_id):
    return delete(f"/doctors/slots/{slot_id}")


def set_appointment_outcome(appointment_id, outcome):
    return post(f"/doctors/appointments/{appointment_id}/outcome", {"outcome": outcome})
