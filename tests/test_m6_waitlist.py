"""Waitlist, exclusive hold offers, acceptance and expiry, plus the regression
check that ordinary booking respects held seats."""

import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

import pytest
from botocore.exceptions import ClientError

from lambdas.appointment import appointment as appt
from lambdas.common import repos, seats
from lambdas.common.seats import MemorySlotStore
from lambdas.hold_offers import hold_offers as ho
from lambdas.waitlist import waitlist as wl

IST = appt.IST
DOCTOR = "42"
NOW_EPOCH = 1_800_000_000


def slot_time(day):
    return (datetime.now(IST).replace(tzinfo=None) + timedelta(days=day)).replace(
        hour=9, minute=0, second=0, microsecond=0).replace(tzinfo=IST).isoformat(timespec="seconds")


def slot_id_for(time_str, hospital=7, consult=3):
    return f"{DOCTOR}#{hospital}#{consult}#{time_str}"


def slot_item(time_str, booked=1, held=0, capacity=1):
    return {
        "slotId": slot_id_for(time_str), "capacity": capacity, "bookedCount": booked,
        "heldCount": held, "seatRevision": 0, "status": "FULLY_BOOKED" if booked else "AVAILABLE",
        "isBlackout": False, "consultationTypeId": "3", "hospitalId": 7,
        "endTime": (datetime.fromisoformat(time_str).replace(tzinfo=None) + timedelta(minutes=30))
        .replace(tzinfo=IST).isoformat(timespec="seconds"),
    }


def key_for(time_str):
    return {"doctorId": DOCTOR, "slotTime": time_str}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(appt, "emit_metric", lambda *a, **k: None)
    monkeypatch.setattr(seats.time, "sleep", lambda *_: None)
    events = []

    def publish(name, detail):
        events.append((name, detail))
        return True

    monkeypatch.setattr(appt, "publish_event", publish)
    time_a = slot_time(3)
    store = MemorySlotStore([(key_for(time_a), slot_item(time_a, booked=1))])
    repo = repos.MemoryWaitlistRepo()
    wl.set_backends(store, repo)
    return {"store": store, "repo": repo, "events": events, "slot": time_a,
            "slot_id": slot_id_for(time_a), "publish": publish}


def free_the_seat(env):
    """Simulate a cancellation: the booked count drops back to zero."""
    item = env["store"].items[MemorySlotStore._k(key_for(env["slot"]))]
    item["bookedCount"] = 0
    item["status"] = "AVAILABLE"


def freed_detail(env, appointment_id=900):
    return {"slot_id": env["slot_id"], "doctor_id": int(DOCTOR), "slot_time": env["slot"],
            "appointment_id": appointment_id, "freed_seats": 1}


def join_patients(env, patient_ids):
    for patient in patient_ids:
        wl.join(patient, env["slot_id"], now=datetime(2026, 11, 26, 10, 0, patient % 60))


def test_join_is_refused_while_seats_are_free(env):
    free_the_seat(env)
    with pytest.raises(appt.ApiError) as error:
        wl.join(1, env["slot_id"])
    assert error.value.status_code == 409


def test_join_queue_is_ordered_by_join_time(env):
    join_patients(env, [11, 12, 13])
    waiting = env["repo"].waiting_for_slot(env["slot_id"])
    assert [w["patientId"] for w in waiting] == ["11", "12", "13"]


def test_patient_cannot_hold_more_than_five_waitlists(env, monkeypatch):
    monkeypatch.setenv("MEDISLOT_WAITLIST_MAX_ACTIVE", "5")
    for day in range(4, 9):  # five waitlists are allowed
        t = slot_time(day)
        env["store"].items[MemorySlotStore._k(key_for(t))] = slot_item(t, booked=1)
        wl.join(5, slot_id_for(t))
    t = slot_time(12)  # the sixth must be refused
    env["store"].items[MemorySlotStore._k(key_for(t))] = slot_item(t, booked=1)
    with pytest.raises(appt.ApiError) as error:
        wl.join(5, slot_id_for(t))
    assert error.value.status_code == 409


def test_duplicate_join_is_refused(env):
    join_patients(env, [21])
    with pytest.raises(appt.ApiError):
        wl.join(21, env["slot_id"])


def test_three_patients_one_freed_seat_gives_exactly_one_live_offer(env):
    join_patients(env, [31, 32, 33])
    free_the_seat(env)
    offer = ho.issue_for_freed_seat(freed_detail(env), repo=env["repo"], store=env["store"],
                                    now_epoch=NOW_EPOCH, publish=env["publish"])
    assert offer["patientId"] == "31"
    assert len(env["repo"].offers) == 1
    assert env["store"].get(key_for(env["slot"]))["heldCount"] == 1


def test_duplicate_seat_freed_delivery_does_not_create_a_second_offer(env):
    join_patients(env, [41, 42])
    free_the_seat(env)
    for _ in range(3):
        ho.issue_for_freed_seat(freed_detail(env, 900), repo=env["repo"], store=env["store"],
                                now_epoch=NOW_EPOCH, publish=env["publish"])
    live = [o for o in env["repo"].offers.values() if o["offerStatus"] == "PENDING"]
    assert len(live) == 1
    assert env["store"].get(key_for(env["slot"]))["heldCount"] == 1


def test_concurrent_freed_events_create_one_offer_per_seat(env):
    join_patients(env, [51, 52, 53, 54])
    free_the_seat(env)

    def fire(_):
        return ho.issue_for_freed_seat(freed_detail(env, 777), repo=env["repo"], store=env["store"],
                                       now_epoch=NOW_EPOCH, publish=env["publish"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(fire, range(8)))
    assert len([o for o in env["repo"].offers.values() if o["offerStatus"] == "PENDING"]) == 1


def test_held_seat_is_refused_to_ordinary_booking(env):
    join_patients(env, [61])
    free_the_seat(env)
    ho.issue_for_freed_seat(freed_detail(env), repo=env["repo"], store=env["store"],
                            now_epoch=NOW_EPOCH, publish=env["publish"])
    with pytest.raises(seats.SeatUnavailable):
        seats.book_seat(env["store"], key_for(env["slot"]), "claim:other:1")


def test_offer_acceptance_books_the_held_seat(env, monkeypatch):
    join_patients(env, [71])
    free_the_seat(env)
    offer = ho.issue_for_freed_seat(freed_detail(env), repo=env["repo"], store=env["store"],
                                    now_epoch=NOW_EPOCH, publish=env["publish"])
    monkeypatch.setattr(wl, "_insert_appointment", lambda o, s, oid: {
        "appointment_id": 555, "patient_id": 71, "doctor_id": 42, "slot_id": o["slotId"],
        "start_at_ist": "x", "end_at_ist": "y", "status": "CONFIRMED"})
    result = wl.accept(71, offer["offerId"], now_epoch=NOW_EPOCH + 10)
    assert result["appointment_id"] == 555
    item = env["store"].get(key_for(env["slot"]))
    assert (item["heldCount"], item["bookedCount"]) == (0, 1)
    assert env["repo"].offers[offer["offerId"]]["offerStatus"] == "ACCEPTED"


def test_accept_is_idempotent(env, monkeypatch):
    join_patients(env, [72])
    free_the_seat(env)
    offer = ho.issue_for_freed_seat(freed_detail(env), repo=env["repo"], store=env["store"],
                                    now_epoch=NOW_EPOCH, publish=env["publish"])
    monkeypatch.setattr(wl, "_insert_appointment", lambda o, s, oid: {"appointment_id": 556})
    first = wl.accept(72, offer["offerId"], now_epoch=NOW_EPOCH + 10)
    second = wl.accept(72, offer["offerId"], now_epoch=NOW_EPOCH + 20)
    assert second["replayed"] is True
    assert second["appointment_id"] == first["appointment_id"]
    assert env["store"].get(key_for(env["slot"]))["bookedCount"] == 1


def test_failed_appointment_write_restores_the_hold(env, monkeypatch):
    join_patients(env, [73])
    free_the_seat(env)
    offer = ho.issue_for_freed_seat(freed_detail(env), repo=env["repo"], store=env["store"],
                                    now_epoch=NOW_EPOCH, publish=env["publish"])

    def boom(*_):
        raise RuntimeError("RDS unavailable")

    monkeypatch.setattr(wl, "_insert_appointment", boom)
    with pytest.raises(appt.ApiError):
        wl.accept(73, offer["offerId"], now_epoch=NOW_EPOCH + 10)
    item = env["store"].get(key_for(env["slot"]))
    assert (item["heldCount"], item["bookedCount"]) == (1, 0)
    assert env["repo"].offers[offer["offerId"]]["offerStatus"] == "PENDING"


def test_expired_offer_cannot_be_accepted(env, monkeypatch):
    join_patients(env, [74])
    free_the_seat(env)
    offer = ho.issue_for_freed_seat(freed_detail(env), repo=env["repo"], store=env["store"],
                                    now_epoch=NOW_EPOCH, publish=env["publish"])
    with pytest.raises(appt.ApiError):
        wl.accept(74, offer["offerId"], now_epoch=NOW_EPOCH + 901)


def test_expiry_passes_the_seat_to_the_next_patient_exactly_once(env):
    join_patients(env, [81, 82])
    free_the_seat(env)
    first = ho.issue_for_freed_seat(freed_detail(env), repo=env["repo"], store=env["store"],
                                    now_epoch=NOW_EPOCH, publish=env["publish"])
    passed = ho.sweep_expired(repo=env["repo"], store=env["store"], now_epoch=NOW_EPOCH + 901,
                              publish=env["publish"])
    assert len(passed) == 1
    assert env["repo"].offers[first["offerId"]]["offerStatus"] == "EXPIRED"
    live = [o for o in env["repo"].offers.values() if o["offerStatus"] == "PENDING"]
    assert [o["patientId"] for o in live] == ["82"]
    assert env["store"].get(key_for(env["slot"]))["heldCount"] == 1


def test_sweeper_is_safe_to_run_twice(env):
    join_patients(env, [91, 92])
    free_the_seat(env)
    ho.issue_for_freed_seat(freed_detail(env), repo=env["repo"], store=env["store"],
                            now_epoch=NOW_EPOCH, publish=env["publish"])
    ho.sweep_expired(repo=env["repo"], store=env["store"], now_epoch=NOW_EPOCH + 901, publish=env["publish"])
    second = ho.sweep_expired(repo=env["repo"], store=env["store"], now_epoch=NOW_EPOCH + 901,
                              publish=env["publish"])
    assert second == []
    assert len([o for o in env["repo"].offers.values() if o["offerStatus"] == "PENDING"]) == 1
    assert env["store"].get(key_for(env["slot"]))["heldCount"] == 1


def test_leaving_while_offered_is_refused(env):
    join_patients(env, [95])
    free_the_seat(env)
    ho.issue_for_freed_seat(freed_detail(env), repo=env["repo"], store=env["store"],
                            now_epoch=NOW_EPOCH, publish=env["publish"])
    with pytest.raises(appt.ApiError):
        wl.leave(95, env["slot_id"])


def test_wrong_patient_cannot_accept_someone_elses_offer(env):
    join_patients(env, [96])
    free_the_seat(env)
    offer = ho.issue_for_freed_seat(freed_detail(env), repo=env["repo"], store=env["store"],
                                    now_epoch=NOW_EPOCH, publish=env["publish"])
    with pytest.raises(appt.ApiError) as error:
        wl.accept(97, offer["offerId"], now_epoch=NOW_EPOCH + 1)
    assert error.value.status_code == 404


def test_ordinary_booking_refuses_held_seat_via_reserve_slot(monkeypatch):
    """Regression guard for appointment.reserve_slot: a held seat is not bookable."""
    monkeypatch.setattr(appt.time, "sleep", lambda *_: None)
    t = slot_time(5)
    item = slot_item(t, booked=0, held=1)
    item["capacity"] = 1
    item["seatRevision"] = 1

    class FakeTable:
        updates = 0

        def get_item(self, Key, ConsistentRead=False):
            return {"Item": dict(item)}

        def update_item(self, **kwargs):
            FakeTable.updates += 1
            raise AssertionError("reserve_slot must not write to a full slot")

    monkeypatch.setattr(appt, "slots_table", FakeTable())
    with pytest.raises(appt.ApiError) as error:
        appt.reserve_slot(int(DOCTOR), t)
    assert error.value.status_code == 409
    assert FakeTable.updates == 0


def test_ordinary_booking_still_books_a_free_unheld_seat(monkeypatch):
    """Regression guard: with no holds, reserve_slot behaves exactly as before."""
    monkeypatch.setattr(appt.time, "sleep", lambda *_: None)
    t = slot_time(6)
    item = slot_item(t, booked=0, held=0)
    item["seatRevision"] = 0
    writes = []

    class FakeTable:
        def get_item(self, Key, ConsistentRead=False):
            return {"Item": dict(item)}

        def update_item(self, **kwargs):
            writes.append(kwargs)
            return {}

    monkeypatch.setattr(appt, "slots_table", FakeTable())
    appt.reserve_slot(int(DOCTOR), t)
    assert len(writes) == 1


def test_sqs_batch_issues_offer_and_reports_failures(env, monkeypatch):
    import json
    join_patients(env, [101])
    free_the_seat(env)
    monkeypatch.setattr(ho.wl, "get_repo", lambda: env["repo"])
    monkeypatch.setattr(ho.wl, "get_store", lambda: env["store"])
    monkeypatch.setattr(ho, "_publish", env["publish"])
    good = json.dumps({"detail-type": "SlotSeatFreed", "detail": freed_detail(env, 1234)})
    event = {"Records": [{"messageId": "ok", "body": good},
                         {"messageId": "bad", "body": "not json"}]}
    result = ho.lambda_handler(event, None)
    assert result["batchItemFailures"] == [{"itemIdentifier": "bad"}]
    assert len([o for o in env["repo"].offers.values() if o["offerStatus"] == "PENDING"]) == 1


def test_scheduled_sweep_event_runs_the_expiry(env, monkeypatch):
    monkeypatch.setattr(ho.wl, "get_repo", lambda: env["repo"])
    monkeypatch.setattr(ho.wl, "get_store", lambda: env["store"])
    monkeypatch.setattr(ho, "_publish", env["publish"])
    result = ho.lambda_handler({"action": "sweep-holds"}, None)
    assert result == {"passed_on": 0}


def test_unknown_hold_event_is_rejected(env):
    import pytest as _pytest
    with _pytest.raises(ValueError):
        ho.lambda_handler({"action": "something-else"}, None)
