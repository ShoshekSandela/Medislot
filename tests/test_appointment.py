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
            "seatRevision": 0,
            "status": "AVAILABLE",
            "isBlackout": False,
        }

    def get_item(self, Key, ConsistentRead=False):
        with self.lock:
            item = dict(self.item)
            if "releasedAppointments" in item:
                item["releasedAppointments"] = set(item["releasedAppointments"])
            return {"Item": item}

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
            current_revision = self.item.get("seatRevision")
            revision_ok = (
                current_revision == values.get(":revision")
                if current_revision is not None
                else "attribute_not_exists(#revision)" in ConditionExpression
            )
            if ":token" in values:  # atomic cancellation release
                token = values[":token"]
                released = set(self.item.get("releasedAppointments", set()))
                valid = (
                    self.item["bookedCount"] == values[":booked"]
                    and revision_ok
                    and token not in released
                    and self.item["bookedCount"] > 0
                )
                if not valid:
                    raise ClientError(
                        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "conflict"}},
                        "UpdateItem",
                    )
                self.item["bookedCount"] = values[":next_booked"]
                self.item["status"] = values[":next_status"]
                self.item["seatRevision"] = values[":next_revision"]
                released.update(values[":appt"])
                self.item["releasedAppointments"] = released
                return {"Attributes": dict(self.item)}

            valid = (
                self.item["bookedCount"] == values[":current_booked"]
                and self.item["bookedCount"] < values[":capacity"]
                and revision_ok
                and self.item["status"] in {
                    values[":available"], values[":open"], values[":partial"],
                    values[":booked"], values[":full"], values[":full_booked"],
                    values[":fully_booked"],
                }
                and self.item["isBlackout"] == values[":false"]
            )
            if not valid:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "conflict"}},
                    "UpdateItem",
                )
            self.item["bookedCount"] = values[":next_booked"]
            self.item["status"] = values[":next_status"]
            self.item["seatRevision"] = values[":next_revision"]
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


def test_partial_slot_remains_bookable_and_status_tracks_capacity(monkeypatch, quiet_aws):
    table = FakeSlotTable(capacity=3)
    table.item["bookedCount"] = 1
    table.item["status"] = "PARTIALLY_BOOKED"
    monkeypatch.setattr(appointment, "slots_table", table)

    appointment.reserve_slot(42, table.item["slotTime"])
    assert table.item["bookedCount"] == 2
    assert table.item["status"] == "PARTIALLY_BOOKED"

    appointment.reserve_slot(42, table.item["slotTime"])
    assert table.item["bookedCount"] == 3
    assert table.item["status"] == "FULLY_BOOKED"



def test_health_camp_cancel_releases_one_seat_and_another_patient_can_book(monkeypatch, quiet_aws):
    table = FakeSlotTable(capacity=10)
    monkeypatch.setattr(appointment, "slots_table", table)
    monkeypatch.setattr(appointment, "idempotency_table", EmptyMarkerTable())

    for _ in range(10):
        appointment.reserve_slot(42, table.item["slotTime"])

    assert table.item["bookedCount"] == 10
    assert table.item["status"] == "FULLY_BOOKED"
    with pytest.raises(appointment.ApiError) as full_error:
        appointment.reserve_slot(42, table.item["slotTime"])
    assert full_error.value.status_code == 409

    assert appointment.release_cancelled_seat_once(
        901, 42, table.item["slotTime"], table.item["slotId"], cancellation_type="PATIENT"
    ) is True
    assert table.item["bookedCount"] == 9
    assert table.item["status"] == "PARTIALLY_BOOKED"

    # A different patient can immediately reserve the released seat.
    appointment.reserve_slot(42, table.item["slotTime"])
    assert table.item["bookedCount"] == 10
    assert table.item["status"] == "FULLY_BOOKED"

    # Retrying the same cancellation cannot free a second seat.
    assert appointment.release_cancelled_seat_once(
        901, 42, table.item["slotTime"], table.item["slotId"], cancellation_type="PATIENT"
    ) is False
    assert table.item["bookedCount"] == 10


def test_booking_without_existing_revision_still_uses_count_compare_and_swap(monkeypatch, quiet_aws):
    table = FakeSlotTable(capacity=10)
    table.item.pop("seatRevision", None)
    monkeypatch.setattr(appointment, "slots_table", table)

    def attempt(_):
        try:
            appointment.reserve_slot(42, table.item["slotTime"])
            return "BOOKED"
        except appointment.ApiError as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(attempt, range(20)))

    assert results.count("BOOKED") == 10
    assert table.item["bookedCount"] == 10
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



def test_cancellation_uses_exact_slot_time_from_persisted_slot_id():
    appointment_row = {
        "appointment_id": 77,
        # MySQL DATETIME may be returned without timezone information.
        "start_at_ist": datetime(2026, 12, 1, 9, 0, 0),
        "slot_id": "42#7#3#2026-12-01T09:00:00+05:30",
    }

    assert appointment.appointment_slot_time(appointment_row) == (
        "2026-12-01T09:00:00+05:30"
    )


def test_cancellation_slot_time_falls_back_to_mysql_start_time():
    appointment_row = {
        "appointment_id": 77,
        "start_at_ist": datetime(2026, 12, 1, 9, 0, 0),
        "slot_id": "legacy-slot-id",
    }

    assert appointment.appointment_slot_time(appointment_row) == (
        "2026-12-01T09:00:00+05:30"
    )


class ReleaseSlotTable:
    """In-memory slot table that enforces the release's update condition."""

    name = "slots"

    def __init__(self, booked, capacity=1, status="FULLY_BOOKED", revision=1):
        self.lock = threading.Lock()
        self.item = {
            "doctorId": "42",
            "slotTime": "2026-12-01T09:00:00+05:30",
            "capacity": capacity,
            "bookedCount": booked,
            "seatRevision": revision,
            "status": status,
            "isBlackout": False,
        }

    def get_item(self, Key, ConsistentRead=False):
        with self.lock:
            item = dict(self.item)
            if "releasedAppointments" in item:
                item["releasedAppointments"] = set(item["releasedAppointments"])
            return {"Item": item}

    def update_item(self, Key, UpdateExpression, ConditionExpression,
                    ExpressionAttributeNames, ExpressionAttributeValues):
        v = ExpressionAttributeValues
        with self.lock:
            revision_ok = (
                (":revision" not in v and "seatRevision" not in self.item)
                or self.item.get("seatRevision") == v.get(":revision")
            )
            if (
                self.item["bookedCount"] != v[":booked"]
                or not revision_ok
                or v[":token"] in self.item.get("releasedAppointments", set())
            ):
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "conflict"}},
                    "UpdateItem",
                )
            self.item["bookedCount"] = v[":next_booked"]
            self.item["status"] = v[":next_status"]
            self.item["seatRevision"] = v[":next_revision"]
            self.item["releasedAppointments"] = (
                set(self.item.get("releasedAppointments", set())) | set(v[":appt"])
            )
            return {}


class EmptyMarkerTable:
    name = "idempotency"

    def __init__(self, markers=()):
        self.markers = set(markers)

    def get_item(self, Key, ConsistentRead=False):
        key = Key["idemKey"]
        return {"Item": {"idemKey": key}} if key in self.markers else {}


def _use_tables(monkeypatch, slots, markers=()):
    monkeypatch.setattr(appointment, "slots_table", slots)
    monkeypatch.setattr(appointment, "idempotency_table", EmptyMarkerTable(markers))


def test_release_frees_one_seat_and_second_call_is_a_no_op(monkeypatch, quiet_aws):
    slots = ReleaseSlotTable(booked=2, capacity=3, status="PARTIALLY_BOOKED", revision=4)
    _use_tables(monkeypatch, slots)

    assert appointment.release_cancelled_seat_once(
        77, 42, slots.item["slotTime"], "slot", cancellation_type="PATIENT"
    ) is True
    assert slots.item["bookedCount"] == 1
    assert slots.item["status"] == "PARTIALLY_BOOKED"
    assert slots.item["seatRevision"] == 5
    assert slots.item["releasedAppointments"] == {"77"}

    assert appointment.release_cancelled_seat_once(
        77, 42, slots.item["slotTime"], "slot", cancellation_type="PATIENT"
    ) is False
    assert slots.item["bookedCount"] == 1


def test_duplicate_concurrent_releases_free_exactly_one_seat(monkeypatch, quiet_aws):
    slots = ReleaseSlotTable(booked=1)
    _use_tables(monkeypatch, slots)

    def attempt(_):
        return appointment.release_cancelled_seat_once(
            77, 42, slots.item["slotTime"], "slot", cancellation_type="PATIENT"
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))

    assert results.count(True) == 1
    assert slots.item["bookedCount"] == 0
    assert slots.item["status"] == "AVAILABLE"


def test_different_appointments_on_one_slot_each_free_their_own_seat(monkeypatch, quiet_aws):
    slots = ReleaseSlotTable(booked=2, capacity=3, status="PARTIALLY_BOOKED")
    _use_tables(monkeypatch, slots)

    assert appointment.release_cancelled_seat_once(21, 42, slots.item["slotTime"], "slot") is True
    assert appointment.release_cancelled_seat_once(22, 42, slots.item["slotTime"], "slot") is True
    assert slots.item["bookedCount"] == 0
    assert slots.item["releasedAppointments"] == {"21", "22"}


def test_legacy_marker_prevents_releasing_an_already_released_seat(monkeypatch, quiet_aws):
    slots = ReleaseSlotTable(booked=1)
    _use_tables(monkeypatch, slots, markers={"SEAT_RELEASE#55"})

    assert appointment.release_cancelled_seat_once(55, 42, slots.item["slotTime"], "slot") is False
    assert slots.item["bookedCount"] == 1


def test_release_never_goes_below_zero(monkeypatch, quiet_aws):
    slots = ReleaseSlotTable(booked=0, status="AVAILABLE")
    _use_tables(monkeypatch, slots)

    assert appointment.release_cancelled_seat_once(77, 42, slots.item["slotTime"], "slot") is False
    assert slots.item["bookedCount"] == 0


def test_doctor_cancellation_releases_count_but_keeps_slot_blocked(monkeypatch, quiet_aws):
    slots = ReleaseSlotTable(booked=2, capacity=3, status="PARTIALLY_BOOKED")
    _use_tables(monkeypatch, slots)

    assert appointment.release_cancelled_seat_once(
        88, 42, slots.item["slotTime"], "slot", cancellation_type="DOCTOR"
    ) is True
    assert slots.item["bookedCount"] == 1
    assert slots.item["status"] == "SLOT_CANCELLED_BY_DOCTOR"

    assert appointment.release_cancelled_seat_once(
        88, 42, slots.item["slotTime"], "slot", cancellation_type="DOCTOR"
    ) is False
    assert slots.item["status"] == "SLOT_CANCELLED_BY_DOCTOR"


def test_legacy_map_counter_is_refused_not_guessed(monkeypatch, quiet_aws):
    slots = ReleaseSlotTable(booked=1)
    slots.item["bookedCount"] = {"appointment-1": True}
    _use_tables(monkeypatch, slots)

    with pytest.raises(appointment.ApiError) as exc:
        appointment.release_cancelled_seat_once(77, 42, slots.item["slotTime"], "slot")
    assert exc.value.status_code == 409
    assert slots.item["bookedCount"] == {"appointment-1": True}


@pytest.mark.parametrize(
    ("booked", "capacity", "expected"),
    [
        (0, 1, "AVAILABLE"),
        (1, 1, "FULLY_BOOKED"),
        (1, 3, "PARTIALLY_BOOKED"),
        (3, 3, "FULLY_BOOKED"),
    ],
)
def test_slot_status_is_derived_from_booked_count(booked, capacity, expected):
    assert appointment.slot_status_for_counts(booked, capacity) == expected


class _FakeCursor:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        return None

    def fetchone(self):
        return self.row


class _FakeConnection:
    def __init__(self, row):
        self.row = row
        self.closed = False

    def cursor(self):
        return _FakeCursor(self.row)

    def close(self):
        self.closed = True


def _appointment_row(**overrides):
    row = {
        "appointment_id": 29,
        "patient_id": 2,
        "doctor_id": 2,
        "slot_id": "2#2#1#2026-12-01T10:15:00+05:30",
        "start_at_ist": datetime(2026, 12, 1, 10, 15),
        "status": "CANCELLED_BY_PATIENT",
    }
    row.update(overrides)
    return row


def test_retry_seat_release_frees_seat_once_and_publishes_waitlist_event(monkeypatch):
    released_calls = []
    published = []

    def fake_release(appointment_id, doctor_id, slot_time, slot_id, cancellation_type="PATIENT"):
        released_calls.append((appointment_id, doctor_id, slot_time, slot_id, cancellation_type))
        # First call frees the seat; a repeat finds the marker and frees nothing.
        return len(released_calls) == 1

    monkeypatch.setattr(appointment, "connect_db", lambda: _FakeConnection(_appointment_row()))
    monkeypatch.setattr(appointment, "release_cancelled_seat_once", fake_release)
    monkeypatch.setattr(
        appointment, "publish_event",
        lambda detail_type, detail: published.append((detail_type, detail)),
    )

    first = appointment.retry_seat_release(29, 2, "patient")
    second = appointment.retry_seat_release(29, 2, "patient")

    assert first == {"appointment_id": 29, "seat_released_now": True, "already_released": False}
    assert second == {"appointment_id": 29, "seat_released_now": False, "already_released": True}
    assert released_calls[0] == (
        29, 2, "2026-12-01T10:15:00+05:30", "2#2#1#2026-12-01T10:15:00+05:30", "PATIENT"
    )
    # Only the call that actually freed a seat publishes SlotSeatFreed.
    assert [name for name, _ in published] == ["SlotSeatFreed"]
    assert published[0][1]["freed_seats"] == 1


def test_retry_seat_release_rejects_other_patients_and_doctors(monkeypatch):
    monkeypatch.setattr(appointment, "connect_db", lambda: _FakeConnection(_appointment_row()))
    monkeypatch.setattr(
        appointment, "release_cancelled_seat_once",
        lambda *a, **k: pytest.fail("must not release another user's seat"),
    )

    with pytest.raises(appointment.ApiError) as patient_exc:
        appointment.retry_seat_release(29, 99, "patient")
    assert patient_exc.value.status_code == 404

    with pytest.raises(appointment.ApiError) as doctor_exc:
        appointment.retry_seat_release(29, 77, "doctor")
    assert doctor_exc.value.status_code == 404


def test_retry_seat_release_only_applies_to_patient_cancellations(monkeypatch):
    monkeypatch.setattr(
        appointment, "connect_db",
        lambda: _FakeConnection(_appointment_row(status="CONFIRMED")),
    )
    monkeypatch.setattr(
        appointment, "release_cancelled_seat_once",
        lambda *a, **k: pytest.fail("a live booking must never release a seat"),
    )

    with pytest.raises(appointment.ApiError) as exc:
        appointment.retry_seat_release(29, 2, "patient")
    assert exc.value.status_code == 409


def test_retry_seat_release_reports_failure_as_retryable_503(monkeypatch):
    monkeypatch.setattr(appointment, "connect_db", lambda: _FakeConnection(_appointment_row()))

    def failing_release(*a, **k):
        raise RuntimeError("dynamodb unavailable")

    monkeypatch.setattr(appointment, "release_cancelled_seat_once", failing_release)

    with pytest.raises(appointment.ApiError) as exc:
        appointment.retry_seat_release(29, 2, "patient")
    assert exc.value.status_code == 503


class OutcomeCursor:
    def __init__(self, row):
        self.row = dict(row)
        self.rowcount = 0
        self.last_sql = ""

    def execute(self, sql, params):
        self.last_sql = sql
        if "SELECT appointment_id" in sql and "FOR UPDATE" in sql:
            self.rowcount = 1
        elif "UPDATE appointments" in sql:
            outcome, outcome_at, appointment_id, doctor_id = params
            if (
                self.row["appointment_id"] == appointment_id
                and self.row["doctor_id"] == doctor_id
                and self.row["status"] == "CONFIRMED"
            ):
                self.row["status"] = outcome
                self.row["outcome_at_ist"] = outcome_at
                self.rowcount = 1
            else:
                self.rowcount = 0

    def fetchone(self):
        return dict(self.row)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class OutcomeConnection:
    def __init__(self, row):
        self.cursor_instance = OutcomeCursor(row)
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


@pytest.mark.parametrize("outcome", ["COMPLETED", "NO_SHOW"])
def test_doctor_can_record_outcome_without_changing_slot_seats(monkeypatch, outcome):
    now = datetime.now(appointment.IST).replace(tzinfo=None)
    connection = OutcomeConnection({
        "appointment_id": 71,
        "doctor_id": 42,
        "start_at_ist": now - timedelta(minutes=1),
        "end_at_ist": now + timedelta(minutes=59),
        "status": "CONFIRMED",
    })
    monkeypatch.setattr(appointment, "connect_db", lambda: connection)

    result = appointment.mark_appointment_outcome(71, 42, outcome)

    assert result["status"] == outcome
    assert connection.cursor_instance.row["status"] == outcome
    assert connection.cursor_instance.row["outcome_at_ist"] is not None
    assert connection.committed is True
    assert connection.closed is True
    assert "UPDATE appointments" in connection.cursor_instance.last_sql
    # No DynamoDB seat release or cancellation code is invoked by this operation.


def test_doctor_cannot_mark_outcome_before_appointment_starts(monkeypatch):
    now = datetime.now(appointment.IST).replace(tzinfo=None)
    connection = OutcomeConnection({
        "appointment_id": 72,
        "doctor_id": 42,
        "start_at_ist": now + timedelta(minutes=10),
        "end_at_ist": now + timedelta(minutes=70),
        "status": "CONFIRMED",
    })
    monkeypatch.setattr(appointment, "connect_db", lambda: connection)

    with pytest.raises(appointment.ApiError) as exc:
        appointment.mark_appointment_outcome(72, 42, "NO_SHOW")
    assert exc.value.status_code == 409
    assert connection.rolled_back is True
    assert connection.cursor_instance.row["status"] == "CONFIRMED"


def test_only_confirmed_appointments_can_receive_an_outcome(monkeypatch):
    now = datetime.now(appointment.IST).replace(tzinfo=None)
    connection = OutcomeConnection({
        "appointment_id": 73,
        "doctor_id": 42,
        "start_at_ist": now - timedelta(minutes=10),
        "end_at_ist": now + timedelta(minutes=50),
        "status": "CANCELLED_BY_PATIENT",
    })
    monkeypatch.setattr(appointment, "connect_db", lambda: connection)

    with pytest.raises(appointment.ApiError) as exc:
        appointment.mark_appointment_outcome(73, 42, "COMPLETED")
    assert exc.value.status_code == 409
    assert connection.cursor_instance.row["status"] == "CANCELLED_BY_PATIENT"


def test_outcome_rejects_unknown_values():
    with pytest.raises(appointment.ApiError) as exc:
        appointment.mark_appointment_outcome(73, 42, "CANCELLED")
    assert exc.value.status_code == 400
