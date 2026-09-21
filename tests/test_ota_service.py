"""
Unit tests for OTAService and OTA API endpoints.
"""

import asyncio
import os
import struct
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import app
from canbus.constants import (
    ACK_OTA_ACTIVATE,
    ACK_OTA_DATA,
    ACK_OTA_START,
    ACK_OTA_VERIFY,
    FITNET_CAN_ID_OTA_ACK_BASE,
)
from canbus.protocol import CANFrame
from config.globals import can_service
from services.ota_service import ota_service


class TestOTAService(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop = asyncio.get_running_loop()
        can_service.set_loop(self.loop)
        can_service.simulation = True

        # Create temporary dummy binary file (32 bytes)
        self.temp_bin = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
        self.dummy_payload = bytes(range(32))
        self.temp_bin.write(self.dummy_payload)
        self.temp_bin.close()

    async def asyncTearDown(self):
        if os.path.exists(self.temp_bin.name):
            os.remove(self.temp_bin.name)

    async def test_flash_device_success(self):
        """Verify full OTA sequence: START -> DATA chunks -> VERIFY -> ACTIVATE."""
        dev_id = 1
        ack_id = FITNET_CAN_ID_OTA_ACK_BASE | dev_id

        async def responder():
            """Simulates device bootloader responses."""
            queue = can_service.get_ota_queue(dev_id)
            expected_chunk = 0

            while True:
                status = ota_service.get_status(dev_id)
                if status["state"] == "COMPLETE":
                    break
                if status["state"] == "ERROR":
                    break

                if status["state"] == "ERASE":
                    # Respond to START
                    frame = CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_START, 0x00]))
                    await queue.put(frame)
                    await asyncio.sleep(0.05)

                elif status["state"] == "FLASH":
                    expected_chunk += 1
                    exp_bytes = struct.pack("<H", expected_chunk)
                    frame = CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_DATA]) + exp_bytes)
                    await queue.put(frame)
                    await asyncio.sleep(0.01)

                elif status["state"] == "VERIFY":
                    frame = CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_VERIFY, 0x00]))
                    await queue.put(frame)
                    await asyncio.sleep(0.05)

                elif status["state"] == "ACTIVATE":
                    frame = CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_ACTIVATE, 0x00]))
                    await queue.put(frame)
                    break

                await asyncio.sleep(0.02)

        resp_task = asyncio.create_task(responder())
        success = await ota_service.flash_device(dev_id, self.temp_bin.name)
        await resp_task

        self.assertTrue(success)
        final_status = ota_service.get_status(dev_id)
        self.assertEqual(final_status["state"], "COMPLETE")
        self.assertEqual(final_status["progress"], 100)


class TestOTAEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_upload_endpoint(self):
        """Test staging firmware binary via POST /api/ota/upload."""
        file_content = b"\x00\x01\x02\x03\x04\x05\x06\x07"
        response = self.client.post(
            "/api/ota/upload",
            files={"file": ("test_fw.bin", file_content, "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 200)
        json_data = response.json()
        self.assertEqual(json_data["status"], "staged")
        self.assertEqual(json_data["filename"], "test_fw.bin")
        self.assertTrue(os.path.exists(json_data["path"]))

    def test_start_and_status_endpoints(self):
        """Test starting an OTA task and polling its status."""
        # Create dummy staged file
        upload_resp = self.client.post(
            "/api/ota/upload",
            files={"file": ("slot_a.bin", b"\xFF" * 16, "application/octet-stream")},
        )
        self.assertEqual(upload_resp.status_code, 200)
        bin_path = upload_resp.json()["path"]

        # Start OTA
        with patch.object(ota_service, "start_ota_task", return_value="task_dev_9"):
            start_resp = self.client.post(
                "/api/devices/9/ota/start",
                json={"binary_path": bin_path},
            )
            self.assertEqual(start_resp.status_code, 200)
            self.assertEqual(start_resp.json()["status"], "started")
            self.assertEqual(start_resp.json()["task_id"], "task_dev_9")

        # Get status
        status_resp = self.client.get("/api/devices/9/ota/status")
        self.assertEqual(status_resp.status_code, 200)
        self.assertIn("state", status_resp.json())
        self.assertIn("progress", status_resp.json())

    def test_auto_slot_resolution_ping_pong(self):
        """Verify automatic ping-pong slot resolution (Slot A -> slot_b.bin, Slot B -> slot_a.bin)."""
        from config.globals import can_listener
        dev = can_listener.get_device(1)

        # Slot A active -> auto target slot_b.bin
        dev.active_slot = 0
        resolved_b = ota_service.resolve_target_binary(1, None)
        self.assertTrue(resolved_b.endswith("slot_b.bin"))

        # Slot B active -> auto target slot_a.bin
        dev.active_slot = 1
        resolved_a = ota_service.resolve_target_binary(1, None)
        self.assertTrue(resolved_a.endswith("slot_a.bin"))

    def test_backend_double_trigger_lockout_409(self):
        """Verify that starting an OTA task while one is already running returns 409 Conflict."""
        with patch.object(ota_service, "is_task_running", return_value=True):
            resp = self.client.post("/api/devices/1/ota/start", json={})
            self.assertEqual(resp.status_code, 409)
            self.assertIn("already active", resp.json()["detail"])

    def test_ota_status_includes_slot_metadata(self):
        """Verify GET /api/devices/{id}/ota/status returns active_slot and target_binary."""
        from config.globals import can_listener
        dev = can_listener.get_device(1)
        dev.active_slot = 0

        status_resp = self.client.get("/api/devices/1/ota/status")
        self.assertEqual(status_resp.status_code, 200)
        data = status_resp.json()
        self.assertEqual(data["active_slot"], 0)
        self.assertEqual(data["target_slot"], 1)
        self.assertEqual(data["target_binary"], "slot_b.bin")

    def test_ota_bundle_upload_and_release_resolution(self):
        """Verify unified .ota bundle upload, manifest validation, CRC checking, and dual-slot resolution."""
        import binascii
        import json
        import io
        import shutil
        import zipfile
        from config.globals import can_listener
        from routers.ota import LATEST_RELEASE_FILE, RELEASES_DIRECTORY

        # Backup current release file if present
        saved_release_content = None
        if os.path.exists(LATEST_RELEASE_FILE):
            with open(LATEST_RELEASE_FILE, "r", encoding="utf-8") as f:
                saved_release_content = f.read()

        try:
            data_a = b"SLOT_A_TEST_PAYLOAD" * 4
            data_b = b"SLOT_B_TEST_PAYLOAD" * 4
            crc_a = binascii.crc32(data_a) & 0xFFFFFFFF
            crc_b = binascii.crc32(data_b) & 0xFFFFFFFF

            manifest = {
                "version": "2.0.0",
                "target_mcu": "STM32C092",
                "timestamp": "2026-09-19T12:00:00Z",
                "binaries": {
                    "slot_a": {"filename": "slot_a.bin", "crc32": f"0x{crc_a:08X}", "size": len(data_a)},
                    "slot_b": {"filename": "slot_b.bin", "crc32": f"0x{crc_b:08X}", "size": len(data_b)},
                },
            }

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("manifest.json", json.dumps(manifest))
                zf.writestr("slot_a.bin", data_a)
                zf.writestr("slot_b.bin", data_b)

            # Upload valid bundle
            res = self.client.post(
                "/api/v1/ota/upload",
                files={"file": ("pslocks_firmware_v2.0.0.ota", buf.getvalue(), "application/octet-stream")},
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["status"], "staged")
            self.assertEqual(data["version"], "v2.0.0")

            # Check GET /api/v1/ota/release
            rel_res = self.client.get("/api/v1/ota/release")
            self.assertEqual(rel_res.status_code, 200)
            rel_data = rel_res.json()
            self.assertTrue(rel_data["has_release"])
            self.assertEqual(rel_data["version"], "v2.0.0")

            # Test Ping-Pong resolution using unpacked release
            dev = can_listener.get_device(1)
            dev.active_slot = 1  # Active on Slot B -> must resolve slot_a.bin in v2.0.0
            resolved_a = ota_service.resolve_target_binary(1, "auto")
            self.assertTrue(resolved_a.endswith("slot_a.bin"))
            self.assertIn("v2.0.0", resolved_a)

            dev.active_slot = 0  # Active on Slot A -> must resolve slot_b.bin in v2.0.0
            resolved_b = ota_service.resolve_target_binary(1, "auto")
            self.assertTrue(resolved_b.endswith("slot_b.bin"))
            self.assertIn("v2.0.0", resolved_b)

        finally:
            # Clean up test release folder and restore production release
            test_v2_dir = os.path.join(RELEASES_DIRECTORY, "v2.0.0")
            if os.path.exists(test_v2_dir):
                shutil.rmtree(test_v2_dir, ignore_errors=True)
            if saved_release_content is not None:
                with open(LATEST_RELEASE_FILE, "w", encoding="utf-8") as f:
                    f.write(saved_release_content)
            elif os.path.exists(LATEST_RELEASE_FILE):
                os.remove(LATEST_RELEASE_FILE)

    def test_ota_bundle_crc_mismatch_rejected(self):
        """Verify that bundles with corrupted binaries or invalid CRC are rejected."""
        import json
        import io
        import zipfile

        data_a = b"SLOT_A_CORRUPT"
        data_b = b"SLOT_B_CORRUPT"

        manifest = {
            "version": "9.9.9",
            "target_mcu": "STM32C092",
            "binaries": {
                "slot_a": {"filename": "slot_a.bin", "crc32": "0x12345678", "size": len(data_a)},
                "slot_b": {"filename": "slot_b.bin", "crc32": "0x87654321", "size": len(data_b)},
            },
        }

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("slot_a.bin", data_a)
            zf.writestr("slot_b.bin", data_b)

        res = self.client.post(
            "/api/v1/ota/upload",
            files={"file": ("corrupt.ota", buf.getvalue(), "application/octet-stream")},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("CRC", res.json()["detail"])

    def test_batch_ota_status_and_reset_endpoints(self):
        """Verify GET /api/v1/ota/batch/status and POST /api/v1/devices/{id}/ota/reset."""
        # Test reset endpoint
        ota_service.statuses[1] = {"device_id": 1, "progress": 100, "state": "COMPLETE", "error": None}
        reset_resp = self.client.post("/api/v1/devices/1/ota/reset")
        self.assertEqual(reset_resp.status_code, 200)
        self.assertEqual(reset_resp.json()["status"], "reset")
        status = ota_service.get_status(1)
        self.assertEqual(status["state"], "IDLE")
        self.assertEqual(status["progress"], 0)

        # Test batch status endpoint
        batch_status_resp = self.client.get("/api/v1/ota/batch/status")
        self.assertEqual(batch_status_resp.status_code, 200)
        data = batch_status_resp.json()
        self.assertIn("running", data)
        self.assertIn("total_devices", data)
        self.assertIn("completed_devices", data)
        self.assertIn("failed_devices", data)

