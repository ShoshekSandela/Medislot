"""Reschedule saga: happy path, every compensation path, rejection rules, and
replay safety. The saga driver below mirrors statemachines/reschedule-saga.asl.json
step by step."""

from datetime import datetime, timedelta

import pytest

from lambdas.appointment import appointment as appt
from lambdas.common import seats
from lambdas.common.seats import MemorySlotStore
from lambdas.reschedule import reschedule as rs

IST = appt.IST
DOCTOR = "42"


def iso(dt):
    return dt.replace(tzinfo=IST).isoformat(timespec="seconds")


NOW = datetime.now(IST).replace(tzinfo=None)
ORIGINAL_START = NOW + timedelta(days=3)
TARGET_START = NOW + timedelta(days=4)
ORIGINAL_KEY = {"doctorId": DOCTOR, "slotTime": iso(ORIGINAL_START)}
TARGET_KEY = {"doctorId": DOCTOR, "slotTime": iso(TARGET_START)}
ORIGINAL_SLOT_ID = f"{DOCTOR}#7#3#{iso(ORIGINAL_START)}"
TARGET_SLOT_ID = f"{DOCTOR}#7#3#{iso(TARGET_START)}"


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.result = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        sql_norm = " ".join(sql.split())
        if sql_norm.startswith("SELECT * FROM appointments"):
            row = self.db.rows.get(params[0])
            self.result = dict(row) if row else None
        elif sql_norm.startswith("SELECT slot_id, reschedule_count, status FROM appointments"):
            row = self.db.rows.get(params[0])
            self.result = dict(row) if row else None
        elif sql_norm.startswith("UPDATE appointments"):
            if self.db.fail_commit:
                self.db.fail_commit = False
                raise RuntimeError("injected RDS failure during commit")
            slot_id, hospital_id, start, end, appointment_id, expected_count = params
            row = self.db.rows[appointment_id]
            if row["status"] == "CONFIRMED" and row["reschedule_count"] == expected_count:
                row.update(slot_id=slot_id, hospital_id=hospital_id, start_at_ist=start,
                           end_at_ist=end, reschedule_count=expected_count + 1)
                self.rowcount = 1
            else:
                self.rowcount = 0

    def fetchone(self):
        return self.result


class FakeDB:
    def __init__(self, appointment_row):
        self.rows = {appointment_row["appointment_id"]: appointment_row}
        self.fail_commit = False
        self.commits = 0

    def connect(self):
        db = self

        class Connection:
            def cursor(self):
                return FakeCursor(db)

            def commit(self):
                db.commits += 1

            def rollback(self):
                pass

            def close(self):
                pass
        return Connection()


def appointment_row(start=ORIGINAL_START, count=0, patient_id=7):
    return {
        "appointment_id": 1, "patient_id": patient_id, "doctor_id": int(DOCTOR),
        "hospital_id": 7, "consultation_type_id": 3, "slot_id": ORIGINAL_SLOT_ID,
        "start_at_ist": start, "end_at_ist": start + timedelta(minutes=30),
        "status": "CONFIRMED", "reschedule_count": count, "consultation_fee": "500.00",
    }


def slot(key, slot_id, booked=0, capacity=1, status="AVAILABLE"):
    start = datetime.fromisoformat(key["slotTime"]).replace(tzinfo=None)
    return {
        "slotId": slot_id, "capacity": capacity, "bookedCount": booked, "heldCount": 0,
        "seatRevision": 0, "status": status, "isBlackout": False, "consultationTypeId": "3",
        "hospitalId": 7, "endTime": iso(start + timedelta(minutes=30)),
    }


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(appt, "emit_metric", lambda *a, **k: None)
    events = []
    monkeypatch.setattr(appt, "publish_event", lambda name, detail: events.append((name, detail)) or True)
    store = MemorySlotStore([
        (ORIGINAL_KEY, slot(ORIGINAL_KEY, ORIGINAL_SLOT_ID, booked=1)),
        (TARGET_KEY, slot(TARGET_KEY, TARGET_SLOT_ID)),
    ])
    rs.set_store(store)
    db = FakeDB(appointment_row())
    monkeypatch.setattr(appt, "connect_db", db.connect)
    return {"store": store, "db": db, "events": events}


def run_saga(world, *, fail_at=None, start_state=None):
    """Drive the saga in the same order as the state machine."""
    state = start_state or {"patient_id": 7, "appointment_id": 1,
                            "target_slot_time": iso(TARGET_START)}
    state = {**state, "validated": {"result": rs.validate(state)}}
    record = state["validated"]["result"]
    if fail_at == "commit_appointment":
        world["db"].fail_commit = True
    rs.claim_target(record)
    try:
        if fail_at == "release_original":
            raise RuntimeError("injected release failure")
        rs.release_original(record)
        rs.commit_appointment(record)
    except Exception:
        if fail_at == "release_original":
            rs.compensate_target(record)
            return "COMPENSATED"
        # Commit failed after the original was released: restore the original seat.
        try:
            rs.reclaim_original(record)
        except seats.SeatUnavailable:
            rs.compensate_target(record)
            return "RECONCILIATION_REQUIRED"
        rs.compensate_target(record)
        return "COMPENSATED"
    rs.publish_rescheduled(record)
    return "RESCHEDULED"


def counts(world):
    return (world["store"].get(ORIGINAL_KEY)["bookedCount"],
            world["store"].get(TARGET_KEY)["bookedCount"])


def test_successful_reschedule_moves_patient_and_frees_original(world):
    assert run_saga(world) == "RESCHEDULED"
    assert world["db"].rows[1]["slot_id"] == TARGET_SLOT_ID
    assert world["db"].rows[1]["reschedule_count"] == 1
    assert counts(world) == (0, 1)
    assert [name for name, _ in world["events"]] == ["AppointmentRescheduled"]


def test_forced_release_failure_compensates_and_patient_keeps_one_appointment(world):
    assert run_saga(world, fail_at="release_original") == "COMPENSATED"
    assert world["db"].rows[1]["slot_id"] == ORIGINAL_SLOT_ID
    assert world["db"].rows[1]["status"] == "CONFIRMED"
    assert counts(world) == (1, 0), "target claim must be undone"


def test_rds_failure_after_release_reclaims_original_and_undoes_target(world):
    assert run_saga(world, fail_at="commit_appointment") == "COMPENSATED"
    assert world["db"].rows[1]["slot_id"] == ORIGINAL_SLOT_ID
    assert counts(world) == (1, 0)


def test_seat_lost_during_compensation_is_reported_for_reconciliation(world, monkeypatch):
    original = seats.book_seat

    def steal_original(store, key, token):
        # Another patient takes the released original before the re-claim runs.
        if key == ORIGINAL_KEY and token.startswith("reclaim:"):
            raise seats.SeatUnavailable("taken by another patient")
        return original(store, key, token)

    monkeypatch.setattr(seats, "book_seat", steal_original)
    assert run_saga(world, fail_at="commit_appointment") == "RECONCILIATION_REQUIRED"


def test_full_target_fails_cleanly_and_original_is_unchanged(world):
    world["store"].items[MemorySlotStore._k(TARGET_KEY)]["bookedCount"] = 1
    world["store"].items[MemorySlotStore._k(TARGET_KEY)]["status"] = "FULLY_BOOKED"
    with pytest.raises(seats.SeatUnavailable):
        run_saga(world)
    assert world["db"].rows[1]["slot_id"] == ORIGINAL_SLOT_ID
    assert counts(world) == (1, 1)


def test_reschedule_limit_is_enforced(world):
    world["db"].rows[1]["reschedule_count"] = 2
    with pytest.raises(rs.RescheduleRejected) as error:
        rs.validate({"patient_id": 7, "appointment_id": 1, "target_slot_time": iso(TARGET_START)})
    assert error.value.status_code == 409


def test_reschedule_within_six_hours_is_rejected(world):
    world["db"].rows[1]["start_at_ist"] = datetime.now(IST).replace(tzinfo=None) + timedelta(hours=3)
    with pytest.raises(rs.RescheduleRejected):
        rs.validate({"patient_id": 7, "appointment_id": 1, "target_slot_time": iso(TARGET_START)})


def test_another_patients_appointment_is_not_visible(world):
    with pytest.raises(rs.RescheduleRejected) as error:
        rs.validate({"patient_id": 99, "appointment_id": 1, "target_slot_time": iso(TARGET_START)})
    assert error.value.status_code == 404


def test_target_must_use_same_consultation_type(world):
    world["store"].items[MemorySlotStore._k(TARGET_KEY)]["consultationTypeId"] = "4"
    with pytest.raises(rs.RescheduleRejected):
        rs.validate({"patient_id": 7, "appointment_id": 1, "target_slot_time": iso(TARGET_START)})


def test_replayed_release_frees_one_seat_only(world):
    record = rs.validate({"patient_id": 7, "appointment_id": 1, "target_slot_time": iso(TARGET_START)})
    rs.release_original(record)
    rs.release_original(record)
    assert counts(world)[0] == 0


def test_state_machine_only_calls_implemented_operations():
    import json
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "statemachines", "reschedule-saga.asl.json")
    definition = json.load(open(path))
    called = set()
    for state in definition["States"].values():
        payload = state.get("Parameters", {}).get("Payload", {})
        if "operation" in payload:
            called.add(payload["operation"])
    implemented = set(rs.OPERATIONS) | {"validate"}
    assert called <= implemented, called - implemented


class FakeSfn:
    def __init__(self):
        self.calls = []
        self.existing = set()

    def start_execution(self, stateMachineArn, name, input):
        from botocore.exceptions import ClientError
        self.calls.append((name, input))
        if name in self.existing:
            raise ClientError({"Error": {"Code": "ExecutionAlreadyExists", "Message": "dup"}}, "StartExecution")
        self.existing.add(name)
        return {"executionArn": f"arn:exec:{name}"}


def patch_event(appointment_id, target, patient_role="patient", patient_id="7"):
    import json
    return {
        "httpMethod": "PATCH", "path": f"/appointments/{appointment_id}",
        "body": json.dumps({"target_slot_time": target}),
        "requestContext": {"authorizer": {"profileId": patient_id, "role": patient_role}},
    }


def test_patch_starts_one_execution_and_retry_reuses_it(monkeypatch):
    monkeypatch.setenv("RESCHEDULE_STATE_MACHINE_ARN", "arn:sm")
    sfn = FakeSfn()
    monkeypatch.setattr(rs, "get_sfn", lambda: sfn)
    first = rs.lambda_handler(patch_event(1, iso(TARGET_START)), None)
    second = rs.lambda_handler(patch_event(1, iso(TARGET_START)), None)
    assert first["statusCode"] == 202 and second["statusCode"] == 202
    assert len(sfn.existing) == 1, "a retried PATCH must not start a second saga"


def test_patch_requires_patient_role(monkeypatch):
    monkeypatch.setenv("RESCHEDULE_STATE_MACHINE_ARN", "arn:sm")
    monkeypatch.setattr(rs, "get_sfn", lambda: FakeSfn())
    result = rs.lambda_handler(patch_event(1, iso(TARGET_START), patient_role="doctor"), None)
    assert result["statusCode"] == 403


def test_patch_without_authentication_is_401(monkeypatch):
    monkeypatch.setenv("RESCHEDULE_STATE_MACHINE_ARN", "arn:sm")
    event = patch_event(1, iso(TARGET_START))
    event["requestContext"] = {}
    assert rs.lambda_handler(event, None)["statusCode"] == 401


def test_appointment_lambda_delegates_patch_to_reschedule(monkeypatch):
    """The existing appointments proxy routes PATCH to the Appointment Lambda, which hands it to the saga starter."""
    from lambdas.appointment import appointment as module
    monkeypatch.setenv("RESCHEDULE_STATE_MACHINE_ARN", "arn:sm")
    sfn = FakeSfn()
    monkeypatch.setattr(rs, "get_sfn", lambda: sfn)
    event = patch_event(1, iso(TARGET_START))
    event["httpMethod"] = "PATCH"
    context = type("Ctx", (), {"aws_request_id": "req-1"})()
    result = module.lambda_handler(event, context)
    assert result["statusCode"] == 202
    assert len(sfn.existing) == 1
