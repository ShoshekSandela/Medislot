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
            return "BOOKED"
        except appointment.ApiError as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(attempt, range(20)))

    assert results.count("BOOKED") == 3
    assert results.count(409) == 17
    assert table.item["bookedCount"] == 3
    assert table.item["bookedCount"] <= table.item["capacity"]


def test_refund_tier_cutoff_is_inclusive():
    tiers = [
        {"name": "FULL", "minimum_hours_before": Decimal("48"), "refund_percentage": Decimal("100")},
        {"name": "PARTIAL", "minimum_hours_before": Decimal("24"), "refund_percentage": Decimal("50")},
        {"name": "NONE", "minimum_hours_before": Decimal("0"), "refund_percentage": Decimal("0")},
    ]
    now = datetime(2026, 12, 1, 9, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    exact_48 = appointment.calculate_refund_tier(now + timedelta(hours=48), now, tiers)
    just_under_48 = appointment.calculate_refund_tier(
        now + timedelta(hours=48) - timedelta(seconds=1), now, tiers
    )
    exact_24 = appointment.calculate_refund_tier(now + timedelta(hours=24), now, tiers)
    just_under_24 = appointment.calculate_refund_tier(
        now + timedelta(hours=24) - timedelta(seconds=1), now, tiers
    )
    exact_0 = appointment.calculate_refund_tier(now, now, tiers)

    assert exact_48["name"] == "FULL"
    assert just_under_48["name"] == "PARTIAL"
    assert exact_24["name"] == "PARTIAL"
    assert just_under_24["name"] == "NONE"
    assert exact_0["name"] == "NONE"


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
        "status": "BOOKED",
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



def test_booked_count_value_supports_legacy_map_and_nested_count():
    assert appointment.booked_count_value({"appointment-1": True, "appointment-2": True}) == 2
    assert appointment.booked_count_value({"count": 3}) == 3
    assert appointment.booked_count_value({"appointments": ["a", "b", "c"]}) == 3
    assert appointment.booked_count_value(2) == 2



def test_cancelled_seat_release_converts_legacy_map_counter_once(monkeypatch):
    class SlotTable:
        name = "slots"

        def __init__(self):
            self.item = {
                "doctorId": "42",
                "slotTime": "2026-12-01T09:00:00+05:30",
                "slotId": "42#7#3#2026-12-01T09:00:00+05:30",
                "bookedCount": {"appointment-1": True, "appointment-2": True},
                "status": "FULLY_BOOKED",
                "version": 2,
            }

        def get_item(self, Key, ConsistentRead=False):
            return {"Item": dict(self.item)}

    class IdemTable:
        name = "idempotency"

        def __init__(self):
            self.items = {}

        def get_item(self, Key, ConsistentRead=False):
            item = self.items.get(Key["idemKey"])
            return {"Item": dict(item)} if item else {}

    class FakeClient:
        def __init__(self, slots, idem):
            self.slots = slots
            self.idem = idem

        def transact_write_items(self, TransactItems):
            update = TransactItems[0]["Update"]
            # DynamoDB cannot compare a legacy Map counter with a number.
            # The production transaction must use existence + version CAS only.
            assert ">" not in update["ConditionExpression"]
            values = update["ExpressionAttributeValues"]
            assert update["ConditionExpression"].startswith("attribute_exists(#booked)")
            if self.slots.item["version"] != int(values[":version"]["N"]):
                raise ClientError(
                    {"Error": {"Code": "TransactionCanceledException", "Message": "condition"}},
                    "TransactWriteItems",
                )
            self.slots.item["bookedCount"] = int(values[":new_count"]["N"])
            self.slots.item["status"] = update["ExpressionAttributeValues"][":new_status"]["S"]
            self.slots.item["version"] += 1
            put = TransactItems[1]["Put"]["Item"]
            key = put["idemKey"]["S"]
            if key in self.idem.items:
                raise ClientError(
                    {"Error": {"Code": "TransactionCanceledException", "Message": "exists"}},
                    "TransactWriteItems",
                )
            self.idem.items[key] = {"idemKey": key, "status": "COMPLETE"}

    slots = SlotTable()
    idem = IdemTable()
    client = FakeClient(slots, idem)
    monkeypatch.setattr(appointment, "slots_table", slots)
    monkeypatch.setattr(appointment, "idempotency_table", idem)
    monkeypatch.setattr(
        appointment,
        "dynamodb",
        type("DynamoResource", (), {"meta": type("Meta", (), {"client": client})()})(),
    )

    released = appointment.release_cancelled_seat_once(
        77, 42, slots.item["slotTime"], slots.item["slotId"]
    )
    assert released is True
    assert slots.item["bookedCount"] == 1
    assert slots.item["status"] == "AVAILABLE"

    retried = appointment.release_cancelled_seat_once(
        77, 42, slots.item["slotTime"], slots.item["slotId"]
    )
    assert retried is False
    assert slots.item["bookedCount"] == 1
