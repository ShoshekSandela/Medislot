"""Notification dispatch, exactly-once behaviour, reminders and the no-show sweeper."""

import json
from datetime import datetime, timedelta

import pytest

from lambdas.appointment import appointment as appt
from lambdas.common import repos
from lambdas.notification import notification as notify
from lambdas.no_show import no_show
from lambdas.reminders import reminder

IST = appt.IST


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr(appt, "emit_metric", lambda *a, **k: None)
    monkeypatch.setattr(notify, "_sns_publish",
                        lambda topic, message, event: messages.append((topic, event, message)))
    monkeypatch.setattr(notify, "PATIENT_TOPIC_ARN", "arn:patient")
    monkeypatch.setattr(notify, "DOCTOR_TOPIC_ARN", "arn:doctor")
    return messages


def test_booking_notifies_patient_and_doctor_once_each(sent):
    log = repos.MemoryDispatchLog()
    detail = {"appointment_id": 125, "patient_id": 7, "doctor_id": 42}
    notify.dispatch("AppointmentBooked", detail, dispatch_log=log, publish=lambda t, m, e:
                    sent.append((t, e, m)), now_epoch=1000)
    notify.dispatch("AppointmentBooked", detail, dispatch_log=log, publish=lambda t, m, e:
                    sent.append((t, e, m)), now_epoch=1001)
    assert sorted(topic for topic, _, _ in sent) == ["arn:doctor", "arn:patient"]


def test_doctor_cancellation_notifies_only_the_patient(sent):
    log = repos.MemoryDispatchLog()
    notify.dispatch("AppointmentCancelled",
                    {"appointment_id": 9, "status": "CANCELLED_BY_DOCTOR"},
                    dispatch_log=log, publish=lambda t, m, e: sent.append((t, e, m)), now_epoch=1)
    assert [topic for topic, _, _ in sent] == ["arn:patient"]


def test_patient_cancellation_notifies_both(sent):
    log = repos.MemoryDispatchLog()
    notify.dispatch("AppointmentCancelled",
                    {"appointment_id": 9, "status": "CANCELLED_BY_PATIENT"},
                    dispatch_log=log, publish=lambda t, m, e: sent.append((t, e, m)), now_epoch=1)
    assert sorted(topic for topic, _, _ in sent) == ["arn:doctor", "arn:patient"]


def test_duplicate_sqs_delivery_dispatches_once(sent, monkeypatch):
    log = repos.MemoryDispatchLog()
    monkeypatch.setattr(notify, "get_log", lambda: log)
    envelope = json.dumps({"detail-type": "AppointmentRescheduled",
                           "detail": {"appointment_id": 3, "patient_id": 7, "doctor_id": 42}})
    records = {"Records": [{"messageId": "m1", "body": envelope},
                           {"messageId": "m2", "body": envelope}]}
    result = notify.lambda_handler(records, None)
    assert result["batchItemFailures"] == []
    assert len(sent) == 2, "one patient send and one doctor send in total"


def test_failed_record_is_returned_for_retry(sent, monkeypatch):
    log = repos.MemoryDispatchLog()
    monkeypatch.setattr(notify, "get_log", lambda: log)

    def fail(*_a, **_k):
        raise RuntimeError("SNS unavailable")

    monkeypatch.setattr(notify, "_sns_publish", fail)
    records = {"Records": [{"messageId": "bad", "body": json.dumps(
        {"detail-type": "WaitlistOfferIssued", "detail": {"offer_id": "o1", "patient_id": "7"}})}]}
    result = notify.lambda_handler(records, None)
    assert result["batchItemFailures"] == [{"itemIdentifier": "bad"}]


def test_reminder_windows():
    start = datetime(2026, 12, 1, 9, 0)
    assert reminder.due_interval(start, start - timedelta(hours=30)) is None
    assert reminder.due_interval(start, start - timedelta(hours=20)) == "24H"
    assert reminder.due_interval(start, start - timedelta(hours=1)) == "2H"
    assert reminder.due_interval(start, start + timedelta(minutes=1)) is None


def test_reminder_dispatcher_run_twice_sends_once(sent):
    log = repos.MemoryDispatchLog()
    start = datetime(2026, 12, 1, 9, 0)
    now = start - timedelta(hours=1)
    rows = [{"appointment_id": 5, "patient_id": 7, "start_at_ist": start}]
    first = reminder.run(rows, now, dispatch_log=log, publish=lambda t, m, e: sent.append((t, e, m)),
                         now_epoch=10)
    second = reminder.run(rows, now, dispatch_log=log, publish=lambda t, m, e: sent.append((t, e, m)),
                          now_epoch=11)
    assert first == 1 and second == 0
    assert len(sent) == 1


class NoShowDB:
    """Applies the sweeper's UPDATE to in-memory rows so a second run sees the result."""

    def __init__(self, rows, now):
        self.rows = rows
        self.now = now
        self.commits = 0

    def connect(self):
        db = self

        class Cursor:
            rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=()):
                assert "status = 'CONFIRMED'" in sql and "end_at_ist" in sql
                grace = timedelta(minutes=params[0])
                self.rowcount = 0
                for row in db.rows:
                    if row["status"] == "CONFIRMED" and row["end_at_ist"] + grace <= db.now:
                        row["status"] = "NO_SHOW"
                        self.rowcount += 1

        class Connection:
            def cursor(self):
                return Cursor()

            def commit(self):
                db.commits += 1

            def rollback(self):
                pass

            def close(self):
                pass
        return Connection()


def test_no_show_sweeper_marks_stale_appointments_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(appt, "emit_metric", lambda *a, **k: None)
    now = datetime(2026, 12, 1, 12, 0)
    rows = [
        {"appointment_id": 1, "status": "CONFIRMED", "end_at_ist": now - timedelta(minutes=45)},
        {"appointment_id": 2, "status": "CONFIRMED", "end_at_ist": now - timedelta(minutes=10)},
        {"appointment_id": 3, "status": "COMPLETED", "end_at_ist": now - timedelta(hours=3)},
    ]
    db = NoShowDB(rows, now)
    monkeypatch.setattr(appt, "connect_db", db.connect)
    assert no_show.lambda_handler({}, None) == {"marked_no_show": 1}
    assert no_show.lambda_handler({}, None) == {"marked_no_show": 0}
    assert rows[0]["status"] == "NO_SHOW"
    assert rows[1]["status"] == "CONFIRMED"
    assert rows[2]["status"] == "COMPLETED"
