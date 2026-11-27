import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from lambdas.common import seats
from lambdas.common.seats import MemorySlotStore, SeatUnavailable

KEY = {"doctorId": "42", "slotTime": "2026-12-01T09:00:00+05:30"}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(seats.time, "sleep", lambda *_: None)


def store(capacity=1, booked=0, held=0):
    return MemorySlotStore([(KEY, {
        "capacity": capacity, "bookedCount": booked, "heldCount": held,
        "seatRevision": 0, "status": "AVAILABLE", "isBlackout": False,
        "slotId": "42#7#3#2026-12-01T09:00:00+05:30",
    })])


def test_hold_consumes_capacity_and_blocks_normal_booking():
    s = store(capacity=1)
    assert seats.hold_seat(s, KEY, "hold:1") is True
    with pytest.raises(SeatUnavailable):
        seats.book_seat(s, KEY, "claim:9:1")


def test_hold_is_idempotent_per_token():
    s = store(capacity=2)
    assert seats.hold_seat(s, KEY, "freed:5") is True
    assert seats.hold_seat(s, KEY, "freed:5") is False
    assert s.get(KEY)["heldCount"] == 1


def test_convert_hold_moves_seat_from_held_to_booked():
    s = store(capacity=1, held=1)
    assert seats.convert_hold(s, KEY, "accept:x") is True
    item = s.get(KEY)
    assert (item["heldCount"], item["bookedCount"]) == (0, 1)


def test_convert_back_restores_hold_after_failed_write():
    s = store(capacity=1, held=1)
    seats.convert_hold(s, KEY, "accept:x")
    seats.convert_back(s, KEY, "accept-undo:x")
    item = s.get(KEY)
    assert (item["heldCount"], item["bookedCount"]) == (1, 0)


def test_release_never_goes_below_zero_and_is_a_noop():
    s = store(capacity=1, booked=0)
    assert seats.release_booked(s, KEY, "release:1") is False
    assert s.get(KEY)["bookedCount"] == 0


def test_blackout_slot_refuses_new_seats():
    s = store(capacity=3)
    s.items[MemorySlotStore._k(KEY)]["isBlackout"] = True
    with pytest.raises(SeatUnavailable):
        seats.hold_seat(s, KEY, "hold:1")


def test_concurrent_claims_never_exceed_capacity():
    s = store(capacity=3)
    results = []

    def attempt(i):
        try:
            return seats.book_seat(s, KEY, f"claim:{i}:1")
        except (SeatUnavailable, seats.SeatContention):
            return "rejected"

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(attempt, range(20)))
    assert results.count(True) == 3
    assert s.get(KEY)["bookedCount"] == 3


def test_held_and_booked_together_never_exceed_capacity():
    s = store(capacity=3)

    def attempt(i):
        try:
            if i % 2:
                return seats.hold_seat(s, KEY, f"hold:{i}")
            return seats.book_seat(s, KEY, f"claim:{i}")
        except (SeatUnavailable, seats.SeatContention):
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(attempt, range(16)))
    item = s.get(KEY)
    assert item["bookedCount"] + item["heldCount"] <= 3
