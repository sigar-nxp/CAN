"""
Unit and regression tests for device offline/heartbeat timeout,
UID deduplication on re-assignment, and factory reset announcement handling.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import app
from canbus.constants import (
    CAN_ID_REQUEST,
    CAN_STATUS,
    DEFAULT_DEVICE_ID,
    STATUS_BASIC,
)
from canbus.protocol import CANFrame
from canbus.listener import CANListener, DeviceState, normalize_uid, uids_match
from config.globals import can_listener, can_service
from database.database import SessionLocal
from models.device import Device


class TestDeviceOfflineAndDedup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        # Reset devices and unassigned state between tests
        can_listener.devices.clear()
        can_listener.unassigned_devices.clear()
        with SessionLocal() as db:
            db.query(Device).delete()
            db.commit()

    def tearDown(self):
        can_listener.devices.clear()
        can_listener.unassigned_devices.clear()
        with SessionLocal() as db:
            db.query(Device).delete()
            db.commit()

    # =========================================================================
    # 1. Offline / Heartbeat Timeout Tests
    # =========================================================================

    def test_stale_device_transitions_to_offline_after_timeout(self):
        """
        Verify that a registered device transitions from online to offline
        when no telemetry is received within its configured timeout.
        """
        dev = can_listener.get_device(1)
        dev.offline_timeout = 0.3
        dev.update_lock_state(1)
        dev.mark_seen()

        self.assertTrue(dev.online)
        self.assertTrue(dev.is_online)

        # Before timeout expires
        time.sleep(0.1)
        self.assertTrue(dev.online)
        self.assertTrue(dev.is_online)

        # Wait until timeout has elapsed
        time.sleep(0.3)
        self.assertFalse(dev.online)
        self.assertFalse(dev.is_online)

        # Background janitor execution marks status as OFFLINE
        can_listener.check_offline_devices()
        self.assertFalse(dev.online)
        self.assertFalse(dev.is_online)
        self.assertEqual(dev.status_text, "OFFLINE")

        # Telemetry arrival recovers the device
        dev.mark_seen()
        self.assertTrue(dev.online)
        self.assertTrue(dev.is_online)
        self.assertEqual(dev.status_text, "LOCKED")

    def test_offline_reflected_in_api_and_active_count(self):
        """
        Verify that the /api/v1/devices API accurately reports is_online = False
        and active online lock count only counts truly active locks.
        """
        # Create dev 1 (online) and dev 2 (stale / offline)
        dev1 = can_listener.get_device(1)
        dev1.offline_timeout = 10.0
        dev1.mark_seen()

        dev2 = can_listener.get_device(2)
        dev2.offline_timeout = 0.1
        dev2.last_seen = time.time() - 5.0
        can_listener.check_offline_devices()

        res = self.client.get("/api/v1/devices")
        self.assertEqual(res.status_code, 200)
        items = res.json()

        d1 = next(item for item in items if item["device_id"] == 1)
        d2 = next(item for item in items if item["device_id"] == 2)

        self.assertTrue(d1["is_online"])
        self.assertTrue(d1["online"])

        self.assertFalse(d2["is_online"])
        self.assertFalse(d2["online"])
        self.assertEqual(d2["status"], "OFFLINE")

        # Active online locks count
        online_count = len([d for d in items if d.get("is_online")])
        self.assertEqual(online_count, 1)

    # =========================================================================
    # 2. UID Deduplication on Provisioning / Re-assignment Tests
    # =========================================================================

    def test_reassigning_uid_cleans_up_old_device_node(self):
        """
        Verify that re-assigning an existing hardware UID to a new Device ID
        removes the old device entry from memory and database so no ghost node remains.
        """
        uid_hex = "28005801"

        # 1. Provision to Device ID 1
        res1 = self.client.post("/api/v1/devices/assign", json={
            "uid32": int(uid_hex, 16),
            "device_id": 1,
            "bitrate_code": 4
        })
        self.assertEqual(res1.status_code, 200)
        self.assertIn(1, can_listener.devices)
        self.assertEqual(can_listener.devices[1].hardware_uid, uid_hex)

        # 2. Re-assign the SAME hardware UID to Device ID 2
        res2 = self.client.post("/api/v1/devices/assign", json={
            "uid32": int(uid_hex, 16),
            "device_id": 2,
            "bitrate_code": 4
        })
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertEqual(data2["device_id"], 2)
        self.assertEqual(data2.get("reassigned_from"), 1)

        # Verify Node 1 is completely purged (no ghost node)
        self.assertNotIn(1, can_listener.devices)
        self.assertIn(2, can_listener.devices)
        self.assertEqual(can_listener.devices[2].hardware_uid, uid_hex)

        with SessionLocal() as db:
            db_1 = db.query(Device).filter(Device.device_id == 1).first()
            self.assertIsNone(db_1, "Old Device ID 1 must be deleted from database")

            db_2 = db.query(Device).filter(Device.device_id == 2).first()
            self.assertIsNotNone(db_2, "New Device ID 2 must exist in database")
            self.assertEqual(db_2.hardware_uid, uid_hex)

    def test_locks_endpoint_provisioning_with_deduplication(self):
        """
        Verify provisioning via POST /api/v1/locks with string UID and deduplication.
        """
        uid_str = "AABBCCDD11223344"

        # Provision to Node 5
        res = self.client.post("/api/v1/locks", json={
            "uid": uid_str,
            "device_id": 5
        })
        self.assertEqual(res.status_code, 200)
        self.assertIn(5, can_listener.devices)

        # Re-assign to Node 6
        res2 = self.client.post("/api/v1/locks", json={
            "uid": uid_str,
            "device_id": 6
        })
        self.assertEqual(res2.status_code, 200)

        # Node 5 should be cleaned up
        self.assertNotIn(5, can_listener.devices)
        self.assertIn(6, can_listener.devices)

        # Check GET /api/v1/locks
        res_list = self.client.get("/api/v1/locks")
        self.assertEqual(res_list.status_code, 200)
        lock_ids = [d["device_id"] for d in res_list.json()]
        self.assertNotIn(5, lock_ids)
        self.assertIn(6, lock_ids)

    # =========================================================================
    # 3. Factory Reset / Unassigned Announcement Handling
    # =========================================================================

    def test_receiving_unassigned_frame_marks_known_uid_offline(self):
        """
        When the CAN listener receives an unassigned ID request (0x57F) with a hardware UID
        matching an already-registered device, verify that device node is immediately
        marked offline and unprovisioned.
        """
        uid32_bytes = bytes.fromhex("11223344")
        uid_hex = uid32_bytes.hex()

        dev = can_listener.get_device(3)
        dev.hardware_uid = uid_hex
        dev.update_lock_state(1)
        dev.mark_seen()

        self.assertTrue(dev.online)
        self.assertTrue(dev.is_online)
        self.assertEqual(dev.status_text, "LOCKED")

        # Simulate receiving unassigned ID request (0x57F)
        can_listener.running = True
        frame = CANFrame(
            arbitration_id=CAN_ID_REQUEST | DEFAULT_DEVICE_ID, # 0x57F
            data=uid32_bytes
        )
        can_listener.handle_frame(frame)

        # Verify device 3 was marked offline / unprovisioned
        self.assertFalse(dev.online)
        self.assertFalse(dev.is_online)
        self.assertEqual(dev.status_text, "UNPROVISIONED")
        self.assertTrue(getattr(dev, "is_unprovisioned", False))

        # Verify it was added to unassigned_devices
        self.assertIn(uid_hex, can_listener.unassigned_devices)

    def test_receiving_unassigned_frame_with_opcode_0x28(self):
        """
        Verify that receiving an unassigned announcement containing opcode 0x28
        in payload is decoded and marks matching registered node as offline/unprovisioned.
        """
        uid32_bytes = bytes.fromhex("DEADBEEF")
        uid_hex = uid32_bytes.hex().lower()

        dev = can_listener.get_device(4)
        dev.hardware_uid = uid_hex
        dev.update_lock_state(2)
        dev.mark_seen()

        self.assertTrue(dev.online)

        # Frame on 0x57F with opcode 0x28 followed by 4-byte UID
        can_listener.running = True
        frame = CANFrame(
            arbitration_id=0x57F,
            data=bytes([0x28]) + uid32_bytes
        )
        can_listener.handle_frame(frame)

        self.assertFalse(dev.online)
        self.assertFalse(dev.is_online)
        self.assertEqual(dev.status_text, "UNPROVISIONED")

    def test_full_uid_prefix_matching_on_unassigned_announcement(self):
        """
        Verify that a registered device with an 8-byte full UID (e.g. 1122334455667788)
        is properly matched and marked offline when a 4-byte announcement (11223344) arrives.
        """
        full_uid = "1122334455667788"
        dev = can_listener.get_device(7)
        dev.hardware_uid = full_uid
        dev.full_uid = full_uid
        dev.update_lock_state(1)
        dev.mark_seen()

        self.assertTrue(dev.online)

        # 4-byte unassigned frame arrives
        can_listener.running = True
        frame = CANFrame(
            arbitration_id=0x57F,
            data=bytes.fromhex("11223344")
        )
        can_listener.handle_frame(frame)

        self.assertFalse(dev.online)
        self.assertFalse(dev.is_online)
        self.assertEqual(dev.status_text, "UNPROVISIONED")

    def test_deduplication_cleans_up_from_db_when_not_in_memory(self):
        """
        Verify that deduplication queries the database to find and remove ghost devices
        even if the old device entry is not currently loaded in listener memory.
        """
        old_uid = "cafebabe"
        with SessionLocal() as db:
            old_device = Device(
                device_id=20,
                name="Ghost Lock 20",
                hardware_uid=old_uid,
                lock_mode=1,
                last_seen=time.time()
            )
            db.add(old_device)
            db.commit()

        # Ensure it is NOT in listener memory
        can_listener.devices.pop(20, None)
        self.assertNotIn(20, can_listener.devices)

        # Re-assign same UID to device 21 via API
        res = self.client.post("/api/v1/locks", json={
            "uid": old_uid,
            "device_id": 21
        })
        self.assertEqual(res.status_code, 200)

        # Check DB: device 20 must be gone, device 21 exists
        with SessionLocal() as db:
            d20 = db.query(Device).filter(Device.device_id == 20).first()
            self.assertIsNone(d20, "Ghost device 20 should have been deleted from DB")
            d21 = db.query(Device).filter(Device.device_id == 21).first()
            self.assertIsNotNone(d21, "Device 21 should exist in DB")
            self.assertEqual(d21.hardware_uid, old_uid)

    # =========================================================================
    # 4. Device Deletion and Offline Pruning Tests
    # =========================================================================

    def test_delete_individual_device_success(self):
        """
        Verify DELETE /api/v1/devices/{device_id} removes device from
        both database and in-memory listener state.
        """
        with SessionLocal() as db:
            dev_db = Device(
                device_id=15,
                name="Test Lock 15",
                hardware_uid="deadbeef15",
                lock_mode=1,
                last_seen=time.time(),
            )
            db.add(dev_db)
            db.commit()

        dev_mem = can_listener.get_device(15)
        dev_mem.hardware_uid = "deadbeef15"
        self.assertIn(15, can_listener.devices)

        with patch("api.device.broadcast_sse_event") as mock_broadcast:
            res = self.client.delete("/api/v1/devices/15")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["status"], "deleted")
            self.assertEqual(data["device_id"], 15)
            mock_broadcast.assert_called_once_with("device_removed", {"device_id": 15})

        self.assertNotIn(15, can_listener.devices)
        with SessionLocal() as db:
            found = db.query(Device).filter(Device.device_id == 15).first()
            self.assertIsNone(found)

        # Deleting a non-existent device returns 404
        res404 = self.client.delete("/api/v1/devices/15")
        self.assertEqual(res404.status_code, 404)

    def test_delete_individual_device_via_locks_alias(self):
        """
        Verify DELETE /api/v1/locks/{device_id} works as an alias to /devices/{device_id}.
        """
        with SessionLocal() as db:
            dev_db = Device(
                device_id=16,
                name="Test Lock 16",
                hardware_uid="deadbeef16",
                lock_mode=1,
                last_seen=time.time(),
            )
            db.add(dev_db)
            db.commit()

        can_listener.get_device(16)
        self.assertIn(16, can_listener.devices)

        res = self.client.delete("/api/v1/locks/16")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(16, can_listener.devices)
        with SessionLocal() as db:
            found = db.query(Device).filter(Device.device_id == 16).first()
            self.assertIsNone(found)

    def test_prune_offline_devices(self):
        """
        Verify DELETE /api/v1/devices/prune-offline deletes all offline devices
        from DB and memory while keeping online devices intact.
        """
        now = time.time()
        with SessionLocal() as db:
            db.add(Device(device_id=10, name="Lock 10", last_seen=now))
            db.add(Device(device_id=11, name="Lock 11", last_seen=now - 100.0))
            db.add(Device(device_id=12, name="Lock 12", last_seen=now - 200.0))
            db.commit()

        # Dev 10: Online in memory
        dev10 = can_listener.get_device(10)
        dev10.last_seen = now
        dev10.offline_timeout = 60.0

        # Dev 11: Offline in memory
        dev11 = can_listener.get_device(11)
        dev11.last_seen = now - 100.0
        dev11.offline_timeout = 5.0
        dev11.status = "OFFLINE"

        # Dev 12: Offline in DB only (not in memory)
        can_listener.devices.pop(12, None)

        self.assertIn(10, can_listener.devices)
        self.assertIn(11, can_listener.devices)
        self.assertNotIn(12, can_listener.devices)

        with patch("api.device.broadcast_sse_event") as mock_broadcast:
            res = self.client.delete("/api/v1/devices/prune-offline")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertIn(data["status"], ("pruned", "success"))
            pruned = data["pruned_device_ids"]
            self.assertIn(11, pruned)
            self.assertIn(12, pruned)
            self.assertNotIn(10, pruned)

            # Check broadcast occurred for each pruned ID
            broadcast_ids = [
                call.args[1]["device_id"]
                for call in mock_broadcast.call_args_list
                if call.args[0] == "device_removed"
            ]
            self.assertIn(11, broadcast_ids)
            self.assertIn(12, broadcast_ids)
            self.assertNotIn(10, broadcast_ids)

        # Dev 10 still online and in memory and DB
        self.assertIn(10, can_listener.devices)
        with SessionLocal() as db:
            self.assertIsNotNone(db.query(Device).filter(Device.device_id == 10).first())
            self.assertIsNone(db.query(Device).filter(Device.device_id == 11).first())
            self.assertIsNone(db.query(Device).filter(Device.device_id == 12).first())

        # Calling prune again when all are online returns empty pruned list
        res_empty = self.client.delete("/api/v1/devices/prune-offline")
        self.assertEqual(res_empty.status_code, 200)
        self.assertEqual(res_empty.json()["pruned_device_ids"], [])

    def test_prune_offline_devices_via_locks_alias(self):
        """
        Verify DELETE /api/v1/locks/prune-offline works as an alias.
        """
        now = time.time()
        with SessionLocal() as db:
            db.add(Device(device_id=30, name="Lock 30", last_seen=now - 50.0))
            db.commit()

        dev30 = can_listener.get_device(30)
        dev30.last_seen = now - 50.0
        dev30.offline_timeout = 1.0
        dev30.status = "OFFLINE"

        res = self.client.delete("/api/v1/locks/prune-offline")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn(30, data["pruned_device_ids"])
        self.assertNotIn(30, can_listener.devices)

        # Calling prune again should yield empty list since device 30 was removed
        res2 = self.client.delete("/api/v1/locks/prune-offline")
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.json()["pruned_device_ids"], [])


if __name__ == "__main__":
    unittest.main()
