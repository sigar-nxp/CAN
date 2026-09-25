"""
Unit tests for CAN TX and RX live logging and stream event payload verification.
"""

import time
import unittest
from fastapi.testclient import TestClient

from app import app
from canbus.commands import buzz_play, led_set
from canbus.constants import BUZZ_PLAY, LED_SET, STATUS_COMMAND_ACK
from canbus.protocol import CANFrame
from canbus.service import CANManager
from canbus.listener import CANListener
from config.globals import can_listener, can_service


class TestTXRXLogging(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Restore can_listener methods if previous test suites replaced them with MagicMock
        if hasattr(can_listener, "start") and hasattr(can_listener.start, "assert_called"):
            can_listener.start = CANListener.start.__get__(can_listener, CANListener)
        if hasattr(can_listener, "stop") and hasattr(can_listener.stop, "assert_called"):
            can_listener.stop = CANListener.stop.__get__(can_listener, CANListener)
        if hasattr(can_service, "simulation"):
            can_service.simulation = True

    def setUp(self):
        self.can_manager = CANManager(simulation=True)
        self.client = TestClient(app)
        if not can_listener.running:
            can_listener.start()

    def tearDown(self):
        self.can_manager.stop()

    def _wait_for_logs(self, target_listener, min_count, timeout=2.0):
        """Helper to wait for asynchronous log worker to persist entries."""
        start = time.time()
        while time.time() - start < timeout:
            if len(target_listener.recent_logs) >= min_count:
                return True
            time.sleep(0.05)
        return False

    def test_tx_logging_on_send(self):
        """Verify that transmitting commands via CANManager emits an explicit TX log entry."""
        logged_items = []
        self.can_manager.set_log_handler(lambda **kwargs: logged_items.append(kwargs))

        # 1. Transmit BUZZ_PLAY
        frame_buzz = buzz_play(device_id=1, sound=1, repeat=2, lock_id=0)
        self.can_manager.send(frame_buzz)

        self.assertEqual(len(logged_items), 1)
        buzz_log = logged_items[0]
        self.assertEqual(buzz_log["direction"], "TX")
        self.assertEqual(buzz_log["can_id"], "0x101")
        self.assertEqual(buzz_log["event_type"], "BUZZ_PLAY")
        self.assertIn("00 50", buzz_log["payload"])
        self.assertIsNotNone(buzz_log["corr_id"])
        self.assertIn("Play buzzer", buzz_log["details"])

        # 2. Transmit LED_SET
        frame_led = led_set(device_id=1, mode=1, period10ms=100, duty=50, ttl=5, lock_id=0)
        self.can_manager.send(frame_led)

        self.assertEqual(len(logged_items), 2)
        led_log = logged_items[1]
        self.assertEqual(led_log["direction"], "TX")
        self.assertEqual(led_log["can_id"], "0x101")
        self.assertEqual(led_log["event_type"], "LED_SET")
        self.assertIn("00 40", led_log["payload"])
        self.assertIsNotNone(led_log["corr_id"])
        self.assertIn("Set LED", led_log["details"])

    def test_rx_command_ack_logging(self):
        """Verify that incoming device ACK frames emit RX log entries with matching context."""
        initial_count = len(can_listener.recent_logs)
        dev_id = 1
        dev = can_listener.get_device(dev_id)

        # Simulate incoming CommandAck for BUZZ_PLAY (cmd 0x50, corrId 42)
        ack_data = bytes([STATUS_COMMAND_ACK, 0, BUZZ_PLAY, 42])
        ack_frame = CANFrame(arbitration_id=0x200 | dev_id, data=ack_data)
        can_listener.handle_frame(ack_frame)

        # Wait for async worker to process log
        self._wait_for_logs(can_listener, initial_count + 1)

        recent = can_listener.recent_logs
        self.assertGreater(len(recent), initial_count)
        latest = recent[-1]
        self.assertEqual(latest["direction"], "RX")
        self.assertEqual(latest["can_id"], "0x201")
        self.assertEqual(latest["event_type"], "COMMAND_ACK")
        self.assertEqual(latest["corr_id"], 42)
        self.assertEqual(latest["corrId"], 42)
        self.assertIn("BUZZ_PLAY", latest["details"])

    def test_api_buzzer_and_led_tx_rx_flow(self):
        """Trigger Buzzer and LED via API and verify TX entry followed by simulated ACK."""
        initial_log_count = len(can_listener.recent_logs)

        # 1. Trigger Buzzer command via API
        res = self.client.post("/api/v1/devices/1/buzzer", json={"sound": 1, "repeat": 1})
        self.assertEqual(res.status_code, 200)

        # Wait for TX log entry
        self._wait_for_logs(can_listener, initial_log_count + 1)

        tx_logs = [l for l in can_listener.recent_logs[initial_log_count:] if l.get("direction") == "TX"]
        self.assertGreaterEqual(len(tx_logs), 1)
        latest_tx = tx_logs[-1]
        self.assertEqual(latest_tx["direction"], "TX")
        self.assertEqual(latest_tx["can_id"], "0x101")
        self.assertEqual(latest_tx["event_type"], "BUZZ_PLAY")
        corr_id = latest_tx.get("corrId") or latest_tx.get("corr_id")

        # 2. Simulate device ACK response
        mid_count = len(can_listener.recent_logs)
        ack_data = bytes([STATUS_COMMAND_ACK, 0, BUZZ_PLAY, corr_id or 1])
        ack_frame = CANFrame(arbitration_id=0x201, data=ack_data)
        can_listener.handle_frame(ack_frame)

        # Wait for RX log entry
        self._wait_for_logs(can_listener, mid_count + 1)

        rx_logs = [l for l in can_listener.recent_logs[mid_count:] if l.get("direction") == "RX"]
        self.assertGreaterEqual(len(rx_logs), 1)
        latest_rx = rx_logs[-1]
        self.assertEqual(latest_rx["direction"], "RX")
        self.assertEqual(latest_rx["can_id"], "0x201")
        self.assertEqual(latest_rx["event_type"], "COMMAND_ACK")
        self.assertIn("BUZZ_PLAY", latest_rx["details"])

    def test_logs_endpoint_includes_direction_and_corr_id(self):
        """Verify GET /api/v1/logs returns direction, can_id, payload, and corrId."""
        res = self.client.get("/api/v1/logs?limit=5")
        self.assertEqual(res.status_code, 200)
        logs = res.json()
        self.assertIsInstance(logs, list)
        if logs:
            first = logs[0]
            self.assertIn("direction", first)
            self.assertIn(first["direction"], ("TX", "RX"))
            self.assertIn("can_id", first)
            self.assertIn("payload", first)
            self.assertIn("corrId", first)

