"""Storage for waitlist entries, hold offers and the notification dispatch log.

Each repository has a DynamoDB implementation (production, existing tables from
data-stack.yaml) and an in-memory implementation with identical semantics
(tests). Conditional writes are what make offers exclusive and dispatch exactly once.
"""

import threading
import time

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

ACTIVE_WAITLIST_STATES = ("WAITING", "OFFERED")


def _is_conditional_failure(error):
    return error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class DynamoWaitlistRepo:
    def __init__(self, waitlist_table, offers_table):
        self.waitlist = waitlist_table
        self.offers = offers_table

    def put_waitlist(self, item):
        try:
            self.waitlist.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(patientId) AND attribute_not_exists(waitlistSortKey)",
            )
            return True
        except ClientError as error:
            if _is_conditional_failure(error):
                return False
            raise

    def waitlist_for_patient(self, patient_id):
        response = self.waitlist.query(KeyConditionExpression=Key("patientId").eq(str(patient_id)))
        return list(response.get("Items", []))

    def waiting_for_slot(self, slot_id):
        response = self.waitlist.query(
            IndexName="slot-queue-index",
            KeyConditionExpression=Key("slotId").eq(slot_id),
            FilterExpression=Attr("status").eq("WAITING"),
            ScanIndexForward=True,
        )
        return sorted(response.get("Items", []), key=lambda item: item["joinedAt"])

    def set_waitlist_status(self, patient_id, sort_key, expect, new):
        try:
            self.waitlist.update_item(
                Key={"patientId": str(patient_id), "waitlistSortKey": sort_key},
                UpdateExpression="SET #s = :new",
                ConditionExpression="#s = :expect",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":new": new, ":expect": expect},
            )
            return True
        except ClientError as error:
            if _is_conditional_failure(error):
                return False
            raise

    def put_offer(self, item):
        try:
            self.offers.put_item(Item=item, ConditionExpression="attribute_not_exists(offerId)")
            return True
        except ClientError as error:
            if _is_conditional_failure(error):
                return False
            raise

    def get_offer(self, offer_id):
        return self.offers.get_item(Key={"offerId": offer_id}, ConsistentRead=True).get("Item")

    def set_offer_status(self, offer_id, expect, new, extra=None):
        names = {"#s": "offerStatus"}
        values = {":new": new, ":expect": expect}
        sets = ["#s = :new"]
        for index, (name, value) in enumerate((extra or {}).items()):
            names[f"#x{index}"] = name
            values[f":x{index}"] = value
            sets.append(f"#x{index} = :x{index}")
        try:
            self.offers.update_item(
                Key={"offerId": offer_id},
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression="#s = :expect",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            return True
        except ClientError as error:
            if _is_conditional_failure(error):
                return False
            raise

    def expired_pending_offers(self, now_epoch):
        # Full scan: acceptable at project scale. The sweeper runs every minute.
        response = self.offers.scan(
            FilterExpression=Attr("offerStatus").eq("PENDING") & Attr("expiresAt").lte(now_epoch)
        )
        return list(response.get("Items", []))


class MemoryWaitlistRepo:
    def __init__(self):
        self.lock = threading.Lock()
        self.waitlist = {}
        self.offers = {}

    def put_waitlist(self, item):
        with self.lock:
            key = (str(item["patientId"]), item["waitlistSortKey"])
            if key in self.waitlist:
                return False
            self.waitlist[key] = dict(item)
            return True

    def waitlist_for_patient(self, patient_id):
        with self.lock:
            return [dict(v) for (pid, _), v in self.waitlist.items() if pid == str(patient_id)]

    def waiting_for_slot(self, slot_id):
        with self.lock:
            rows = [dict(v) for v in self.waitlist.values()
                    if v["slotId"] == slot_id and v["status"] == "WAITING"]
        return sorted(rows, key=lambda item: item["joinedAt"])

    def set_waitlist_status(self, patient_id, sort_key, expect, new):
        with self.lock:
            item = self.waitlist.get((str(patient_id), sort_key))
            if item is None or item["status"] != expect:
                return False
            item["status"] = new
            return True

    def put_offer(self, item):
        with self.lock:
            if item["offerId"] in self.offers:
                return False
            self.offers[item["offerId"]] = dict(item)
            return True

    def get_offer(self, offer_id):
        with self.lock:
            item = self.offers.get(offer_id)
            return dict(item) if item else None

    def set_offer_status(self, offer_id, expect, new, extra=None):
        with self.lock:
            item = self.offers.get(offer_id)
            if item is None or item["offerStatus"] != expect:
                return False
            item["offerStatus"] = new
            item.update(extra or {})
            return True

    def expired_pending_offers(self, now_epoch):
        with self.lock:
            return [dict(v) for v in self.offers.values()
                    if v["offerStatus"] == "PENDING" and v["expiresAt"] <= now_epoch]


class DynamoDispatchLog:
    """Exactly-once claim per dispatch id. Stale PENDING claims can be retaken."""

    STALE_AFTER_SECONDS = 300

    def __init__(self, table):
        self.table = table

    def claim(self, dispatch_id, now_epoch, ttl_epoch):
        try:
            self.table.put_item(
                Item={"dispatchId": dispatch_id, "status": "PENDING",
                      "claimedAt": now_epoch, "expiresAt": ttl_epoch},
                ConditionExpression="attribute_not_exists(dispatchId)",
            )
            return True
        except ClientError as error:
            if not _is_conditional_failure(error):
                raise
        try:
            self.table.update_item(
                Key={"dispatchId": dispatch_id},
                UpdateExpression="SET #s = :pending, claimedAt = :now",
                ConditionExpression="#s = :pending AND claimedAt <= :stale",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":pending": "PENDING", ":now": now_epoch,
                                           ":stale": now_epoch - self.STALE_AFTER_SECONDS},
            )
            return True
        except ClientError as error:
            if _is_conditional_failure(error):
                return False
            raise

    def mark_sent(self, dispatch_id):
        self.table.update_item(
            Key={"dispatchId": dispatch_id},
            UpdateExpression="SET #s = :sent",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":sent": "SENT"},
        )


class MemoryDispatchLog:
    def __init__(self):
        self.lock = threading.Lock()
        self.rows = {}

    def claim(self, dispatch_id, now_epoch, ttl_epoch):
        with self.lock:
            row = self.rows.get(dispatch_id)
            if row is None:
                self.rows[dispatch_id] = {"status": "PENDING", "claimedAt": now_epoch}
                return True
            if row["status"] == "PENDING" and row["claimedAt"] <= now_epoch - DynamoDispatchLog.STALE_AFTER_SECONDS:
                row["claimedAt"] = now_epoch
                return True
            return False

    def mark_sent(self, dispatch_id):
        with self.lock:
            self.rows[dispatch_id]["status"] = "SENT"


def now_epoch():
    return int(time.time())
