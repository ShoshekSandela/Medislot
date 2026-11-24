import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError

# PyMySQL is installed in CI/Lambda. A tiny import stub keeps the pure
# DynamoDB/refund unit tests runnable in minimal local Python environments.
try:
    import pymysql  # noqa: F401
except ImportError:
    import sys
    import types
    pymysql_stub = types.ModuleType("pymysql")
    pymysql_stub.err = types.SimpleNamespace(IntegrityError=type("IntegrityError", (Exception,), {}))
    sys.modules["pymysql"] = pymysql_stub

from lambdas.appointment import appointment


class FakeSlotTable:
    """Thread-safe in-memory table that enforces the same slot CAS conditions."""

    name = "test-slots"

    def __init__(self, capacity):
        self.lock = threading.Lock()
        self.item = {
            "doctorId": "42",
            "slotTime": "2026-12-01T09:00:00+05:30",
            "slotId": "42#7#3#2026-12-01T09:00:00+05:30",
            "capacity": capacity,
            "bookedCount": 0,
            "version": 1,
            "status": "AVAILABLE",
            "isBlackout": False,
        }

    def get_item(self, Key, ConsistentRead=False):
        with self.lock:
            return {"Item": dict(self.item)}

    def update_item(
        self,
        Key,
        UpdateExpression,
        ConditionExpression,
        ExpressionAttributeNames,
        ExpressionAttributeValues,
    ):
        with self.lock:
            values = ExpressionAttributeValues
            if (
                self.item["version"] != values[":version"]
                or self.item["bookedCount"] != values[":booked"]
                or self.item["bookedCount"] >= self.item["capacity"]
                or self.item["status"] != values[":available"]
                or self.item["isBlackout"] != values[":false"]
            ):
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "conflict"}},
                    "UpdateItem",
                )
            self.item["bookedCount"] += values[":one"]
            self.item["version"] += values[":one"]
            return {"Attributes": dict(self.item)}


@pytest.fixture
def quiet_aws(monkeypatch):
    monkeypatch.setattr(appointment, "emit_metric", lambda *args, **kwargs: None)
    monkeypatch.setattr(appointment.time, "sleep", lambda *_: None)


def test_capacity_three_allows_exactly_three_of_twenty(monkeypatch, quiet_aws):
    table = FakeSlotTable(capacity=3)
    monkeypatch.setattr(appointment, "slots_table", table)

    def attempt(_):
        try:
            appointment.reserve_slot(42, table.item["slotTime"])
            return "CONFIRMED"
        except appointment.ApiError as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(attempt, range(20)))

    assert results.count("CONFIRMED") == 3
    assert results.count(409) == 17
    assert table.item["bookedCount"] == 3
    assert table.item["bookedCount"] <= table.item["capacity"]


def test_refund_tier_boundaries_match_frozen_rules():
    tiers = [
        {"name": "FULL", "minimum_hours_before": Decimal("48"),
         "strictly_greater_than": True, "refund_percentage": Decimal("100")},
        {"name": "75_PERCENT", "minimum_hours_before": Decimal("24"),
         "refund_percentage": Decimal("75")},
        {"name": "50_PERCENT", "minimum_hours_before": Decimal("6"),
         "refund_percentage": Decimal("50")},
        {"name": "NONE", "minimum_hours_before": Decimal("0"),
         "refund_percentage": Decimal("0")},
    ]
    now = datetime(2026, 12, 1, 9, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    assert appointment.calculate_refund_tier(now + timedelta(hours=48, seconds=1), now, tiers)["refund_percentage"] == 100
    assert appointment.calculate_refund_tier(now + timedelta(hours=48), now, tiers)["refund_percentage"] == 75
    assert appointment.calculate_refund_tier(now + timedelta(hours=24), now, tiers)["refund_percentage"] == 75
    assert appointment.calculate_refund_tier(now + timedelta(hours=6), now, tiers)["refund_percentage"] == 50
    assert appointment.calculate_refund_tier(now + timedelta(hours=6) - timedelta(seconds=1), now, tiers)["refund_percentage"] == 0


class FakeIdempotencyTable:
    name = "test-idempotency"

    def __init__(self):
        self.item = None

    def put_item(self, Item, ConditionExpression):
        if self.item is not None:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                "PutItem",
            )
        self.item = dict(Item)

    def get_item(self, Key, ConsistentRead=False):
        return {"Item": dict(self.item)} if self.item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues):
        self.item["status"] = ExpressionAttributeValues[":complete"]
        self.item["response"] = ExpressionAttributeValues[":response"]


def test_idempotency_replay_returns_the_original_appointment(monkeypatch):
    table = FakeIdempotencyTable()
    monkeypatch.setattr(appointment, "idempotency_table", table)
    monkeypatch.setattr(appointment.time, "time", lambda: 1_800_000_000)

    idem_key = "test-idempotency-key"
    body_hash = appointment.request_hash(10, 42, "2026-12-01T09:00:00+05:30")

    assert appointment.start_idempotency(idem_key, 10, body_hash, "request-1") is None

    original = {
        "appointment_id": 123,
        "patient_id": 10,
        "doctor_id": 42,
        "slot_id": "42#7#3#2026-12-01T09:00:00+05:30",
        "status": "CONFIRMED",
    }
    appointment.complete_idempotency(idem_key, original)

    replay = appointment.start_idempotency(idem_key, 10, body_hash, "request-2")
    assert replay == original
    assert table.item["status"] == "COMPLETE"


def test_request_hash_is_stable_and_scoped_to_patient():
    first = appointment.request_hash(10, 42, "2026-12-01T09:00:00+05:30")
    same = appointment.request_hash(10, 42, "2026-12-01T09:00:00+05:30")
    other_patient = appointment.request_hash(11, 42, "2026-12-01T09:00:00+05:30")

    assert first == same
    assert first != other_patient
