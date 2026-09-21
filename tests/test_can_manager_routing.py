"""
Unit tests for CANManager routing and CANListener integration.
"""

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from canbus.constants import (
    FITNET_CAN_ID_OTA_ACK_BASE,
    FITNET_CAN_ID_OTA_BASE_MASK,
    FITNET_CAN_ID_OTA_DEVICE_MASK,
    CAN_STATUS,
)
from canbus.protocol import CANFrame
from canbus.service import CANManager
from canbus.listener import CANListener


class TestCANManagerRouting(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.can_manager = CANManager(simulation=True)
        self.loop = asyncio.get_running_loop()
        self.can_manager.set_loop(self.loop)

    async def asyncTearDown(self):
        self.can_manager.stop()

    async def test_ota_ack_routing_bitwise_or(self):
        """Verify OTA ACK frames with bitwise OR are routed to the per-device queue."""
        dev_id = 5
        queue = self.can_manager.get_ota_queue(dev_id)

        ack_id = FITNET_CAN_ID_OTA_ACK_BASE | dev_id
        # Verify mask-based routing requirement: (arb_id & FITNET_CAN_ID_OTA_BASE_MASK) == 0x790
        self.assertEqual(ack_id & FITNET_CAN_ID_OTA_BASE_MASK, 0x790)

        ota_frame = CANFrame(arbitration_id=ack_id, data=bytes([0x81, 0x00]))
        self.can_manager._dispatch_frame(ota_frame)

        received_frame = await asyncio.wait_for(queue.get(), timeout=1.0)
        self.assertEqual(received_frame.arbitration_id, ack_id)
        self.assertEqual(received_frame.data, bytes([0x81, 0x00]))

    async def test_ota_ack_routing_7bit_boundary(self):
        """Verify 7-bit device IDs (up to 0x7F / 0x80F) are routed properly."""
        dev_id = 0x7F
        queue = self.can_manager.get_ota_queue(dev_id)

        ack_id = FITNET_CAN_ID_OTA_ACK_BASE + dev_id  # 0x790 + 0x7F = 0x80F
        self.assertEqual(ack_id, 0x80F)

        ota_frame = CANFrame(arbitration_id=ack_id, data=bytes([0x82, 0x01, 0x00]))
        self.can_manager._dispatch_frame(ota_frame)

        received_frame = await asyncio.wait_for(queue.get(), timeout=1.0)
        self.assertEqual(received_frame.arbitration_id, ack_id)
        self.assertEqual(received_frame.data, bytes([0x82, 0x01, 0x00]))

    async def test_standard_frame_routing_to_callback(self):
        """Verify standard telemetry frames are passed to registered callbacks."""
        received_frames = []

        def callback(frame: CANFrame):
            received_frames.append(frame)

        self.can_manager.register_callback(callback)

        std_frame = CANFrame(arbitration_id=CAN_STATUS | 0x01, data=bytes([0x00, 0x01, 0x00]))
        self.can_manager._dispatch_frame(std_frame)

        self.assertEqual(len(received_frames), 1)
        self.assertEqual(received_frames[0].arbitration_id, CAN_STATUS | 0x01)

    async def test_can_listener_integration(self):
        """Verify CANListener receives standard frames and updates device state."""
        listener = CANListener(self.can_manager)
        listener.running = True
        self.can_manager.register_callback(listener.handle_frame)

        from canbus.constants import STATUS_BASIC
        status_frame = CANFrame(
            arbitration_id=CAN_STATUS | 0x02,
            data=bytes([STATUS_BASIC, 0, 1, 0, 0, 0, 0, 0])
        )
        self.can_manager._dispatch_frame(status_frame)

        dev = listener.get_device(2)
        self.assertEqual(dev.lock_state, 1)
        self.assertTrue(dev.is_locked)
