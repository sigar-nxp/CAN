"""
Unit and regression tests for the offline fitness-gym locker mode:
free locker selection, 1-card-1-locker enforcement, auto-provisioning of locks
arriving on the staging ID 1, locker renaming and the admin force-release.
"""

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

import api.device as api_device
from app import app
from canbus.constants import (
    AUTH_ACTION_CLOSE,
    AUTH_ACTION_NONE,
    AUTH_ACTION_OPEN,
    AUTH_RESP,
    AUTH_RESULT_ALLOW,
    AUTH_RESULT_DENY,
    BITRATE_50K,
    CAN_COMMAND,
    CAN_HEALTH,
    CAN_STATUS,
    DELETE_WL_SLOT,
    FACTORY_RESET,
    OPEN_HOLD,
    OPEN_RESET,
)
from canbus.listener import CardEvent, UnassignedDevice
from canbus.protocol import CANFrame
from config import settings
from config.globals import can_listener, gym_service
from database.database import SessionLocal, ensure_db_schema
from models.device import Device
from models.log import EventLog
from services.gym_provisioning import GymProvisioner
from services.gym_service import GymLockerService, GymCommandError

CARD_A = "04a1b2c3"
CARD_B = "0badc0de"
HW_UID_FULL_SUFFIX = "0011223344556677"


class FrameRecorder:
    """CAN sender double that records every frame and can simulate bus failures."""

    def __init__(self):
        self.frames = []
        self.result = True
        self.on_frame = None

    def __call__(self, frame):
        self.frames.append(frame)
        if self.on_frame is not None:
            self.on_frame(frame)
        return self.result

    def auth_responses(self, device_id):
        """Returns (result, action) for each AUTH_RESP sent to the node."""
        return [
            (f.data[3], f.data[4])
            for f in self.frames
            if f.arbitration_id == (CAN_COMMAND | device_id) and f.data[1] == AUTH_RESP
        ]

    def commands(self, device_id):
        """Returns the command opcodes sent to the node."""
        return [f.data[1] for f in self.frames if f.arbitration_id == (CAN_COMMAND | device_id)]


class FakeProvisioning:
    """Stands in for ProvisioningService: 'assigns' the ID without touching the CAN bus."""

    def __init__(self, listener):
        self.listener = listener
        self.calls = []

    def assign_device_id(self, uid32_hex, target_device_id, bitrate_code=5, timeout=5.0):
        self.calls.append((uid32_hex, target_device_id, bitrate_code))
        dev = self.listener.get_device(target_device_id)
        dev.mark_seen()
        with self.listener.lock:
            self.listener.unassigned_devices.pop(uid32_hex, None)
        return {
            "device_id": target_device_id,
            "full_uid_hex": uid32_hex + HW_UID_FULL_SUFFIX,
            "status": "provisioned",
        }


def auth_request_frames(device_id, card_hex, requested_action=AUTH_ACTION_CLOSE, lock_id=1):
    """Builds the STATUS 0x11 frame(s) a lock sends for a card it does not know locally."""
    card = bytes.fromhex(card_hex)
    frames = [CANFrame(CAN_STATUS | device_id, bytes([0x11, lock_id, requested_action, len(card)]) + card[:4])]
    if len(card) > 4:
        frames.append(CANFrame(CAN_STATUS | device_id, bytes([0x14, lock_id, 0]) + card[4:7] + b"\x00\x00"))
    return frames


class GymTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        self._previous_mode = settings.get_locker_operation_mode()
        settings.set_locker_operation_mode("gym")
        self._wipe()

        self.sender = FrameRecorder()
        self.events = []
        self.service = GymLockerService(
            can_listener,
            sender=self.sender,
            broadcaster=lambda event, data: self.events.append((event, data)),
            command_gap_s=0,
        )

    def tearDown(self):
        settings.set_locker_operation_mode(self._previous_mode)
        self._wipe()

    @staticmethod
    def _wipe():
        can_listener.devices.clear()
        can_listener.unassigned_devices.clear()
        with SessionLocal() as db:
            db.query(Device).delete()
            db.query(EventLog).delete()
            db.commit()

    @staticmethod
    def make_locker(device_id, online=True):
        dev = can_listener.get_device(device_id)
        if online:
            dev.update_lock_state(1)
            dev.mark_seen()
        return dev

    @staticmethod
    def db_row(device_id):
        with SessionLocal() as db:
            row = db.get(Device, device_id)
            if row is None:
                return None
            db.expunge(row)
            return row

    @staticmethod
    def logged_events(event_type, device_id=None):
        with SessionLocal() as db:
            query = db.query(EventLog).filter(EventLog.event_type == event_type)
            if device_id is not None:
                query = query.filter(EventLog.device_id == device_id)
            return query.all()


# =============================================================================
# 1. Free locker selection
# =============================================================================

class TestFreeLockerSelection(GymTestBase):
    def test_new_devices_default_to_vacant_with_default_name(self):
        dev = self.make_locker(5)
        row = self.db_row(5)
        self.assertEqual(row.locker_state, "vacant")
        self.assertIsNone(row.assigned_card_uid)
        self.assertIsNone(row.occupied_at)
        self.assertEqual(row.custom_name, "Locker 5")
        self.assertEqual(dev.locker_state, "vacant")
        self.assertEqual(dev.display_name, "Locker 5")

    def test_vacant_locker_is_occupied_by_free_card(self):
        dev = self.make_locker(2)

        decision = self.service.handle_auth_request(2, CARD_A, requested_action=AUTH_ACTION_CLOSE)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.outcome, "occupied")
        row = self.db_row(2)
        self.assertEqual(row.locker_state, "occupied")
        self.assertEqual(row.assigned_card_uid, CARD_A)
        self.assertIsNotNone(row.occupied_at)
        self.assertEqual(dev.locker_state, "occupied")
        self.assertEqual(dev.assigned_card_uid, CARD_A)
        # The lock is told to close the bolt through the existing AUTH_RESP command
        self.assertEqual(self.sender.auth_responses(2), [(AUTH_RESULT_ALLOW, AUTH_ACTION_CLOSE)])
        # Dashboards get an SSE device_update carrying the new state
        event, payload = self.events[-1]
        self.assertEqual(event, "device_update")
        self.assertEqual(payload["device_id"], 2)
        self.assertEqual(payload["details"]["locker_state"], "occupied")
        self.assertEqual(payload["details"]["assigned_card_uid"], CARD_A)
        self.assertTrue(payload["details"]["occupied_at"].endswith("Z"))
        self.assertEqual(len(self.logged_events("LOCKER_OCCUPIED", 2)), 1)

    def test_owner_card_releases_occupied_locker(self):
        dev = self.make_locker(2)
        self.service.handle_auth_request(2, CARD_A, requested_action=AUTH_ACTION_CLOSE)
        self.sender.frames.clear()

        decision = self.service.handle_auth_request(2, CARD_A, requested_action=AUTH_ACTION_OPEN)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.outcome, "released")
        row = self.db_row(2)
        self.assertEqual(row.locker_state, "vacant")
        self.assertIsNone(row.assigned_card_uid)
        self.assertIsNone(row.occupied_at)
        self.assertEqual(dev.locker_state, "vacant")
        self.assertIsNone(dev.assigned_card_uid)
        self.assertEqual(self.sender.auth_responses(2), [(AUTH_RESULT_ALLOW, AUTH_ACTION_OPEN)])
        self.assertEqual(self.events[-1][1]["details"]["locker_state"], "vacant")
        self.assertEqual(len(self.logged_events("LOCKER_RELEASED", 2)), 1)

    def test_different_card_is_denied_on_occupied_locker(self):
        self.make_locker(2)
        self.service.handle_auth_request(2, CARD_A)
        self.sender.frames.clear()

        decision = self.service.handle_auth_request(2, CARD_B, requested_action=AUTH_ACTION_OPEN)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.outcome, "denied_not_owner")
        row = self.db_row(2)
        self.assertEqual(row.locker_state, "occupied")
        self.assertEqual(row.assigned_card_uid, CARD_A)
        self.assertEqual(self.sender.auth_responses(2), [(AUTH_RESULT_DENY, AUTH_ACTION_NONE)])
        self.assertEqual(len(self.logged_events("ACCESS_DENIED", 2)), 1)

    def test_one_card_can_only_occupy_one_locker(self):
        self.make_locker(2)
        self.make_locker(3)
        self.service.handle_auth_request(2, CARD_A)
        self.sender.frames.clear()

        decision = self.service.handle_auth_request(3, CARD_A)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.outcome, "denied_card_in_use")
        self.assertEqual(decision.other_device_id, 2)
        self.assertEqual(self.db_row(3).locker_state, "vacant")
        self.assertIsNone(self.db_row(3).assigned_card_uid)
        self.assertEqual(self.sender.auth_responses(3), [(AUTH_RESULT_DENY, AUTH_ACTION_NONE)])

        # Once the first locker is released the same card may take another one
        self.service.handle_auth_request(2, CARD_A)
        retry = self.service.handle_auth_request(3, CARD_A)
        self.assertTrue(retry.allowed)
        self.assertEqual(self.db_row(3).assigned_card_uid, CARD_A)

    def test_card_uid_comparison_ignores_formatting(self):
        self.make_locker(2)
        self.make_locker(3)
        self.service.handle_auth_request(2, "04:A1:B2:C3")
        self.assertEqual(self.db_row(2).assigned_card_uid, CARD_A)
        self.assertFalse(self.service.handle_auth_request(3, "04a1b2c3").allowed)
        self.assertTrue(self.service.handle_auth_request(2, bytes.fromhex(CARD_A)).allowed)
        self.assertEqual(self.db_row(2).locker_state, "vacant")

    def test_four_byte_uid_is_not_a_prefix_match_for_longer_uid(self):
        self.make_locker(2)
        self.make_locker(3)
        self.service.handle_auth_request(2, "04a1b2c3d4e5f6")
        # A different card whose UID happens to be the first four bytes must be treated as distinct
        self.assertTrue(self.service.handle_auth_request(3, "04a1b2c3").allowed)
        self.assertFalse(self.service.handle_auth_request(2, "04a1b2c3").allowed)

    def test_invalid_card_uid_is_denied(self):
        self.make_locker(2)
        decision = self.service.handle_auth_request(2, "")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.outcome, "denied_invalid_card")
        self.assertEqual(self.db_row(2).locker_state, "vacant")
        self.assertEqual(self.sender.auth_responses(2), [(AUTH_RESULT_DENY, AUTH_ACTION_NONE)])

    def test_requests_are_ignored_outside_gym_mode(self):
        self.make_locker(2)
        settings.set_locker_operation_mode("standard")

        self.assertEqual(self.service.handle_auth_request(2, CARD_A).outcome, "ignored")
        self.assertIsNone(self.service.handle_card_event(CardEvent(2, 1, "auth_request", CARD_A, 1)))

        self.assertEqual(self.sender.frames, [])
        self.assertEqual(self.db_row(2).locker_state, "vacant")

    def test_failed_transmit_rolls_back_occupancy(self):
        self.make_locker(2)
        self.sender.result = False

        decision = self.service.handle_auth_request(2, CARD_A)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.outcome, "error")
        self.assertEqual(self.db_row(2).locker_state, "vacant")
        self.assertIsNone(self.db_row(2).assigned_card_uid)

    def test_failed_transmit_keeps_locker_occupied_on_release(self):
        self.make_locker(2)
        self.service.handle_auth_request(2, CARD_A)
        self.sender.result = False

        decision = self.service.handle_auth_request(2, CARD_A)

        self.assertEqual(decision.outcome, "error")
        self.assertEqual(self.db_row(2).locker_state, "occupied")
        self.assertEqual(self.db_row(2).assigned_card_uid, CARD_A)

    def test_simultaneous_swipes_of_one_card_claim_only_one_locker(self):
        self.make_locker(2)
        self.make_locker(3)
        barrier = threading.Barrier(2)
        decisions = {}

        def swipe(device_id):
            barrier.wait()
            decisions[device_id] = self.service.handle_auth_request(device_id, CARD_A)

        threads = [threading.Thread(target=swipe, args=(i,)) for i in (2, 3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(sum(1 for d in decisions.values() if d.allowed), 1)
        with SessionLocal() as db:
            occupied = db.query(Device).filter(Device.locker_state == "occupied").count()
        self.assertEqual(occupied, 1)


# =============================================================================
# 2. Lock-side events mirrored into the database
# =============================================================================

class TestHardwareEventSync(GymTestBase):
    def test_local_open_by_owner_vacates_locker(self):
        self.make_locker(2)
        self.service.handle_auth_request(2, CARD_A)

        # The lock has the owner in its ephemeral slot, answers locally and reports OPEN_OK
        self.service.handle_local_action(2, CARD_A, 0x01)

        self.assertEqual(self.db_row(2).locker_state, "vacant")
        self.assertIsNone(self.db_row(2).assigned_card_uid)
        self.assertEqual(self.events[-1][1]["details"]["locker_state"], "vacant")

    def test_close_echo_of_a_fresh_claim_keeps_locker_occupied(self):
        self.make_locker(2)
        self.service.handle_auth_request(2, CARD_A)

        self.service.handle_local_action(2, CARD_A, 0x02)  # LOCAL_CLOSE_OK

        self.assertEqual(self.db_row(2).locker_state, "occupied")

    def test_firmware_guest_credential_auto_delete_vacates_locker(self):
        self.make_locker(2)
        self.service.handle_auth_request(2, CARD_A)

        self.service.handle_wl_auto_delete(2)

        self.assertEqual(self.db_row(2).locker_state, "vacant")

    def test_four_byte_auth_request_frame_reaches_the_service(self):
        self.make_locker(2)
        with patch.object(can_listener, "running", True), \
             patch.object(can_listener, "card_event_handler", self.service.handle_card_event):
            for frame in auth_request_frames(2, CARD_A):
                can_listener.handle_frame(frame)

        self.assertEqual(self.db_row(2).assigned_card_uid, CARD_A)
        self.assertEqual(self.sender.auth_responses(2), [(AUTH_RESULT_ALLOW, AUTH_ACTION_CLOSE)])

    def test_seven_byte_auth_request_waits_for_part2(self):
        self.make_locker(2)
        card = "04a1b2c3d4e5f6"
        frames = auth_request_frames(2, card)
        with patch.object(can_listener, "running", True), \
             patch.object(can_listener, "card_event_handler", self.service.handle_card_event):
            can_listener.handle_frame(frames[0])
            self.assertEqual(self.db_row(2).locker_state, "vacant")
            can_listener.handle_frame(frames[1])

        self.assertEqual(self.db_row(2).assigned_card_uid, card)

    def test_local_open_frame_with_seven_byte_card_vacates_locker(self):
        self.make_locker(2)
        card = "04a1b2c3d4e5f6"
        self.service.handle_auth_request(2, card)
        raw = bytes.fromhex(card)
        local_open = [
            CANFrame(CAN_STATUS | 2, bytes([0x10, 1, 0x01, 7]) + raw[:4]),
            CANFrame(CAN_STATUS | 2, bytes([0x13, 1, 0]) + raw[4:7] + b"\x00\x00"),
        ]
        with patch.object(can_listener, "running", True), \
             patch.object(can_listener, "card_event_handler", self.service.handle_card_event):
            for frame in local_open:
                can_listener.handle_frame(frame)

        self.assertEqual(self.db_row(2).locker_state, "vacant")

    def test_failing_hook_does_not_break_frame_processing(self):
        dev = self.make_locker(2)
        boom = MagicMock(side_effect=RuntimeError("boom"))
        with patch.object(can_listener, "running", True), \
             patch.object(can_listener, "card_event_handler", boom):
            for frame in auth_request_frames(2, CARD_A):
                can_listener.handle_frame(frame)
        self.assertTrue(boom.called)
        self.assertEqual(dev.last_scanned_card, CARD_A)


# =============================================================================
# 3. Auto-provisioning of locks arriving on staging ID 1
# =============================================================================

class TestAutoProvisioning(GymTestBase):
    def setUp(self):
        super().setUp()
        self.fake_provisioning = FakeProvisioning(can_listener)
        self.provisioner = GymProvisioner(
            can_listener,
            sender=self.sender,
            provisioning=self.fake_provisioning,
            broadcaster=lambda event, data: self.events.append((event, data)),
            window_s=3.0,
            quiet_s=0.3,
            cooldown_s=60.0,
            poll_s=0.02,
        )

    def announce_after_factory_reset(self, *uid32_hex_list):
        """Simulates locks rebooting as unassigned nodes as soon as FACTORY_RESET reaches node 1."""
        def on_frame(frame):
            if frame.arbitration_id == (CAN_COMMAND | 1) and frame.data[1] == FACTORY_RESET:
                for uid_hex in uid32_hex_list:
                    can_listener.unassigned_devices[uid_hex] = UnassignedDevice(bytes.fromhex(uid_hex))
        self.sender.on_frame = on_frame

    def test_lock_on_id_1_gets_lowest_free_id(self):
        self.make_locker(2)
        self.make_locker(3)
        self.make_locker(5)

        result = self.provisioner.provision_uid("a1b2c3d4")

        self.assertEqual(result["device_id"], 4)
        self.assertFalse(result["reused"])
        self.assertEqual(self.fake_provisioning.calls, [("a1b2c3d4", 4, BITRATE_50K)])
        row = self.db_row(4)
        self.assertEqual(row.hardware_uid, "a1b2c3d4" + HW_UID_FULL_SUFFIX)
        self.assertEqual(row.custom_name, "Locker 4")
        self.assertEqual(row.locker_state, "vacant")
        event, payload = self.events[-1]
        self.assertEqual(event, "device_update")
        self.assertEqual(payload["device_id"], 4)
        self.assertEqual(payload["details"]["hardware_uid"], "a1b2c3d4" + HW_UID_FULL_SUFFIX)

    def test_known_hardware_uid_keeps_its_id_and_name_without_duplicate(self):
        self.make_locker(7)
        self.service.rename(7, "Spind 07")
        self.service.handle_auth_request(7, CARD_A)
        with SessionLocal() as db:
            db.get(Device, 7).hardware_uid = "a1b2c3d4" + HW_UID_FULL_SUFFIX
            db.commit()
        can_listener.devices[7].hardware_uid = "a1b2c3d4" + HW_UID_FULL_SUFFIX

        result = self.provisioner.provision_uid("a1b2c3d4")

        self.assertEqual(result["device_id"], 7)
        self.assertTrue(result["reused"])
        with SessionLocal() as db:
            self.assertEqual(db.query(Device).count(), 1)
        row = self.db_row(7)
        self.assertEqual(row.custom_name, "Spind 07")
        # The factory reset wiped the lock's credentials, so the old claim is void
        self.assertEqual(row.locker_state, "vacant")
        self.assertIsNone(row.assigned_card_uid)

    def test_full_cycle_resets_staging_node_and_assigns_next_free_id(self):
        self.make_locker(1)  # the freshly onboarded lock, visible on the staging ID
        self.make_locker(2)
        self.announce_after_factory_reset("deadbeef")

        results = self.provisioner.run_cycle()

        # Standard UID assignment follows a FACTORY_RESET (0xFB) addressed to node 1
        self.assertEqual(self.sender.frames[0].arbitration_id, CAN_COMMAND | 1)
        self.assertEqual(self.sender.frames[0].data[1], FACTORY_RESET)
        self.assertEqual([r["device_id"] for r in results], [3])
        self.assertEqual(self.fake_provisioning.calls, [("deadbeef", 3, BITRATE_50K)])
        # Node 1 stays reserved: it is gone from memory, database and the dashboards
        self.assertNotIn(1, can_listener.devices)
        self.assertIsNone(self.db_row(1))
        self.assertIn(("device_removed", {"device_id": 1}), self.events)
        self.assertEqual(self.db_row(3).custom_name, "Locker 3")
        self.assertEqual(len(self.logged_events("DEVICE_PROVISIONED", 3)), 1)

    def test_several_locks_resetting_together_get_distinct_ids(self):
        self.make_locker(1)
        self.announce_after_factory_reset("11111111", "22222222")

        results = self.provisioner.run_cycle()

        self.assertEqual(sorted(r["device_id"] for r in results), [2, 3])
        self.assertEqual(len({r["hardware_uid"] for r in results}), 2)

    def test_announcements_older_than_the_reset_are_ignored(self):
        self.make_locker(1)
        stale = UnassignedDevice(bytes.fromhex("cafebabe"))
        stale.last_seen = time.time() - 100
        can_listener.unassigned_devices["cafebabe"] = stale
        self.provisioner.window_s = 0.3

        results = self.provisioner.run_cycle()

        self.assertEqual(results, [])
        self.assertEqual(self.fake_provisioning.calls, [])
        self.assertEqual(len(self.logged_events("PROVISION_FAILED")), 1)

    def test_exhausted_id_range_leaves_staging_lock_untouched(self):
        for node_id in range(settings.GYM_NODE_ID_MIN, settings.GYM_NODE_ID_MAX + 1):
            self.make_locker(node_id, online=False)
        self.make_locker(1)

        results = self.provisioner.run_cycle()

        self.assertEqual(results, [])
        self.assertEqual(self.sender.frames, [])
        self.assertIn(1, can_listener.devices)

    def test_assignment_never_leaves_the_gym_range(self):
        for node_id in range(settings.GYM_NODE_ID_MIN, settings.GYM_NODE_ID_MAX):
            self.make_locker(node_id, online=False)
        result = self.provisioner.provision_uid("a1b2c3d4")
        self.assertEqual(result["device_id"], settings.GYM_NODE_ID_MAX)

    def test_transmit_failure_aborts_without_removing_the_staging_node(self):
        self.make_locker(1)
        self.sender.result = False

        self.assertEqual(self.provisioner.run_cycle(), [])
        self.assertIn(1, can_listener.devices)

    def test_failed_assignment_is_retried_once(self):
        self.make_locker(1)
        self.announce_after_factory_reset("deadbeef")
        original = self.fake_provisioning.assign_device_id
        attempts = {"n": 0}

        def flaky(*args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise TimeoutError("no reply")
            return original(*args, **kwargs)

        self.fake_provisioning.assign_device_id = flaky
        results = self.provisioner.run_cycle()

        self.assertEqual(attempts["n"], 2)
        self.assertEqual([r["device_id"] for r in results], [2])

    def test_staging_frame_starts_a_single_cycle_in_gym_mode_only(self):
        started = threading.Event()
        release = threading.Event()

        def fake_cycle():
            started.set()
            release.wait(timeout=5)
            return []

        self.provisioner.run_cycle = fake_cycle
        try:
            self.assertTrue(self.provisioner.on_staging_frame(1))
            self.assertTrue(started.wait(timeout=2))
            # While a cycle runs, and during the cooldown after it, further frames are ignored
            self.assertFalse(self.provisioner.on_staging_frame(1))
        finally:
            release.set()
        deadline = time.time() + 2
        while self.provisioner._inflight and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.provisioner.on_staging_frame(1))

        settings.set_locker_operation_mode("standard")
        self.provisioner._next_allowed = 0.0
        self.assertFalse(self.provisioner.on_staging_frame(1))

    def test_listener_notifies_hook_only_for_staging_node_traffic(self):
        hook = MagicMock()
        with patch.object(can_listener, "running", True), \
             patch.object(can_listener, "staging_frame_handler", hook):
            can_listener.handle_frame(CANFrame(CAN_STATUS | 1, bytes([0x01, 1, 1, 0])))
            hook.assert_called_once_with(1)
            # In Gym Mode, node 1 must never be registered or persisted
            self.assertNotIn(1, can_listener.devices)
            self.assertIsNone(self.db_row(1))
            hook.reset_mock()
            can_listener.handle_frame(CANFrame(CAN_STATUS | 6, bytes([0x01, 1, 1, 0])))
            hook.assert_not_called()

    def test_telemetry_from_id_1_triggers_reassignment_without_leaving_device_id_1(self):
        """
        Regression test: When a factory-tested lock boots on Node ID 1 and sends status/health
        telemetry (e.g. 0x201 StatusBasic, 0x301 HealthShort), CANListener must trigger auto-provisioning
        WITHOUT creating a Device(id=1) record in SQLite and WITHOUT leaving device_id=1 in
        can_listener.devices or the GET /devices endpoint.
        """
        # Ensure ID 1 is not in memory or DB initially
        self.assertNotIn(1, can_listener.devices)
        self.assertIsNone(self.db_row(1))

        self.announce_after_factory_reset("c0ffee01")

        with patch.object(can_listener, "running", True), \
             patch.object(can_listener, "staging_frame_handler", self.provisioner.on_staging_frame):

            # Send status telemetry frame from Node 1 (CAN_STATUS | 1 = 0x201)
            can_listener.handle_frame(CANFrame(CAN_STATUS | 1, bytes([0x01, 1, 1, 0, 0, 0, 0, 0])))

            # Also simulate a health telemetry frame (CAN_HEALTH | 1 = 0x301)
            can_listener.handle_frame(CANFrame(CAN_HEALTH | 1, bytes([0x00, 0x0C, 0x80, 0x19, 0, 0, 0, 0])))

            # Wait for background staging thread to finish
            deadline = time.time() + 2.0
            while self.provisioner._inflight and time.time() < deadline:
                time.sleep(0.02)

        # 1. Device 1 was never created or persisted in SQLite
        self.assertIsNone(self.db_row(1))
        # 2. Device 1 is not in can_listener.devices
        self.assertNotIn(1, can_listener.devices)
        # 3. GET /api/v1/devices does not contain device_id=1
        resp = self.client.get("/api/v1/devices")
        self.assertEqual(resp.status_code, 200)
        device_ids = [d["device_id"] for d in resp.json()]
        self.assertNotIn(1, device_ids)
        # 4. GET /api/v1/devices/1 returns 404 in gym mode
        resp_single = self.client.get("/api/v1/devices/1")
        self.assertEqual(resp_single.status_code, 404)
        # 5. Lock was successfully assigned to lowest free gym ID (Node 2)
        self.assertIn(2, can_listener.devices)
        self.assertIsNotNone(self.db_row(2))
        self.assertIn(2, device_ids)

    def test_telemetry_on_id_1_for_known_lock_reassigns_same_id_preserving_custom_name(self):
        """
        When a known lock (previously provisioned as Node 5 with custom name "VIP Locker")
        boots on Node 1 after EOL or reset, auto-provisioning must reassign it back to
        Node 5, preserving its custom name, and leave no ghost Node 1 in DB or device list.
        """
        dev5 = self.make_locker(5)
        self.service.rename(5, "VIP Locker")
        with SessionLocal() as db:
            row5 = db.get(Device, 5)
            row5.hardware_uid = "feedbeef" + HW_UID_FULL_SUFFIX
            db.commit()
        dev5.hardware_uid = "feedbeef" + HW_UID_FULL_SUFFIX

        self.announce_after_factory_reset("feedbeef")

        with patch.object(can_listener, "running", True), \
             patch.object(can_listener, "staging_frame_handler", self.provisioner.on_staging_frame):

            can_listener.handle_frame(CANFrame(CAN_STATUS | 1, bytes([0x01, 1, 1, 0, 0, 0, 0, 0])))

            deadline = time.time() + 2.0
            while self.provisioner._inflight and time.time() < deadline:
                time.sleep(0.02)

        self.assertNotIn(1, can_listener.devices)
        self.assertIsNone(self.db_row(1))
        row5 = self.db_row(5)
        self.assertEqual(row5.custom_name, "VIP Locker")
        self.assertEqual(row5.locker_state, "vacant")
        self.assertIn(5, can_listener.devices)

    def test_provisioning_from_0x7f_does_not_leave_node_127(self):
        """
        Regression test: During unassigned discovery or factory reset, a lock communicates
        on Node ID 127 (0x7F). Receiving frames on 0x7F and provisioning the lock to Node 2
        must never create or leave a record or locker card for Node 127 in SQLite,
        can_listener.devices, or the GET /api/v1/devices endpoint.
        """
        self.assertNotIn(127, can_listener.devices)
        self.assertIsNone(self.db_row(127))

        with patch.object(can_listener, "running", True):
            # 0. Outbound/echoed master broadcast frames (e.g. 0x47F Master Beacon 0xB0)
            # must be ignored and never logged as UNPROVISIONED_FRAME on node 127.
            can_listener.handle_frame(CANFrame(0x47F, bytes([0xB0, 1, 0, 0, 0, 0, 0, 0])))

            # 1. Simulate unassigned announcement (0x57F ID_REQUEST) from node 127 (0x7F)
            can_listener.handle_frame(CANFrame(0x57F, bytes([0x28, 0x11, 0x22, 0x33, 0x44])))

            # 2. Simulate in-flight status or health telemetry from node 127 (0x7F)
            can_listener.handle_frame(CANFrame(CAN_STATUS | 0x7F, bytes([0x01, 1, 1, 0, 0, 0, 0, 0])))
            can_listener.handle_frame(CANFrame(CAN_HEALTH | 0x7F, bytes([0x00, 0x0C, 0x80, 0x19, 0, 0, 0, 0])))

        # Confirm 0x47F master broadcast did not produce an UNPROVISIONED_FRAME log,
        # while non-master unprovisioned frames on 0x7F still log UNPROVISIONED_FRAME.
        with SessionLocal() as db:
            unprov_logs = (
                db.query(EventLog)
                .filter(EventLog.event_type == "UNPROVISIONED_FRAME")
                .all()
            )
            unprov_can_ids = {log.can_id for log in unprov_logs}
            self.assertNotIn("0x47F", unprov_can_ids)
            self.assertIn(f"0x{CAN_STATUS | 0x7F:03X}", unprov_can_ids)

        # Verify that node 127 was NOT created in DB or listener devices
        self.assertNotIn(127, can_listener.devices)
        self.assertIsNone(self.db_row(127))

        # 3. Provision the lock from unassigned to Node 2
        result = self.provisioner.provision_uid("11223344")
        self.assertEqual(result["device_id"], 2)

        # 4. Confirm Node 127 is completely absent from DB, listener cache, and REST endpoints
        self.assertNotIn(127, can_listener.devices)
        self.assertIsNone(self.db_row(127))
        self.assertIn(("device_removed", {"device_id": 127}), self.events)

        resp = self.client.get("/api/v1/devices")
        self.assertEqual(resp.status_code, 200)
        device_ids = [d["device_id"] for d in resp.json()]
        self.assertNotIn(127, device_ids)
        self.assertIn(2, device_ids)

        # GET /api/v1/devices/127 returns 404
        resp_127 = self.client.get("/api/v1/devices/127")
        self.assertEqual(resp_127.status_code, 404)


# =============================================================================
# 4. Rename, admin force-release and mode configuration (REST API)
# =============================================================================

class TestGymRestApi(GymTestBase):
    def setUp(self):
        super().setUp()
        # The API routes use the global service; give it the recording sender for this test
        self._sender_patch = patch.object(gym_service, "sender", self.sender)
        self._gap_patch = patch.object(gym_service, "command_gap_s", 0)
        self._bcast_patch = patch.object(
            gym_service, "broadcaster", lambda event, data: self.events.append((event, data))
        )
        for p in (self._sender_patch, self._gap_patch, self._bcast_patch):
            p.start()
            self.addCleanup(p.stop)

    def test_rename_via_patch_persists_and_is_broadcast(self):
        dev = self.make_locker(2)

        resp = self.client.patch("/api/v1/devices/2/name", json={"name": "  Spind 01  "})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "Spind 01")
        row = self.db_row(2)
        self.assertEqual(row.custom_name, "Spind 01")
        self.assertEqual(row.name, "Spind 01")
        self.assertEqual(dev.display_name, "Spind 01")
        self.assertEqual(self.client.get("/api/v1/devices/2").json()["name"], "Spind 01")
        listed = [d for d in self.client.get("/api/v1/devices").json() if d["device_id"] == 2][0]
        self.assertEqual(listed["custom_name"], "Spind 01")
        self.assertEqual(self.events[-1][1]["details"]["custom_name"], "Spind 01")
        self.assertEqual(len(self.logged_events("LOCKER_RENAMED", 2)), 1)

    def test_rename_via_post_alias(self):
        self.make_locker(2)
        resp = self.client.post("/api/v1/devices/2/rename", json={"name": "Spind 02"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.db_row(2).custom_name, "Spind 02")

    def test_legacy_patch_device_keeps_both_name_fields_in_sync(self):
        self.make_locker(2)
        resp = self.client.patch("/api/v1/devices/2", json={"name": "Legacy Name"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.db_row(2).custom_name, "Legacy Name")
        self.assertEqual(self.db_row(2).name, "Legacy Name")

    def test_rename_rejects_invalid_names(self):
        self.make_locker(2)
        for bad in ("", "   ", "x" * 65, "bad\nname"):
            resp = self.client.patch("/api/v1/devices/2/name", json={"name": bad})
            self.assertEqual(resp.status_code, 422, bad)
        self.assertEqual(self.db_row(2).custom_name, "Locker 2")

    def test_rename_unknown_device_returns_404_without_creating_it(self):
        resp = self.client.patch("/api/v1/devices/44/name", json={"name": "Ghost"})
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn(44, can_listener.devices)
        self.assertIsNone(self.db_row(44))

    def test_admin_force_release_vacates_and_unlocks(self):
        self.make_locker(2)
        self.make_locker(3)
        gym_service.handle_auth_request(2, CARD_A)
        self.sender.frames.clear()

        resp = self.client.post("/api/v1/devices/2/release")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "released")
        self.assertEqual(body["released_card_uid"], CARD_A)
        row = self.db_row(2)
        self.assertEqual(row.locker_state, "vacant")
        self.assertIsNone(row.assigned_card_uid)
        # Guest credential is wiped first, then the bolt is held open
        self.assertEqual(self.sender.commands(2), [DELETE_WL_SLOT, OPEN_HOLD])
        self.assertEqual(self.sender.frames[0].data[3], 1)  # Slot 1 (ephemeral)
        self.assertEqual(self.events[-1][1]["details"]["locker_state"], "vacant")
        self.assertEqual(len(self.logged_events("LOCKER_FORCE_RELEASE", 2)), 1)
        # The former owner is free to pick another locker right away
        self.assertTrue(gym_service.handle_auth_request(3, CARD_A).allowed)

    def test_force_release_of_vacant_locker_is_idempotent(self):
        self.make_locker(2)
        resp = self.client.post("/api/v1/devices/2/release")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["released_card_uid"])
        self.assertEqual(self.db_row(2).locker_state, "vacant")

    def test_force_release_unknown_device_returns_404(self):
        resp = self.client.post("/api/v1/devices/44/release")
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn(44, can_listener.devices)

    def test_force_release_transmit_failure_keeps_state_and_returns_503(self):
        self.make_locker(2)
        gym_service.handle_auth_request(2, CARD_A)
        self.sender.result = False

        resp = self.client.post("/api/v1/devices/2/release")

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(self.db_row(2).locker_state, "occupied")
        with self.assertRaises(GymCommandError):
            gym_service.force_release(2)

    def test_release_keeps_legacy_reset_behaviour_in_standard_mode(self):
        self.make_locker(2)
        settings.set_locker_operation_mode("standard")
        with patch.object(api_device.lock_service, "send") as legacy_send:
            resp = self.client.post("/api/v1/devices/2/release")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "open state reset"})
        self.assertEqual(legacy_send.call_args[0][0].data[1], OPEN_RESET)
        self.assertEqual(self.sender.frames, [])

    def test_locker_mode_can_be_read_and_switched(self):
        resp = self.client.get("/api/v1/system/locker-mode")
        self.assertEqual(resp.json()["mode"], "gym")
        self.assertEqual(resp.json()["node_id_range"], [2, 49])

        resp = self.client.put("/api/v1/system/locker-mode", json={"mode": "standard"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(settings.get_locker_operation_mode(), "standard")

        resp = self.client.put("/api/v1/system/locker-mode", json={"mode": "nonsense"})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(settings.get_locker_operation_mode(), "standard")

    def test_switching_to_gym_mode_purges_existing_node_1(self):
        settings.set_locker_operation_mode("standard")
        # In standard mode, node 1 can exist in memory and DB
        can_listener.get_device(1)
        self.assertIsNotNone(self.db_row(1))
        self.assertIn(1, can_listener.devices)

        # Switch to gym mode via API
        resp = self.client.put("/api/v1/system/locker-mode", json={"mode": "gym"})
        self.assertEqual(resp.status_code, 200)

        # Node 1 must be purged from DB and memory
        self.assertNotIn(1, can_listener.devices)
        self.assertIsNone(self.db_row(1))

    def test_locker_mode_persists_across_settings_and_db_reload(self):
        # Switch to standard mode via API
        resp = self.client.put("/api/v1/system/locker-mode", json={"mode": "standard"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(settings.get_locker_operation_mode(), "standard")
        self.assertFalse(settings.is_gym_mode())

        # Simulate in-memory state reset followed by reload from persistent SQLite storage
        settings.LOCKER_OPERATION_MODE = "gym"
        reloaded = settings.load_locker_operation_mode()
        self.assertEqual(reloaded, "standard")
        self.assertEqual(settings.get_locker_operation_mode(), "standard")
        self.assertFalse(settings.is_gym_mode())

        # Switch back to gym mode via API and verify persistence across reload
        resp_gym = self.client.put("/api/v1/system/locker-mode", json={"mode": "gym"})
        self.assertEqual(resp_gym.status_code, 200)
        settings.LOCKER_OPERATION_MODE = "standard"
        self.assertEqual(settings.load_locker_operation_mode(), "gym")
        self.assertTrue(settings.is_gym_mode())

    def test_gym_web_ui_route_renders_standalone_interface(self):
        resp = self.client.get("/gym")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("content-type", ""))
        html = resp.text
        self.assertIn("PS LOCKS", html)
        self.assertIn("Vacant", html)
        self.assertIn("Occupied", html)
        self.assertIn("Offline", html)
        self.assertIn("Force Release / Unlock", html)
        self.assertIn("Release All", html)
        self.assertIn("Beep / Locate", html)
        self.assertIn("/api/v1/devices/release-all", html)
        self.assertIn("/api/v1/system/locker-mode", html)
        self.assertIn("/api/v1/stream", html)
        self.assertNotIn("https://cdn.tailwindcss.com", html)

        # Verify technical dashboard (/) also exposes the mode switcher and /gym link
        root_resp = self.client.get("/", headers={"Accept": "text/html"})
        self.assertEqual(root_resp.status_code, 200)
        self.assertIn('href="/gym"', root_resp.text)
        self.assertIn("mode-switcher-gym", root_resp.text)
        self.assertIn("mode-switcher-standard", root_resp.text)

    def test_release_all_vacates_and_unlocks_every_occupied_locker(self):
        self.make_locker(2)
        self.make_locker(3)
        self.make_locker(4)  # stays vacant
        gym_service.handle_auth_request(2, CARD_A)
        gym_service.handle_auth_request(3, CARD_B)
        self.sender.frames.clear()
        self.events.clear()

        resp = self.client.post("/api/v1/devices/release-all")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "released_all")
        self.assertEqual(body["released_count"], 2)
        self.assertEqual(body["released_device_ids"], [2, 3])
        self.assertEqual(body["failed_device_ids"], [])

        # Both occupied lockers receive DELETE_WL_SLOT + OPEN_HOLD, while vacant locker 4 is untouched
        self.assertEqual(self.sender.commands(2), [DELETE_WL_SLOT, OPEN_HOLD])
        self.assertEqual(self.sender.commands(3), [DELETE_WL_SLOT, OPEN_HOLD])
        self.assertEqual(self.sender.commands(4), [])

        for dev_id in (2, 3, 4):
            row = self.db_row(dev_id)
            self.assertEqual(row.locker_state, "vacant")
            self.assertIsNone(row.assigned_card_uid)
            self.assertIsNone(row.occupied_at)

        # SSE updates were broadcast for both released lockers
        updated_ids = [payload["device_id"] for ev, payload in self.events if ev == "device_update"]
        self.assertEqual(updated_ids, [2, 3])

        # Calling release-all again when nothing is occupied is a clean no-op
        self.sender.frames.clear()
        empty_resp = self.client.post("/api/v1/devices/release-all")
        self.assertEqual(empty_resp.status_code, 200)
        self.assertEqual(empty_resp.json()["released_count"], 0)
        self.assertEqual(self.sender.frames, [])


# =============================================================================
# 5. Settings & database migration
# =============================================================================

class TestSettingsAndMigration(unittest.TestCase):
    def test_mode_defaults_to_gym_and_rejects_garbage_from_environment(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOCKER_OPERATION_MODE", None)
            self.assertEqual(settings._mode_from_environment(), "gym")
        with patch.dict(os.environ, {"LOCKER_OPERATION_MODE": " Standard "}):
            self.assertEqual(settings._mode_from_environment(), "standard")
        with patch.dict(os.environ, {"LOCKER_OPERATION_MODE": "party"}):
            self.assertEqual(settings._mode_from_environment(), "gym")
        with self.assertRaises(ValueError):
            settings.normalize_locker_mode("party")

    def test_node_id_plan_reserves_id_1_and_supports_48_lockers(self):
        self.assertEqual(settings.STAGING_NODE_ID, 1)
        self.assertEqual(settings.GYM_NODE_ID_MAX - settings.GYM_NODE_ID_MIN + 1, 48)

    def test_existing_database_is_migrated_with_gym_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "legacy.db")
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE devices (device_id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, "
                "lock_mode INTEGER, auto_close_timeout INTEGER, behavior_flags INTEGER, "
                "active_slot INTEGER, last_seen FLOAT)"
            )
            conn.executemany(
                "INSERT INTO devices (device_id, name) VALUES (?, ?)",
                [(5, "Lock 5"), (6, "Lock"), (7, "Spind A")],
            )
            conn.commit()
            conn.close()

            engine = create_engine(f"sqlite:///{path}")
            ensure_db_schema(bind=engine)
            ensure_db_schema(bind=engine)  # must be idempotent

            with engine.connect() as c:
                columns = {r[1] for r in c.execute(text("PRAGMA table_info(devices)"))}
                rows = c.execute(
                    text("SELECT device_id, custom_name, locker_state, assigned_card_uid, occupied_at "
                         "FROM devices ORDER BY device_id")
                ).fetchall()
            engine.dispose()

        self.assertTrue({"custom_name", "locker_state", "assigned_card_uid", "occupied_at"} <= columns)
        self.assertEqual(
            [tuple(r) for r in rows],
            [
                (5, "Locker 5", "vacant", None, None),
                (6, "Locker 6", "vacant", None, None),
                (7, "Spind A", "vacant", None, None),
            ],
        )


if __name__ == "__main__":
    unittest.main()