"""
Targeted regression tests for partner-reported findings (Tobias's review):
1. CAN-ID Collision & Demultiplexing (0x0B vs 0x1B vs 0x7E).
2. Stale / Delayed ACK handling & Clean Transfer Restart.
3. Single CMD_OTA_START Handshake (RAM Mailbox).
4. P4 Health Report Capability Bits (Bit 7: OTA capable, Bit 6: Watchdog active).
"""

import asyncio
import os
import struct
import tempfile
import unittest
from unittest.mock import MagicMock

from canbus.constants import (
    ACK_OTA_ACTIVATE,
    ACK_OTA_DATA,
    ACK_OTA_START,
    ACK_OTA_VERIFY,
    CAN_HEALTH,
    CMD_OTA_ACTIVATE,
    CMD_OTA_DATA,
    CMD_OTA_START,
    CMD_OTA_VERIFY,
    FITNET_CAN_ID_OTA_ACK_BASE,
    FITNET_CAN_ID_OTA_CMD_BASE,
    HEALTH_DEVICE_INFO,
)
from canbus.parser import CANParser, HealthDeviceInfo
from canbus.protocol import CANFrame
from canbus.service import CANManager
from config.globals import can_service
from services.ota_service import ota_service


class TestOtaRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop = asyncio.get_running_loop()
        can_service.set_loop(self.loop)
        can_service.simulation = True

        # Temporary binary file: 32 bytes = 4 chunks of 8 bytes each
        self.temp_bin = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
        self.temp_bin.write(bytes(range(32)))
        self.temp_bin.close()

    async def asyncTearDown(self):
        if os.path.exists(self.temp_bin.name):
            os.remove(self.temp_bin.name)

    # -------------------------------------------------------------------------
    # 1. CAN-ID Collision Test (0x0B vs 0x1B vs 0x7E) & Demultiplexing
    # -------------------------------------------------------------------------
    async def test_can_id_collision_isolation_and_demux(self):
        """
        Verify that device IDs differing only in bit 4 (e.g., 0x0B vs 0x1B) and boundary 0x7E
        generate distinct CAN IDs and have isolated queues.
        Also verify that CANManager demultiplexes Commands (0x01..0x04) vs ACKs (0x81..0x84)
        on the unified 0x780 | devId ID without routing Commands into the ACK queue.
        """
        dev_0b = 0x0B
        dev_1b = 0x1B
        dev_7e = 0x7E

        # Verify unified 11-bit CAN arbitration IDs
        arb_0b = FITNET_CAN_ID_OTA_CMD_BASE | dev_0b  # 0x780 | 0x0B = 0x78B
        arb_1b = FITNET_CAN_ID_OTA_CMD_BASE | dev_1b  # 0x780 | 0x1B = 0x79B
        arb_7e = FITNET_CAN_ID_OTA_CMD_BASE | dev_7e  # 0x780 | 0x7E = 0x7FE

        self.assertEqual(arb_0b, 0x78B)
        self.assertEqual(arb_1b, 0x79B)
        self.assertEqual(arb_7e, 0x7FE)
        self.assertNotEqual(arb_0b, arb_1b)
        self.assertNotEqual(arb_1b, arb_7e)

        queue_0b = can_service.get_ota_queue(dev_0b)
        queue_1b = can_service.get_ota_queue(dev_1b)
        queue_7e = can_service.get_ota_queue(dev_7e)

        # Clear any preexisting frames
        for q in (queue_0b, queue_1b, queue_7e):
            while not q.empty():
                q.get_nowait()

        # 1. Dispatch an ACK frame for 0x0B
        ack_0b = CANFrame(arbitration_id=arb_0b, data=bytes([ACK_OTA_START, 0x00]))
        can_service._dispatch_frame(ack_0b)

        rec_0b = await asyncio.wait_for(queue_0b.get(), timeout=1.0)
        self.assertEqual(rec_0b.arbitration_id, 0x78B)
        self.assertTrue(queue_1b.empty())
        self.assertTrue(queue_7e.empty())

        # 2. Dispatch an ACK frame for 0x1B
        ack_1b = CANFrame(arbitration_id=arb_1b, data=bytes([ACK_OTA_DATA, 0x01, 0x00]))
        can_service._dispatch_frame(ack_1b)

        rec_1b = await asyncio.wait_for(queue_1b.get(), timeout=1.0)
        self.assertEqual(rec_1b.arbitration_id, 0x79B)
        self.assertTrue(queue_0b.empty())
        self.assertTrue(queue_7e.empty())

        # 3. Dispatch an ACK frame for 0x7E
        ack_7e = CANFrame(arbitration_id=arb_7e, data=bytes([ACK_OTA_VERIFY, 0x00]))
        can_service._dispatch_frame(ack_7e)

        rec_7e = await asyncio.wait_for(queue_7e.get(), timeout=1.0)
        self.assertEqual(rec_7e.arbitration_id, 0x7FE)
        self.assertTrue(queue_0b.empty())
        self.assertTrue(queue_1b.empty())

        # 4. Demultiplexing check: Transmit Command frames (0x01..0x04) on 0x780 | devId
        cmd_start = CANFrame(arbitration_id=arb_0b, data=bytes([CMD_OTA_START, 0x10, 0x00, 0x00, 0x12, 0x34, 0x56, 0x78]))
        cmd_data = CANFrame(arbitration_id=arb_1b, data=bytes([CMD_OTA_DATA, 0x00, 0x00, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE]))
        cmd_verify = CANFrame(arbitration_id=arb_7e, data=bytes([CMD_OTA_VERIFY]))
        cmd_activate = CANFrame(arbitration_id=arb_0b, data=bytes([CMD_OTA_ACTIVATE]))

        can_service._dispatch_frame(cmd_start)
        can_service._dispatch_frame(cmd_data)
        can_service._dispatch_frame(cmd_verify)
        can_service._dispatch_frame(cmd_activate)

        # Assert that NONE of the command frames were placed into OTA queues
        self.assertTrue(queue_0b.empty(), "Command frames must not enter OTA ACK queue")
        self.assertTrue(queue_1b.empty(), "Command frames must not enter OTA ACK queue")
        self.assertTrue(queue_7e.empty(), "Command frames must not enter OTA ACK queue")

    # -------------------------------------------------------------------------
    # 2. Stale / Delayed ACK & Clean Transfer Restart Test
    # -------------------------------------------------------------------------
    async def test_stale_delayed_ack_ignored(self):
        """
        Verify that when a stale/duplicate ACK arrives mid-transfer with an older
        expected chunk (exp_chunk <= chunk_idx), ota_service ignores the stale frame
        and continues waiting for the correct chunk_idx + 1 ACK.
        """
        dev_id = 11
        ack_id = FITNET_CAN_ID_OTA_ACK_BASE | dev_id
        original_send = can_service.send

        def mock_send(frame: CANFrame):
            if frame.data:
                cmd = frame.data[0]
                queue = can_service.get_ota_queue(dev_id)
                if cmd == CMD_OTA_START:
                    queue.put_nowait(CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_START, 0x00])))
                elif cmd == CMD_OTA_DATA:
                    chunk_idx = frame.data[1] | (frame.data[2] << 8)
                    if chunk_idx == 1:
                        # Inject a STALE duplicate ACK for chunk 0 (exp_chunk = 1 <= chunk_idx)
                        queue.put_nowait(CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_DATA, 0x01, 0x00])))
                        # Followed immediately by the valid ACK for chunk 1 (exp_chunk = 2)
                        queue.put_nowait(CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_DATA, 0x02, 0x00])))
                    else:
                        exp = chunk_idx + 1
                        queue.put_nowait(CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_DATA, exp & 0xFF, (exp >> 8) & 0xFF])))
                elif cmd == CMD_OTA_VERIFY:
                    queue.put_nowait(CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_VERIFY, 0x00])))
                elif cmd == CMD_OTA_ACTIVATE:
                    queue.put_nowait(CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_ACTIVATE, 0x00])))
            return original_send(frame)

        can_service.send = mock_send
        try:
            success = await asyncio.wait_for(
                ota_service.flash_device(dev_id, self.temp_bin.name),
                timeout=3.0,
            )
            self.assertTrue(success)
            self.assertEqual(ota_service.get_status(dev_id)["state"], "COMPLETE")
        finally:
            can_service.send = original_send

    async def test_aborted_transfer_clean_restart_from_chunk_zero(self):
        """
        Verify that if an OTA transfer is aborted mid-stream, a subsequent
        OTA start cleanly resets all queues and restarts transmission from chunk 0.
        """
        dev_id = 12
        ack_id = FITNET_CAN_ID_OTA_ACK_BASE | dev_id
        transmitted_frames = []
        original_send = can_service.send

        # Step 1: Simulate an aborted transfer
        def mock_aborting_send(frame: CANFrame):
            transmitted_frames.append(frame)
            if frame.data and frame.data[0] == CMD_OTA_START:
                queue = can_service.get_ota_queue(dev_id)
                queue.put_nowait(CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_START, 0x00])))
            # Do not answer CMD_OTA_DATA to simulate stall/abort
            return original_send(frame)

        can_service.send = mock_aborting_send
        try:
            # Start flash and allow it to enter FLASH stage, then abort
            flash_task = asyncio.create_task(ota_service.flash_device(dev_id, self.temp_bin.name))
            await asyncio.sleep(0.05)
            # Abort/reset session
            ota_service.reset_status(dev_id)
            try:
                await asyncio.wait_for(flash_task, timeout=0.1)
            except (asyncio.CancelledError, asyncio.TimeoutError, RuntimeError):
                pass
        finally:
            can_service.send = original_send

        self.assertEqual(ota_service.get_status(dev_id)["state"], "IDLE")

        # Step 2: Restart transfer from scratch with responsive device
        transmitted_frames.clear()

        def mock_responsive_send(frame: CANFrame):
            transmitted_frames.append(frame)
            if frame.data:
                cmd = frame.data[0]
                queue = can_service.get_ota_queue(dev_id)
                if cmd == CMD_OTA_START:
                    queue.put_nowait(CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_START, 0x00])))
                elif cmd == CMD_OTA_DATA:
                    chunk_idx = frame.data[1] | (frame.data[2] << 8)
                    exp = chunk_idx + 1
                    queue.put_nowait(CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_DATA, exp & 0xFF, (exp >> 8) & 0xFF])))
                elif cmd == CMD_OTA_VERIFY:
                    queue.put_nowait(CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_VERIFY, 0x00])))
                elif cmd == CMD_OTA_ACTIVATE:
                    queue.put_nowait(CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_ACTIVATE, 0x00])))
            return original_send(frame)

        can_service.send = mock_responsive_send
        try:
            success = await asyncio.wait_for(
                ota_service.flash_device(dev_id, self.temp_bin.name),
                timeout=3.0,
            )
            self.assertTrue(success)

            # Verify restarted session transmitted CMD_OTA_START first, then chunk 0
            data_frames = [f for f in transmitted_frames if f.data[0] == CMD_OTA_DATA]
            self.assertGreater(len(data_frames), 0)
            first_chunk_idx = data_frames[0].data[1] | (data_frames[0].data[2] << 8)
            self.assertEqual(first_chunk_idx, 0, "Restarted transfer must begin at chunk 0")
        finally:
            can_service.send = original_send

    # -------------------------------------------------------------------------
    # 3. Single CMD_OTA_START Handshake Test
    # -------------------------------------------------------------------------
    async def test_single_cmd_ota_start_handshake(self):
        """
        Verify that ota_service transmits CMD_OTA_START (0x01) exactly ONCE
        when initiating an update, waits for ACK_OTA_START (0x81), and immediately
        proceeds to transmit chunk 0.
        """
        dev_id = 27
        ack_id = FITNET_CAN_ID_OTA_ACK_BASE | dev_id
        sent_commands = []
        original_send = can_service.send

        def mock_send(frame: CANFrame):
            sent_commands.append(frame)
            if frame.data:
                cmd = frame.data[0]
                queue = can_service.get_ota_queue(dev_id)
                if cmd == CMD_OTA_START:
                    queue.put_nowait(CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_START, 0x00])))
                elif cmd == CMD_OTA_DATA:
                    chunk_idx = frame.data[1] | (frame.data[2] << 8)
                    exp = chunk_idx + 1
                    queue.put_nowait(CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_DATA, exp & 0xFF, (exp >> 8) & 0xFF])))
                elif cmd == CMD_OTA_VERIFY:
                    queue.put_nowait(CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_VERIFY, 0x00])))
                elif cmd == CMD_OTA_ACTIVATE:
                    queue.put_nowait(CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_ACTIVATE, 0x00])))
            return original_send(frame)

        can_service.send = mock_send
        try:
            success = await asyncio.wait_for(
                ota_service.flash_device(dev_id, self.temp_bin.name),
                timeout=3.0,
            )
            self.assertTrue(success)

            # Count occurrences of CMD_OTA_START (0x01)
            start_frames = [f for f in sent_commands if f.data[0] == CMD_OTA_START]
            self.assertEqual(len(start_frames), 1, "CMD_OTA_START must be transmitted exactly ONCE")

            # Verify immediate transition: next transmitted frame after START is CMD_OTA_DATA chunk 0
            start_idx = sent_commands.index(start_frames[0])
            self.assertLess(start_idx + 1, len(sent_commands))
            next_frame = sent_commands[start_idx + 1]
            self.assertEqual(next_frame.data[0], CMD_OTA_DATA)
            next_chunk_idx = next_frame.data[1] | (next_frame.data[2] << 8)
            self.assertEqual(next_chunk_idx, 0, "Immediately after ACK_OTA_START, chunk 0 must be transmitted")
        finally:
            can_service.send = original_send

    # -------------------------------------------------------------------------
    # 4. P4 Capability Bits 6 & 7 Test
    # -------------------------------------------------------------------------
    def test_p4_capability_bits_parsing(self):
        """
        Verify that CANParser correctly parses Bit 7 (ota_supported) and Bit 6 (watchdog_active)
        from P4 HEALTH_REPORT (HEALTH_DEVICE_INFO) frame.
        """
        parser = CANParser()
        dev_id = 0x05
        arb_id = CAN_HEALTH | dev_id  # 0x300 | 0x05 = 0x305

        # Case 1: Both Bit 7 (OTA) and Bit 6 (Watchdog) set -> 0xC0
        frame_c0 = CANFrame(
            arbitration_id=arb_id,
            data=bytes([HEALTH_DEVICE_INFO, 0x00, 0x00, 0x01, 0x02, 0x00, 0x00, 0xC0]),
        )
        parsed_c0 = parser.parse(frame_c0)
        self.assertIsInstance(parsed_c0, HealthDeviceInfo)
        self.assertTrue(parsed_c0.ota_supported, "Bit 7 set must result in ota_supported == True")
        self.assertTrue(parsed_c0.watchdog_active, "Bit 6 set must result in watchdog_active == True")

        # Case 2: Only Bit 7 (OTA) set -> 0x80
        frame_80 = CANFrame(
            arbitration_id=arb_id,
            data=bytes([HEALTH_DEVICE_INFO, 0x00, 0x00, 0x01, 0x02, 0x00, 0x00, 0x80]),
        )
        parsed_80 = parser.parse(frame_80)
        self.assertTrue(parsed_80.ota_supported)
        self.assertFalse(parsed_80.watchdog_active)

        # Case 3: Only Bit 6 (Watchdog) set -> 0x40
        frame_40 = CANFrame(
            arbitration_id=arb_id,
            data=bytes([HEALTH_DEVICE_INFO, 0x00, 0x00, 0x01, 0x02, 0x00, 0x00, 0x40]),
        )
        parsed_40 = parser.parse(frame_40)
        self.assertFalse(parsed_40.ota_supported)
        self.assertTrue(parsed_40.watchdog_active)

        # Case 4: Neither set -> 0x00
        frame_00 = CANFrame(
            arbitration_id=arb_id,
            data=bytes([HEALTH_DEVICE_INFO, 0x00, 0x00, 0x01, 0x02, 0x00, 0x00, 0x00]),
        )
        parsed_00 = parser.parse(frame_00)
        self.assertFalse(parsed_00.ota_supported)
        self.assertFalse(parsed_00.watchdog_active)
