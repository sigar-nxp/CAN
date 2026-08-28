import unittest
from unittest.mock import patch

from canbus.commands import (
    open_lock,
    request_status,
    open_hold,
    open_reset,
    request_wl_info,
    request_wl_list_v2,
    wl_write_uid,
    wl_write_uid_part2,
    delete_wl_slot,
    wl_clear,
    set_wl_slot_policy,
    set_lock_mode,
    request_lock_mode,
    request_occupancy_state,
    auth_resp
)
from canbus.constants import *


class TestCANCommands(unittest.TestCase):
    @patch("canbus.commands._next_corr_id", return_value=42)
    def test_open_lock(self, mock_corr_id):
        frame = open_lock(device_id=5, lock_id=1)
        self.assertEqual(frame.arbitration_id, CAN_COMMAND | 5)
        self.assertEqual(list(frame.data), [1, OPEN_LOCK, 42])

    @patch("canbus.commands._next_corr_id", return_value=42)
    def test_request_status(self, mock_corr_id):
        frame = request_status(device_id=10, lock_id=0)
        self.assertEqual(frame.arbitration_id, CAN_COMMAND | 10)
        self.assertEqual(list(frame.data), [0, REQUEST_STATUS, 42])

    @patch("canbus.commands._next_corr_id", return_value=42)
    def test_open_hold(self, mock_corr_id):
        frame = open_hold(device_id=10, lock_id=0)
        self.assertEqual(frame.arbitration_id, CAN_COMMAND | 10)
        self.assertEqual(list(frame.data), [0, OPEN_HOLD, 42])

    @patch("canbus.commands._next_corr_id", return_value=42)
    def test_open_reset(self, mock_corr_id):
        frame = open_reset(device_id=15, lock_id=2)
        self.assertEqual(frame.arbitration_id, CAN_COMMAND | 15)
        self.assertEqual(list(frame.data), [2, OPEN_RESET, 42])

    @patch("canbus.commands._next_corr_id", return_value=42)
    def test_request_wl_info(self, mock_corr_id):
        frame = request_wl_info(device_id=3)
        self.assertEqual(frame.arbitration_id, CAN_COMMAND | 3)
        self.assertEqual(list(frame.data), [0, REQUEST_WL_INFO, 42])

    @patch("canbus.commands._next_corr_id", return_value=42)
    def test_request_wl_list_v2(self, mock_corr_id):
        frame = request_wl_list_v2(device_id=4, start_index=1, page_size=10, lock_id=0)
        self.assertEqual(frame.arbitration_id, CAN_COMMAND | 4)
        self.assertEqual(list(frame.data), [0, REQUEST_WL_LIST_V2, 42, 1, 10])

    @patch("canbus.commands._next_corr_id", return_value=42)
    def test_wl_write_uid(self, mock_corr_id):
        uid_bytes = bytes([0xAA, 0xBB, 0xCC, 0xDD])
        frame = wl_write_uid(device_id=1, slot_index=5, uid_len=4, uid_bytes=uid_bytes, lock_id=0)
        self.assertEqual(frame.arbitration_id, CAN_COMMAND | 1)
        self.assertEqual(list(frame.data), [0, WL_WRITE_UID, 42, 5, 4, 0xAA, 0xBB, 0xCC])

    @patch("canbus.commands._next_corr_id", return_value=42)
    def test_wl_write_uid_part2(self, mock_corr_id):
        uid_bytes = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF, 0x00])
        frame = wl_write_uid_part2(device_id=1, uid_bytes=uid_bytes, ttl_days=10, lock_id=0)
        self.assertEqual(frame.arbitration_id, CAN_COMMAND | 1)
        self.assertEqual(list(frame.data), [0, WL_WRITE_UID_PART2, 42, 0xDD, 0xEE, 0xFF, 0x00, 10])

    @patch("canbus.commands._next_corr_id", return_value=42)
    def test_delete_wl_slot(self, mock_corr_id):
        frame = delete_wl_slot(device_id=2, slot_index=5, lock_id=0)
        self.assertEqual(frame.arbitration_id, CAN_COMMAND | 2)
        self.assertEqual(list(frame.data), [0, DELETE_WL_SLOT, 42, 5])

    @patch("canbus.commands._next_corr_id", return_value=42)
    def test_wl_clear(self, mock_corr_id):
        frame = wl_clear(device_id=7, lock_id=1)
        self.assertEqual(frame.arbitration_id, CAN_COMMAND | 7)
        self.assertEqual(list(frame.data), [1, WL_CLEAR, 42])

    @patch("canbus.commands._next_corr_id", return_value=42)
    def test_set_wl_slot_policy(self, mock_corr_id):
        frame = set_wl_slot_policy(device_id=8, slot_index=3, policy=1, open_action=2, flags=0, lock_id=0)
        self.assertEqual(frame.arbitration_id, CAN_COMMAND | 8)
        self.assertEqual(list(frame.data), [0, SET_WL_SLOT_POLICY, 42, 3, 1, 2, 0])

    @patch("canbus.commands._next_corr_id", return_value=42)
    def test_set_lock_mode(self, mock_corr_id):
        frame = set_lock_mode(device_id=9, lock_mode=1, auto_close_timeout_s=5, behavior_flags=0, door_warning_delay_s=3, door_release_delay_s=6, lock_id=0)
        self.assertEqual(frame.arbitration_id, CAN_COMMAND | 9)
        self.assertEqual(list(frame.data), [0, SET_LOCK_MODE, 42, 1, 5, 0, 3, 6])

    @patch("canbus.commands._next_corr_id", return_value=42)
    def test_request_lock_mode(self, mock_corr_id):
        frame = request_lock_mode(device_id=11, lock_id=0)
        self.assertEqual(frame.arbitration_id, CAN_COMMAND | 11)
        self.assertEqual(list(frame.data), [0, REQUEST_LOCK_MODE, 42])

    @patch("canbus.commands._next_corr_id", return_value=42)
    def test_request_occupancy_state(self, mock_corr_id):
        frame = request_occupancy_state(device_id=12, lock_id=0)
        self.assertEqual(frame.arbitration_id, CAN_COMMAND | 12)
        self.assertEqual(list(frame.data), [0, REQUEST_OCCUPANCY_STATE, 42])

    @patch("canbus.commands._next_corr_id", return_value=42)
    def test_auth_resp(self, mock_corr_id):
        frame = auth_resp(device_id=13, result=1, action=2, lock_id=0)
        self.assertEqual(frame.arbitration_id, CAN_COMMAND | 13)
        self.assertEqual(list(frame.data), [0, AUTH_RESP, 42, 1, 2])
