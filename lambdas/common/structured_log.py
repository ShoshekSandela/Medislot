"""Structured JSON log lines with a correlation id, for CloudWatch Logs Insights.

The correlation id comes from the request (X-Correlation-Id header, or the saga
input), falling back to the Lambda request id. It is copied into every
downstream event so one business operation can be followed across log groups.
"""

import json
import logging
from datetime import datetime, timezone


def correlation_from(event, context=None):
    headers = (event or {}).get("headers") or {}
    for key in ("X-Correlation-Id", "x-correlation-id"):
        if headers.get(key):
            return str(headers[key])
    if isinstance(event, dict):
        for key in ("correlation_id",):
            if event.get(key):
                return str(event[key])
        inner = event.get("input")
        if isinstance(inner, dict) and inner.get("correlation_id"):
            return str(inner["correlation_id"])
    return getattr(context, "aws_request_id", "unknown")


def log_json(logger, service, operation, correlation_id, status, **fields):
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "correlation_id": correlation_id,
        "service": service,
        "operation": operation,
        "status": status,
        **fields,
    }
    logger.info(json.dumps(record, default=str))


def get_logger():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    return logger
