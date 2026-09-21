"""
PS Locks OIP Asynchronous OTA Service.

Implements the dual-bank CAN bootloader host flasher logic asynchronously,
coordinating firmware staging, chunk transmission with physical layer pacing,
integrity verification, and slot activation without interrupting regular
gateway telemetry.
"""

import asyncio
import binascii
import errno
import os
import struct
from typing import Any, Callable, Dict, List, Optional

import can

from canbus.constants import (
    ACK_OTA_ACTIVATE,
    ACK_OTA_DATA,
    ACK_OTA_START,
    ACK_OTA_VERIFY,
    CMD_OTA_ACTIVATE,
    CMD_OTA_DATA,
    CMD_OTA_START,
    CMD_OTA_VERIFY,
    FITNET_CAN_ID_OTA_CMD_BASE,
    OTA_DATA_CHUNK_MAX_SIZE,
)
from canbus.protocol import CANFrame
from config.globals import can_service, can_listener


class OTAService:
    """
    Asynchronous OTA update service for STM32 dual-bank bootloaders.
    """

    def __init__(self, pacing_delay: float = 0.006):
        self.pacing_delay = pacing_delay
        self.statuses: Dict[int, Dict[str, Any]] = {}
        self.active_tasks: Dict[int, asyncio.Task] = {}
        self.batch_task: Optional[asyncio.Task] = None
        self.batch_running: bool = False
        self.batch_device_ids: List[int] = []
        self.batch_current_index: int = 0
        self.batch_current_device_id: Optional[int] = None
        self.batch_completed_devices: List[int] = []
        self.batch_failed_devices: List[int] = []

    def get_status(self, device_id: int) -> Dict[str, Any]:
        """Returns the current OTA update status for a target device."""
        return self.statuses.get(
            device_id,
            {"device_id": device_id, "progress": 0, "state": "IDLE", "error": None},
        )

    def reset_status(self, device_id: int) -> None:
        """Resets the OTA status for a target device to IDLE."""
        self.statuses[device_id] = {
            "device_id": device_id,
            "progress": 0,
            "state": "IDLE",
            "error": None,
        }

    def get_batch_status(self) -> Dict[str, Any]:
        """Returns the current state and progress of the batch OTA update sequence."""
        running = self.is_batch_running()
        return {
            "running": running,
            "current_device_id": self.batch_current_device_id,
            "current_index": self.batch_current_index,
            "total_devices": len(self.batch_device_ids),
            "completed_devices": list(self.batch_completed_devices),
            "failed_devices": list(self.batch_failed_devices),
            "device_ids": list(self.batch_device_ids),
            "state": (
                "RUNNING"
                if running
                else ("COMPLETE" if self.batch_completed_devices and not self.batch_failed_devices else ("ERROR" if self.batch_failed_devices else "IDLE"))
            ),
        }

    def is_task_running(self, device_id: int) -> bool:
        """Checks if an OTA update task is actively executing for a device."""
        if device_id in self.active_tasks:
            task = self.active_tasks[device_id]
            if not task.done():
                return True
        st = self.get_status(device_id)
        return st.get("state") in ("STARTING", "ERASE", "FLASH", "VERIFY", "ACTIVATE", "QUEUED")

    def is_batch_running(self) -> bool:
        """Checks if a batch OTA sequence is actively executing."""
        if self.batch_running:
            return True
        if self.batch_task is not None and not self.batch_task.done():
            return True
        return False

    def resolve_target_binary(self, device_id: int, requested_path: Optional[str] = None) -> str:
        """
        Automatically resolves the secondary flash target binary (Ping-Pong dual-bank).
        If active on Slot A (0), targets slot_b.bin.
        If active on Slot B (1), targets slot_a.bin.
        Retrieves matching binary from latest unpacked release if available,
        with backward-compatible fallback to uploads/ and build/.
        """
        if requested_path and requested_path.strip() and requested_path.strip().lower() != "auto":
            return self._find_binary_file(requested_path.strip())

        dev = can_listener.get_device(device_id)
        active_slot = getattr(dev, "active_slot", 0) or 0
        target_name = "slot_b.bin" if active_slot == 0 else "slot_a.bin"
        target_slot_key = "slot_b" if active_slot == 0 else "slot_a"

        # Check latest unpacked release package
        latest_rel_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "uploads",
            "releases",
            "latest_release.json",
        )
        if os.path.isfile(latest_rel_path):
            try:
                import json
                with open(latest_rel_path, "r", encoding="utf-8") as f:
                    rel_data = json.load(f)
                bin_info = rel_data.get("binaries", {}).get(target_slot_key, {})
                candidate_path = bin_info.get("path")
                if candidate_path and os.path.isfile(candidate_path):
                    return os.path.abspath(candidate_path)

                rel_dir = rel_data.get("release_dir")
                if rel_dir:
                    candidate_path = os.path.join(rel_dir, target_name)
                    if os.path.isfile(candidate_path):
                        return os.path.abspath(candidate_path)
            except Exception as ex:
                print(f"Warning: Failed reading latest_release.json: {ex}")

        return self._find_binary_file(target_name)

    def _find_binary_file(self, filename: str) -> str:
        """Locates binary file in releases, uploads, or firmware build directories."""
        if os.path.exists(filename):
            return os.path.abspath(filename)

        upload_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads")
        candidate = os.path.join(upload_dir, os.path.basename(filename))
        if os.path.exists(candidate):
            return candidate

        releases_dir = os.path.join(upload_dir, "releases")
        if os.path.isdir(releases_dir):
            for entry in sorted(os.listdir(releases_dir), reverse=True):
                subpath = os.path.join(releases_dir, entry, os.path.basename(filename))
                if os.path.isfile(subpath):
                    return subpath

        fw_build_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "PSLOCKS_Firmware",
            "build",
        )
        fw_candidate = os.path.join(fw_build_dir, os.path.basename(filename))
        if os.path.exists(fw_candidate):
            return fw_candidate

        raise FileNotFoundError(f"Firmware binary file not found: {filename}")

    def _update_status(
        self,
        device_id: int,
        progress: int,
        state: str,
        error: Optional[str] = None,
        callback: Optional[Callable[[int, str, Optional[str]], Any]] = None,
    ) -> None:
        """Internal status updater with optional progress callback trigger."""
        self.statuses[device_id] = {
            "device_id": device_id,
            "progress": progress,
            "state": state,
            "error": error,
        }
        if callback is not None:
            try:
                res = callback(progress, state, error)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception as ex:
                print(f"OTA status callback error: {ex}")

    async def _wait_for_ack(
        self,
        queue: asyncio.Queue,
        expected_ack_cmd: int,
        timeout: float = 1.0,
    ) -> Optional[CANFrame]:
        """Awaits an OTA acknowledgment frame matching the expected opcode."""
        start_time = asyncio.get_event_loop().time()
        while True:
            remaining = timeout - (asyncio.get_event_loop().time() - start_time)
            if remaining <= 0:
                return None
            try:
                frame: CANFrame = await asyncio.wait_for(queue.get(), timeout=remaining)
                if frame.data and frame.data[0] == expected_ack_cmd:
                    return frame
            except asyncio.TimeoutError:
                return None

    async def flash_device(
        self,
        device_id: int,
        binary_path: str,
        progress_callback: Optional[Callable[[int, str, Optional[str]], Any]] = None,
    ) -> bool:
        """
        Executes an asynchronous OTA firmware flash sequence for a single device.
        Phases: ERASE -> FLASH -> VERIFY -> COMPLETE.
        """
        try:
            if not os.path.exists(binary_path):
                err = f"Binary file not found: {binary_path}"
                self._update_status(device_id, 0, "ERROR", error=err, callback=progress_callback)
                return False

            try:
                with open(binary_path, "rb") as f:
                    fw_data = bytearray(f.read())
            except Exception as ex:
                err = f"Failed to read binary: {ex}"
                self._update_status(device_id, 0, "ERROR", error=err, callback=progress_callback)
                return False

            # Pad firmware payload to 8-byte boundary
            if len(fw_data) % 8 != 0:
                fw_data.extend(b"\xFF" * (8 - (len(fw_data) % 8)))

            size = len(fw_data)
            crc32 = binascii.crc32(fw_data) & 0xFFFFFFFF
            cmd_id = FITNET_CAN_ID_OTA_CMD_BASE | device_id
            queue = can_service.get_ota_queue(device_id)

            # Clear any stale frames in the device OTA queue
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break


            # 1. ERASE Phase: Transmit CMD_OTA_START and wait for flash erase completion
            self._update_status(device_id, 0, "ERASE", callback=progress_callback)
            start_payload = bytes(
                [CMD_OTA_START, size & 0xFF, (size >> 8) & 0xFF, (size >> 16) & 0xFF]
            ) + struct.pack("<I", crc32)
            start_msg = CANFrame(arbitration_id=cmd_id, data=start_payload)

            connected = False
            session_start = asyncio.get_event_loop().time()
            while (asyncio.get_event_loop().time() - session_start) < 15.0:
                try:
                    can_service.send(start_msg)
                except (can.CanError, OSError):
                    pass
                ack = await self._wait_for_ack(queue, ACK_OTA_START, timeout=3.0)
                if ack and len(ack.data) > 1 and ack.data[1] == 0:
                    connected = True
                    if len(ack.data) >= 3 and ack.data[2] in (0, 1):
                        bootloader_active_slot = ack.data[2]
                        dev = can_listener.get_device(device_id)
                        dev.active_slot = bootloader_active_slot
                        expected_binary = "slot_b.bin" if bootloader_active_slot == 0 else "slot_a.bin"
                        if not binary_path.endswith(expected_binary):
                            new_binary = self.resolve_target_binary(device_id, "auto")
                            if new_binary != binary_path and os.path.exists(new_binary):
                                with open(new_binary, "rb") as f:
                                    fw_data = bytearray(f.read())
                                if len(fw_data) % 8 != 0:
                                    fw_data.extend(b"\xFF" * (8 - (len(fw_data) % 8)))
                                size = len(fw_data)
                                crc32 = binascii.crc32(fw_data) & 0xFFFFFFFF
                                binary_path = new_binary
                    break

            if not connected:
                err = "Bootloader connection/erase timeout (15s exceeded)"
                self._update_status(device_id, 0, "ERROR", error=err, callback=progress_callback)
                return False

            # 2. FLASH Phase: Stream chunks with physical layer pacing
            self._update_status(device_id, 0, "FLASH", callback=progress_callback)
            chunk_idx = 0
            offset = 0

            while offset < size:
                chunk_data = fw_data[offset : offset + OTA_DATA_CHUNK_MAX_SIZE]
                payload = struct.pack("<B H", CMD_OTA_DATA, chunk_idx) + bytes(chunk_data)
                chunk_frame = CANFrame(arbitration_id=cmd_id, data=payload)

                chunk_ack = False
                for attempt in range(8):
                    try:
                        can_service.send(chunk_frame)
                    except (can.CanError, OSError):
                        await asyncio.sleep(0.01 * (attempt + 1))
                        continue

                    ack = await self._wait_for_ack(queue, ACK_OTA_DATA, timeout=0.15)
                    if ack and len(ack.data) >= 3:
                        exp_chunk = ack.data[1] | (ack.data[2] << 8)
                        if exp_chunk == chunk_idx + 1:
                            chunk_ack = True
                            break
                    await asyncio.sleep(self.pacing_delay * (1 + attempt * 0.5))

                if not chunk_ack:
                    err = f"Failed transmitting chunk index {chunk_idx}"
                    self._update_status(device_id, int(offset * 100 / size), "ERROR", error=err, callback=progress_callback)
                    return False

                offset += len(chunk_data)
                chunk_idx += 1

                if self.pacing_delay > 0:
                    await asyncio.sleep(self.pacing_delay)

                progress_pct = int(offset * 100 / size)
                if chunk_idx % 25 == 0 or offset >= size:
                    self._update_status(device_id, progress_pct, "FLASH", callback=progress_callback)

            # 3. VERIFY Phase: Verify target slot CRC32
            self._update_status(device_id, 100, "VERIFY", callback=progress_callback)
            verify_msg = CANFrame(arbitration_id=cmd_id, data=bytes([CMD_OTA_VERIFY]))
            for _ in range(5):
                try:
                    can_service.send(verify_msg)
                    break
                except (can.CanError, OSError):
                    await asyncio.sleep(0.02)

            ack_verify = await self._wait_for_ack(queue, ACK_OTA_VERIFY, timeout=3.0)
            if not ack_verify or len(ack_verify.data) < 2 or ack_verify.data[1] != 0:
                err = "CRC32 verification check failed on target device"
                self._update_status(device_id, 100, "ERROR", error=err, callback=progress_callback)
                return False

            # 4. ACTIVATE Phase: Switch active slot and trigger reset
            act_msg = CANFrame(arbitration_id=cmd_id, data=bytes([CMD_OTA_ACTIVATE]))
            for _ in range(5):
                try:
                    can_service.send(act_msg)
                    break
                except (can.CanError, OSError):
                    await asyncio.sleep(0.02)
            await self._wait_for_ack(queue, ACK_OTA_ACTIVATE, timeout=1.0)

            # Flip active slot in memory and database upon activation
            dev = can_listener.get_device(device_id)
            curr_slot = getattr(dev, "active_slot", 0) or 0
            new_slot = 1 if curr_slot == 0 else 0
            dev.active_slot = new_slot

            from database.database import SessionLocal
            from models.device import Device

            try:
                with SessionLocal() as db:
                    db_dev = db.query(Device).filter(Device.device_id == device_id).first()
                    if db_dev:
                        db_dev.active_slot = new_slot
                        db.commit()
            except Exception as ex:
                print(f"Failed to persist active_slot: {ex}")

            # Clear OTA queue so operational commands and confirmation buzzer can be transmitted
            can_service.clear_ota_queue(device_id)

            self._update_status(device_id, 100, "COMPLETE", callback=progress_callback)

            # Emit confirmation buzzer after brief activation wait
            try:
                await asyncio.sleep(1.2)
                from canbus.commands import buzz_play
                can_service.send(buzz_play(device_id, 1, 1))
            except Exception as ex:
                print(f"Confirmation buzzer error: {ex}")

            return True

        except Exception as ex:
            self._update_status(device_id, 0, "ERROR", error=str(ex), callback=progress_callback)
            return False
        finally:
            can_service.clear_ota_queue(device_id)
            if device_id in self.active_tasks:
                del self.active_tasks[device_id]

    async def flash_all_devices(
        self,
        device_ids: List[int],
        binary_path: str = "auto",
    ) -> Dict[int, bool]:
        """
        Sequentially executes firmware flashing across a queue of devices.
        Strictly processes one lock at a time to prevent bus congestion.
        Guarantees batch state teardown in finally block.
        """
        results: Dict[int, bool] = {}
        try:
            self.batch_running = True
            for dev_id in device_ids:
                self._update_status(dev_id, 0, "QUEUED")

            for idx, dev_id in enumerate(device_ids):
                self.batch_current_index = idx
                self.batch_current_device_id = dev_id
                try:
                    target_file = self.resolve_target_binary(dev_id, binary_path)
                    success = await self.flash_device(dev_id, target_file)
                except Exception as ex:
                    self._update_status(dev_id, 0, "ERROR", error=str(ex))
                    success = False

                results[dev_id] = success
                if success:
                    self.batch_completed_devices.append(dev_id)
                else:
                    self.batch_failed_devices.append(dev_id)
                await asyncio.sleep(1.0)
            return results
        finally:
            self.batch_running = False
            self.batch_current_device_id = None
            self.batch_task = None

    def start_ota_task(self, device_id: int, binary_path: str) -> str:
        """Spawns an asynchronous background task for a single device update."""
        task = asyncio.create_task(self.flash_device(device_id, binary_path))
        self.active_tasks[device_id] = task
        return f"ota_task_{device_id}"

    def start_batch_ota_task(self, device_ids: List[int], binary_path: str) -> str:
        """Spawns an asynchronous background task for sequential batch updates."""
        self.batch_running = True
        self.batch_device_ids = list(device_ids)
        self.batch_completed_devices = []
        self.batch_failed_devices = []
        self.batch_current_index = 0
        self.batch_current_device_id = device_ids[0] if device_ids else None
        for dev_id in device_ids:
            self._update_status(dev_id, 0, "QUEUED")

        self.batch_task = asyncio.create_task(self.flash_all_devices(device_ids, binary_path))
        return "ota_batch_task"


ota_service = OTAService()
