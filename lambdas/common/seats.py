"""Shared seat primitives for reschedule, waitlist holds and hold acceptance.

A slot item carries bookedCount (confirmed seats), heldCount (seats reserved for
a waitlist offer) and seatRevision. Every change is one conditional write that
checks the values it read plus a token set, so two workers can never take the
same seat and the same change can never be applied twice.

Seat-changing operations return True when they applied a change and False when
they were a no-op because the same token was already applied (idempotent retry).
"""

import random
import time

from botocore.exceptions import ClientError

MAX_ATTEMPTS = 5
TOKEN_SET = "seatTokens"
BLOCKED_STATUSES = {
    "BLACKOUT", "INACTIVE", "SLOT_CANCELLED_BY_DOCTOR", "CANCELLED", "CANCELLING",
}


class SeatUnavailable(Exception):
    """No seat can be taken: slot full, blocked, or blacked out."""


class SlotMissing(Exception):
    """The slot item does not exist."""


class SeatContention(Exception):
    """Too many concurrent writers; the caller should retry later."""


def _count(item, name):
    value = item.get(name)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)) and type(value).__name__ != "Decimal":
        raise SeatUnavailable(f"{name} needs manual repair before this change can be applied")
    return int(value)


def _derive_status(booked, held, capacity):
    taken = booked + held
    if taken >= capacity:
        return "FULLY_BOOKED"
    if taken > 0:
        return "PARTIALLY_BOOKED"
    return "AVAILABLE"


def _sleep(attempt):
    time.sleep(min(0.025 * (2 ** attempt) + random.uniform(0, 0.025), 0.25))


def _apply(store, key, changes, token, *, consume, check_blocked):
    """Apply seat deltas atomically. changes maps counter name to delta."""
    for attempt in range(MAX_ATTEMPTS):
        item = store.get(key)
        if item is None:
            raise SlotMissing(str(key))
        if token in set(item.get(TOKEN_SET) or set()):
            return False
        if check_blocked and (
            item.get("isBlackout") is True
            or str(item.get("status", "")).upper() in BLOCKED_STATUSES
        ):
            raise SeatUnavailable("This appointment slot is blocked or unavailable")

        booked = _count(item, "bookedCount")
        held = _count(item, "heldCount")
        capacity = max(1, _count(item, "capacity") or 1)
        revision = _count(item, "seatRevision")

        new_booked = booked + changes.get("bookedCount", 0)
        new_held = held + changes.get("heldCount", 0)
        if new_booked < 0 or new_held < 0:
            return False  # nothing to release: already free, treat as a no-op
        if consume and new_booked + new_held > capacity:
            raise SeatUnavailable("This appointment slot is full")

        expect = {
            "bookedCount": item.get("bookedCount"),
            "heldCount": item.get("heldCount"),
            "seatRevision": item.get("seatRevision"),
        }
        updates = {
            "bookedCount": new_booked,
            "heldCount": new_held,
            "seatRevision": revision + 1,
            "status": _derive_status(new_booked, new_held, capacity),
        }
        if store.cas(key, expect=expect, updates=updates, token=token):
            return True
        _sleep(attempt)
    raise SeatContention(str(key))


def hold_seat(store, key, token):
    """Reserve one free seat for a waitlist offer."""
    return _apply(store, key, {"heldCount": 1}, token, consume=True, check_blocked=True)


def release_hold(store, key, token):
    """Free a held seat, for an expired or declined offer."""
    return _apply(store, key, {"heldCount": -1}, token, consume=False, check_blocked=False)


def book_seat(store, key, token):
    """Take one free seat as a confirmed booking (reschedule target)."""
    return _apply(store, key, {"bookedCount": 1}, token, consume=True, check_blocked=True)


def release_booked(store, key, token):
    """Free one confirmed seat (reschedule original, undo of a claim)."""
    return _apply(store, key, {"bookedCount": -1}, token, consume=False, check_blocked=False)


def convert_hold(store, key, token):
    """Turn a held seat into a booked seat when the offer is accepted."""
    return _apply(store, key, {"heldCount": -1, "bookedCount": 1}, token,
                  consume=False, check_blocked=True)


def convert_back(store, key, token):
    """Undo convert_hold when the appointment row could not be written."""
    return _apply(store, key, {"heldCount": 1, "bookedCount": -1}, token,
                  consume=True, check_blocked=False)


class DynamoSlotStore:
    """Production adapter: one conditional UpdateItem per change."""

    def __init__(self, table):
        self.table = table

    def get(self, key):
        return self.table.get_item(Key=key, ConsistentRead=True).get("Item")

    def cas(self, key, *, expect, updates, token):
        names, values, conditions, sets = {}, {}, [], []
        for index, (name, value) in enumerate(expect.items()):
            names[f"#e{index}"] = name
            if value is None:
                conditions.append(f"attribute_not_exists(#e{index})")
            else:
                values[f":e{index}"] = value
                conditions.append(f"#e{index} = :e{index}")
        names["#tokens"] = TOKEN_SET
        values[":token"] = token
        conditions.append("NOT contains(#tokens, :token)")
        for index, (name, value) in enumerate(updates.items()):
            names[f"#u{index}"] = name
            values[f":u{index}"] = value
            sets.append(f"#u{index} = :u{index}")
        values[":tokenset"] = {token}
        try:
            self.table.update_item(
                Key=key,
                UpdateExpression=f"SET {', '.join(sets)} ADD #tokens :tokenset",
                ConditionExpression=" AND ".join(conditions),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            return True
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise


class MemorySlotStore:
    """Test double with the same semantics as DynamoSlotStore.cas."""

    def __init__(self, items=None):
        import threading
        self.lock = threading.Lock()
        self.items = {}
        for key, item in items or []:
            self.items[self._k(key)] = dict(item)

    @staticmethod
    def _k(key):
        return (str(key["doctorId"]), str(key["slotTime"]))

    def get(self, key):
        with self.lock:
            item = self.items.get(self._k(key))
            if item is None:
                return None
            copy = dict(item)
            if TOKEN_SET in copy:
                copy[TOKEN_SET] = set(copy[TOKEN_SET])
            return copy

    def cas(self, key, *, expect, updates, token):
        with self.lock:
            item = self.items.get(self._k(key))
            if item is None:
                return False
            for name, value in expect.items():
                if value is None:
                    if name in item:
                        return False
                elif item.get(name) != value:
                    return False
            if token in item.get(TOKEN_SET, set()):
                return False
            item.update(updates)
            item.setdefault(TOKEN_SET, set()).add(token)
            return True
