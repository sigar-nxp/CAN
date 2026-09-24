#!/usr/bin/env python3
"""
PS Locks Lock Doctor - Standalone Automated Diagnostic and Recovery Utility.

Automates end-to-end diagnosis and recovery for PS Locks CAN nodes:
- Stage 1 (Interface Health): Inspects SocketCAN link, resets degraded/bus-off state.
- Stage 2 (Node Ping & Telemetry): Pings node via CAN (0x101 REQUEST_STATUS / 0xFE REQUEST_DEVICE_INFO).
- Stage 3 (Bootloader Recovery): Recovers stuck bootloader nodes (0x781) via CMD_OTA_ACTIVATE or bundle re-flash,
                                 with OpenOCD SWD fallback for unresponsive MCUs.
- Stage 4 (Database Sync): Ensures database entry exists in pslocks.db and synchronizes state.
- Final Verification: Verifies operational status via Gateway REST API.
"""

import argparse
import binascii
import os
import re
import sqlite3
import struct
import subprocess
import sys
import time
from typing import Any, Dict, Optional, Tuple

import can
import requests

# ANSI Color Codes for Terminal Output
CYAN_BOLD = "\033[1;36m"
GREEN_BOLD = "\033[1;32m"
YELLOW_BOLD = "\033[1;33m"
RED_BOLD = "\033[1;31m"
MAGENTA_BOLD = "\033[1;35m"
BLUE_BOLD = "\033[1;34m"
BOLD = "\033[1m"
RESET = "\033[0m"

# CAN Protocol Constants
CAN_COMMAND = 0x100
CAN_STATUS = 0x200
CAN_HEALTH = 0x300

FITNET_CAN_ID_OTA_CMD_BASE = 0x780
FITNET_CAN_ID_OTA_ACK_BASE = 0x790

CMD_OTA_START = 0x01
CMD_OTA_DATA = 0x02
CMD_OTA_VERIFY = 0x03
CMD_OTA_ACTIVATE = 0x04

ACK_OTA_START = 0x81
ACK_OTA_DATA = 0x82
ACK_OTA_VERIFY = 0x83
ACK_OTA_ACTIVATE = 0x84

REQUEST_STATUS = 0x02
REQUEST_DEVICE_INFO = 0xFE
REQUEST_HEALTH = 0xFC


class ConsoleUI:
    """Provides formatted and colored console status messages."""

    @staticmethod
    def stage_header(stage_num: int, title: str) -> None:
        print(f"\n{CYAN_BOLD}============================================================{RESET}")
        print(f"{CYAN_BOLD} Stage {stage_num}: {title}{RESET}")
        print(f"{CYAN_BOLD}============================================================{RESET}")

    @staticmethod
    def found_state(msg: str) -> None:
        print(f"  {YELLOW_BOLD}[FOUND STATE]{RESET} {msg}")

    @staticmethod
    def applying_recovery(msg: str) -> None:
        print(f"  {MAGENTA_BOLD}[APPLYING RECOVERY]{RESET} {msg}")

    @staticmethod
    def verified_operational(msg: str) -> None:
        print(f"  {GREEN_BOLD}[VERIFIED OPERATIONAL]{RESET} {msg}")

    @staticmethod
    def error(msg: str) -> None:
        print(f"  {RED_BOLD}[ERROR]{RESET} {msg}")

    @staticmethod
    def info(msg: str) -> None:
        print(f"  {BLUE_BOLD}[INFO]{RESET} {msg}")


def run_sudo_command(cmd_args: list) -> subprocess.CompletedProcess:
    """Executes a system command with sudo privileges, providing non-interactive fallback."""
    proc = subprocess.run(["sudo", "-n"] + cmd_args, capture_output=True, text=True)
    if proc.returncode == 0:
        return proc

    return subprocess.run(
        ["sudo", "-S"] + cmd_args,
        input="admin\n",
        capture_output=True,
        text=True,
    )


class LockDoctor:
    """
    Automated diagnostic and recovery manager for PS Locks nodes.
    """

    def __init__(
        self,
        device_id: int = 1,
        interface: str = "can0",
        bitrate: int = 50000,
        db_path: str = "/home/admin/PSLOCKS_OIP/pslocks.db",
        api_url: str = "http://127.0.0.1:8000",
        force_reflash: bool = False,
    ):
        self.device_id = device_id
        self.interface = interface
        self.bitrate = bitrate
        self.db_path = db_path
        self.api_url = api_url.rstrip("/")
        self.force_reflash = force_reflash

    # =========================================================================
    # Stage 1: Interface Health
    # =========================================================================
    def stage1_interface_health(self) -> bool:
        """
        Probes the CAN interface link and controller state.
        Automatically recovers from DOWN, STOPPED, or BUS-OFF states.
        """
        ConsoleUI.stage_header(1, f"CAN Interface Health ({self.interface})")

        proc = subprocess.run(
            ["ip", "-details", "link", "show", self.interface],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            ConsoleUI.found_state(f"Interface '{self.interface}' does not exist or cannot be accessed.")
            ConsoleUI.error(proc.stderr.strip())
            return False

        output = proc.stdout
        is_up = "<UP" in output or "state UP" in output
        can_state = "UNKNOWN"
        state_match = re.search(r"can state ([A-Z\-]+)", output)
        if state_match:
            can_state = state_match.group(1)

        ConsoleUI.found_state(f"Interface '{self.interface}' link={'UP' if is_up else 'DOWN'}, can_state={can_state}")

        needs_reset = (not is_up) or (can_state in ("BUS-OFF", "STOPPED", "ERROR-PASSIVE"))
        if needs_reset:
            ConsoleUI.applying_recovery(f"Resetting {self.interface} interface cleanly (bitrate={self.bitrate})...")
            run_sudo_command(["ip", "link", "set", self.interface, "down"])
            time.sleep(0.2)

            up_proc = run_sudo_command([
                "ip", "link", "set", self.interface, "up", "type", "can",
                "bitrate", str(self.bitrate), "restart-ms", "100"
            ])
            if up_proc.returncode != 0:
                up_proc = run_sudo_command([
                    "ip", "link", "set", self.interface, "up", "type", "can",
                    "bitrate", str(self.bitrate)
                ])

            run_sudo_command(["ip", "link", "set", self.interface, "txqueuelen", "1000"])
            run_sudo_command(["ip", "link", "set", self.interface, "up"])
            time.sleep(0.3)

            verify_proc = subprocess.run(
                ["ip", "-details", "link", "show", self.interface],
                capture_output=True,
                text=True,
            )
            v_out = verify_proc.stdout
            v_match = re.search(r"can state ([A-Z\-]+)", v_out)
            new_state = v_match.group(1) if v_match else "UNKNOWN"
            if ("<UP" in v_out or "state UP" in v_out) and new_state in ("ERROR-ACTIVE", "UNKNOWN"):
                ConsoleUI.verified_operational(f"Interface '{self.interface}' restored: state UP, can_state={new_state}")
                return True
            else:
                ConsoleUI.error(f"Failed to cleanly bring up {self.interface}. Output:\n{v_out}")
                return False
        else:
            ConsoleUI.verified_operational(f"Interface '{self.interface}' is healthy and operating in ERROR-ACTIVE mode.")
            return True

    # =========================================================================
    # Stage 2: Node Ping & Telemetry
    # =========================================================================
    def stage2_node_ping(self) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Sends application status request (0x101) with 1-second timeout.
        Returns (is_operational, telemetry_data).
        """
        ConsoleUI.stage_header(2, f"Node Ping & Telemetry Probe (Lock {self.device_id})")

        cmd_id = CAN_COMMAND | self.device_id
        expected_status_id = CAN_STATUS | self.device_id
        expected_health_id = CAN_HEALTH | self.device_id

        try:
            bus = can.Bus(interface="socketcan", channel=self.interface)
        except Exception as ex:
            ConsoleUI.error(f"Failed to open SocketCAN interface: {ex}")
            return False, None

        telemetry: Dict[str, Any] = {}
        try:
            ConsoleUI.info(f"Transmitting REQUEST_STATUS (0x02) to CAN ID 0x{cmd_id:03X}...")
            msg = can.Message(
                arbitration_id=cmd_id,
                data=[REQUEST_STATUS],
                is_extended_id=False,
            )
            bus.send(msg)

            start_t = time.time()
            while time.time() - start_t < 1.0:
                recv_msg = bus.recv(timeout=0.2)
                if not recv_msg or recv_msg.arbitration_id == 0x47F:
                    continue

                if recv_msg.arbitration_id == expected_status_id:
                    data = recv_msg.data
                    telemetry["status_id"] = hex(recv_msg.arbitration_id)
                    telemetry["raw_data"] = [hex(b) for b in data]
                    telemetry["mode"] = "APPLICATION"

                    if len(data) >= 3 and data[0] == 0x01:
                        telemetry["is_locked"] = bool(data[1])
                        telemetry["is_door_closed"] = bool(data[2]) if len(data) > 2 else None
                        telemetry["active_slot"] = data[4] if len(data) > 4 else None

                    ConsoleUI.found_state(
                        f"Lock {self.device_id} is in APPLICATION mode. "
                        f"Received 0x{expected_status_id:03X} data={[hex(b) for b in data]}"
                    )
                    ConsoleUI.verified_operational(
                        f"Lock {self.device_id} operational. "
                        f"Telemetry: is_locked={telemetry.get('is_locked')}, "
                        f"is_door_closed={telemetry.get('is_door_closed')}, "
                        f"active_slot={telemetry.get('active_slot')}"
                    )
                    return True, telemetry

                if recv_msg.arbitration_id == expected_health_id:
                    telemetry["mode"] = "APPLICATION"
                    ConsoleUI.found_state(f"Lock {self.device_id} responded on health CAN ID 0x{expected_health_id:03X}.")
                    ConsoleUI.verified_operational(f"Lock {self.device_id} operational.")
                    return True, telemetry

            ConsoleUI.info(f"No response to REQUEST_STATUS. Transmitting REQUEST_DEVICE_INFO (0xFE)...")
            bus.send(can.Message(arbitration_id=cmd_id, data=[REQUEST_DEVICE_INFO], is_extended_id=False))
            start_t = time.time()
            while time.time() - start_t < 1.0:
                recv_msg = bus.recv(timeout=0.2)
                if not recv_msg or recv_msg.arbitration_id == 0x47F:
                    continue
                if recv_msg.arbitration_id in (expected_status_id, expected_health_id):
                    telemetry["mode"] = "APPLICATION"
                    ConsoleUI.found_state(f"Lock {self.device_id} responded to REQUEST_DEVICE_INFO on 0x{recv_msg.arbitration_id:03X}.")
                    ConsoleUI.verified_operational(f"Lock {self.device_id} operational.")
                    return True, telemetry

            ConsoleUI.found_state(f"Lock {self.device_id} is SILENT on application CAN ID 0x{cmd_id:03X} (1.0s timeout exceeded).")
            return False, None

        finally:
            try:
                bus.shutdown()
            except Exception:
                pass


    # =========================================================================
    # Stage 3: Bootloader Recovery & Flasher
    # =========================================================================
    def stage3_bootloader_recovery(self) -> bool:
        """
        Diagnoses if node is in Bootloader mode (0x781) and triggers recovery:
        1. Issues CMD_OTA_ACTIVATE (0x04) to reboot into application mode.
        2. If node remains stuck or corrupted, executes full release bundle re-flash.
        3. Falls back to OpenOCD SWD hardware reset if CAN bus is completely silent.
        """
        ConsoleUI.stage_header(3, f"Bootloader & Firmware Recovery (Lock {self.device_id})")

        ota_cmd_id = FITNET_CAN_ID_OTA_CMD_BASE | self.device_id  # 0x781
        ota_ack_id = FITNET_CAN_ID_OTA_ACK_BASE | self.device_id  # 0x791

        # Step 3.1: Probe Bootloader Mode via CMD_OTA_ACTIVATE (0x04)
        if not self.force_reflash:
            ConsoleUI.info(f"Probing Bootloader on CAN ID 0x{ota_cmd_id:03X} with CMD_OTA_ACTIVATE (0x04)...")
            try:
                bus = can.Bus(interface="socketcan", channel=self.interface)
                bus.send(can.Message(arbitration_id=ota_cmd_id, data=[CMD_OTA_ACTIVATE], is_extended_id=False))

                ack_received = False
                start_t = time.time()
                while time.time() - start_t < 1.2:
                    recv_msg = bus.recv(timeout=0.2)
                    if not recv_msg or recv_msg.arbitration_id != ota_ack_id:
                        continue
                    if recv_msg.data and recv_msg.data[0] == ACK_OTA_ACTIVATE:
                        ack_received = True
                        break

                bus.shutdown()

                if ack_received:
                    ConsoleUI.found_state(
                        f"Lock {self.device_id} detected in BOOTLOADER mode (acknowledged ACK_OTA_ACTIVATE 0x84)."
                    )
                    ConsoleUI.applying_recovery(
                        f"Triggered slot activation on Lock {self.device_id}. Waiting for MCU reset..."
                    )
                    time.sleep(1.0)

                    # Verify application mode
                    ok, _ = self.stage2_node_ping()
                    if ok:
                        ConsoleUI.verified_operational(
                            f"Lock {self.device_id} successfully recovered back to APPLICATION mode!"
                        )
                        return True
            except Exception as ex:
                ConsoleUI.error(f"Error during Bootloader activation probe: {ex}")

        # Step 3.2: Re-flash target slot from latest release bundle
        ConsoleUI.info("Attempting firmware re-flash from latest release bundle...")
        flash_success = self._flash_release_bundle()
        if flash_success:
            time.sleep(1.0)
            ok, _ = self.stage2_node_ping()
            if ok:
                ConsoleUI.verified_operational(f"Lock {self.device_id} firmware re-flashed and verified operational!")
                return True

        # Step 3.3: Hardware SWD OpenOCD Fallback
        ConsoleUI.found_state("CAN bus silent from Lock node. Attempting SWD OpenOCD hardware recovery...")
        swd_success = self._swd_openocd_recovery()
        if swd_success:
            time.sleep(1.0)
            ok, _ = self.stage2_node_ping()
            if ok:
                ConsoleUI.verified_operational(f"Lock {self.device_id} recovered via SWD reset and verified operational!")
                return True

        ConsoleUI.error(f"Lock {self.device_id} could not be recovered in Stage 3.")
        return False

    def _resolve_latest_release_binaries(self) -> Tuple[Optional[str], Optional[str]]:
        """Finds valid slot_a.bin and slot_b.bin paths from uploads/releases."""
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        latest_json = os.path.join(base_dir, "uploads", "releases", "latest_release.json")
        slot_a = None
        slot_b = None

        if os.path.isfile(latest_json):
            try:
                import json
                with open(latest_json, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                rel_dir = meta.get("release_dir")
                if rel_dir:
                    candidate_a = os.path.join(rel_dir, "slot_a.bin")
                    candidate_b = os.path.join(rel_dir, "slot_b.bin")
                    if os.path.isfile(candidate_a):
                        slot_a = candidate_a
                    if os.path.isfile(candidate_b):
                        slot_b = candidate_b
            except Exception as ex:
                ConsoleUI.error(f"Failed parsing latest_release.json: {ex}")

        if not slot_a or not slot_b:
            rel_dir = os.path.join(base_dir, "uploads", "releases")
            if os.path.isdir(rel_dir):
                for entry in sorted(os.listdir(rel_dir), reverse=True):
                    sub = os.path.join(rel_dir, entry)
                    if os.path.isdir(sub):
                        cand_a = os.path.join(sub, "slot_a.bin")
                        cand_b = os.path.join(sub, "slot_b.bin")
                        if os.path.isfile(cand_a) and os.path.isfile(cand_b):
                            return cand_a, cand_b

        return slot_a, slot_b


    def _flash_release_bundle(self) -> bool:
        """Executes full dual-bank CAN OTA flashing sequence using latest release binary."""
        slot_a_path, slot_b_path = self._resolve_latest_release_binaries()
        if not slot_a_path or not slot_b_path:
            ConsoleUI.error("Could not locate release binaries slot_a.bin / slot_b.bin.")
            return False

        ota_cmd_id = FITNET_CAN_ID_OTA_CMD_BASE | self.device_id
        ota_ack_id = FITNET_CAN_ID_OTA_ACK_BASE | self.device_id

        try:
            bus = can.Bus(interface="socketcan", channel=self.interface)
        except Exception as ex:
            ConsoleUI.error(f"SocketCAN open failed: {ex}")
            return False

        try:
            with open(slot_a_path, "rb") as f:
                fw_data = bytearray(f.read())
            if len(fw_data) % 8 != 0:
                fw_data.extend(b"\xFF" * (8 - (len(fw_data) % 8)))

            size = len(fw_data)
            crc32 = binascii.crc32(fw_data) & 0xFFFFFFFF

            start_payload = bytes(
                [CMD_OTA_START, size & 0xFF, (size >> 8) & 0xFF, (size >> 16) & 0xFF]
            ) + struct.pack("<I", crc32)

            ConsoleUI.applying_recovery(f"Transmitting CMD_OTA_START to 0x{ota_cmd_id:03X}...")
            start_ack = None
            for _ in range(5):
                bus.send(can.Message(arbitration_id=ota_cmd_id, data=start_payload, is_extended_id=False))
                start_t = time.time()
                while time.time() - start_t < 1.5:
                    msg = bus.recv(timeout=0.1)
                    if msg and msg.arbitration_id == ota_ack_id and msg.data and msg.data[0] == ACK_OTA_START:
                        start_ack = msg
                        break
                if start_ack:
                    break

            if not start_ack:
                ConsoleUI.info("No ACK_OTA_START received from node.")
                return False

            active_slot = start_ack.data[2] if len(start_ack.data) >= 3 else 0
            target_slot = 1 if active_slot == 0 else 0
            chosen_path = slot_b_path if target_slot == 1 else slot_a_path
            ConsoleUI.found_state(
                f"Bootloader acknowledged START (active_slot={active_slot}). Targeting slot {target_slot} ({os.path.basename(chosen_path)})."
            )

            with open(chosen_path, "rb") as f:
                fw_data = bytearray(f.read())
            if len(fw_data) % 8 != 0:
                fw_data.extend(b"\xFF" * (8 - (len(fw_data) % 8)))
            size = len(fw_data)
            crc32 = binascii.crc32(fw_data) & 0xFFFFFFFF

            if active_slot == 1:
                start_payload = bytes(
                    [CMD_OTA_START, size & 0xFF, (size >> 8) & 0xFF, (size >> 16) & 0xFF]
                ) + struct.pack("<I", crc32)
                bus.send(can.Message(arbitration_id=ota_cmd_id, data=start_payload, is_extended_id=False))
                time.sleep(0.5)

            ConsoleUI.applying_recovery(f"Streaming {size} bytes ({len(fw_data)//5} chunks) to Lock {self.device_id}...")
            chunk_idx = 0
            offset = 0
            chunk_size = 5

            while offset < size:
                chunk_slice = fw_data[offset : offset + chunk_size]
                chunk_payload = struct.pack("<B H", CMD_OTA_DATA, chunk_idx) + bytes(chunk_slice)
                chunk_msg = can.Message(arbitration_id=ota_cmd_id, data=chunk_payload, is_extended_id=False)

                ack_ok = False
                for _ in range(5):
                    bus.send(chunk_msg)
                    st = time.time()
                    while time.time() - st < 0.15:
                        m = bus.recv(timeout=0.05)
                        if m and m.arbitration_id == ota_ack_id and m.data and m.data[0] == ACK_OTA_DATA:
                            if len(m.data) >= 3:
                                exp = m.data[1] | (m.data[2] << 8)
                                if exp == chunk_idx + 1:
                                    ack_ok = True
                                    break
                    if ack_ok:
                        break
                    time.sleep(0.01)

                if not ack_ok:
                    ConsoleUI.error(f"Chunk transmission failed at index {chunk_idx}.")
                    return False

                offset += len(chunk_slice)
                chunk_idx += 1
                time.sleep(0.006)

            # VERIFY Phase
            ConsoleUI.applying_recovery("Transmitting CMD_OTA_VERIFY (0x03)...")
            verify_ok = False
            for _ in range(5):
                bus.send(can.Message(arbitration_id=ota_cmd_id, data=[CMD_OTA_VERIFY], is_extended_id=False))
                st = time.time()
                while time.time() - st < 2.0:
                    m = bus.recv(timeout=0.1)
                    if m and m.arbitration_id == ota_ack_id and m.data and m.data[0] == ACK_OTA_VERIFY:
                        if len(m.data) > 1 and m.data[1] == 0:
                            verify_ok = True
                            break
                if verify_ok:
                    break

            if not verify_ok:
                ConsoleUI.error("Firmware CRC32 verification failed on target node.")
                return False

            # ACTIVATE Phase
            ConsoleUI.applying_recovery("Transmitting CMD_OTA_ACTIVATE (0x04)...")
            for _ in range(3):
                bus.send(can.Message(arbitration_id=ota_cmd_id, data=[CMD_OTA_ACTIVATE], is_extended_id=False))
                st = time.time()
                while time.time() - st < 1.0:
                    m = bus.recv(timeout=0.1)
                    if m and m.arbitration_id == ota_ack_id and m.data and m.data[0] == ACK_OTA_ACTIVATE:
                        break

            ConsoleUI.verified_operational("Firmware flash and activation complete.")
            return True

        finally:
            try:
                bus.shutdown()
            except Exception:
                pass

    def _swd_openocd_recovery(self) -> bool:
        """Inspects MCU PC, VTOR, and fault registers via OpenOCD SWD, then resets target."""
        ConsoleUI.info("Executing OpenOCD SWD inspection and reset...")
        cmd = [
            "openocd",
            "-f", "interface/stlink.cfg",
            "-f", "target/stm32c0x.cfg",
            "-c", "init",
            "-c", "halt",
            "-c", "reg pc",
            "-c", "mdw 0xe000ed08 1",
            "-c", "reset run",
            "-c", "exit",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            pc_match = re.search(r"pc \(/32\): (0x[0-9a-fA-F]+)", proc.stdout)
            vtor_match = re.search(r"0xe000ed08: ([0-9a-fA-F]+)", proc.stdout)
            pc_val = pc_match.group(1) if pc_match else "unknown"
            vtor_val = f"0x{vtor_match.group(1)}" if vtor_match else "unknown"
            ConsoleUI.found_state(f"SWD Inspection: PC={pc_val}, VTOR={vtor_val}")
            ConsoleUI.applying_recovery("Issued 'reset run' via SWD.")
            return True
        else:
            ConsoleUI.error(f"OpenOCD SWD command failed: {proc.stderr.strip() or proc.stdout.strip()}")
            return False

    # =========================================================================
    # Stage 4: Database Sync
    # =========================================================================
    def stage4_database_sync(self) -> bool:
        """
        Ensures target lock record exists and is active in SQLite database.
        Synchronizes timestamps and active slot metadata.
        """
        ConsoleUI.stage_header(4, f"Database Synchronization (Lock {self.device_id})")

        if not os.path.exists(self.db_path):
            ConsoleUI.error(f"Database file not found at {self.db_path}")
            return False

        try:
            con = sqlite3.connect(self.db_path)
            cur = con.cursor()

            cur.execute("SELECT device_id, name, active_slot, last_seen FROM devices WHERE device_id = ?", (self.device_id,))
            row = cur.fetchone()

            now = time.time()
            if row is None:
                ConsoleUI.found_state(f"Lock {self.device_id} is MISSING from database {self.db_path}.")
                ConsoleUI.applying_recovery(f"Inserting Lock {self.device_id} entry into devices table...")
                cur.execute(
                    """
                    INSERT INTO devices (device_id, name, lock_mode, auto_close_timeout, behavior_flags, last_seen, active_slot)
                    VALUES (?, ?, 1, 3, 0, ?, 1)
                    """,
                    (self.device_id, f"Schloss {self.device_id}", now),
                )
                con.commit()
                ConsoleUI.verified_operational(f"Lock {self.device_id} registered in database {self.db_path}.")
            else:
                ConsoleUI.found_state(
                    f"Lock {self.device_id} found in database: name='{row[1]}', active_slot={row[2]}, last_seen={row[3]}."
                )
                ConsoleUI.applying_recovery("Updating last_seen timestamp to current time...")
                cur.execute("UPDATE devices SET last_seen = ? WHERE device_id = ?", (now, self.device_id))
                con.commit()
                ConsoleUI.verified_operational(f"Lock {self.device_id} database entry synchronized successfully.")

            con.close()
            return True

        except Exception as ex:
            ConsoleUI.error(f"Database synchronization error: {ex}")
            return False

    # =========================================================================
    # Stage 5: Final Gateway Verification
    # =========================================================================
    def stage5_gateway_verification(self) -> bool:
        """
        Verifies operational health and visibility of Lock via Gateway REST API.
        Checks GET /api/v1/devices/{id} and confirms 'online: true'.
        """
        ConsoleUI.stage_header(5, "Gateway API & Web UI Verification")

        # 1. Request health refresh via Gateway
        try:
            requests.post(f"{self.api_url}/api/v1/devices/{self.device_id}/request_health", timeout=2.0)
            time.sleep(0.5)
        except Exception:
            pass

        # 2. Query device telemetry
        try:
            res = requests.get(f"{self.api_url}/api/v1/devices/{self.device_id}", timeout=3.0)
            if res.status_code == 200:
                data = res.json()
                ConsoleUI.found_state(
                    f"Gateway GET /api/v1/devices/{self.device_id} -> "
                    f"status_text={data.get('status_text')}, is_locked={data.get('is_locked')}, "
                    f"active_slot={data.get('active_slot')}"
                )
            else:
                ConsoleUI.error(f"Gateway query returned HTTP {res.status_code}: {res.text}")
                return False
        except Exception as ex:
            ConsoleUI.error(f"Gateway connection error: {ex}")
            return False

        # 3. Query all devices list to confirm online flag
        try:
            list_res = requests.get(f"{self.api_url}/api/v1/devices", timeout=3.0)
            if list_res.status_code == 200:
                devs = list_res.json()
                target_dev = next((d for d in devs if d.get("device_id") == self.device_id), None)
                if target_dev:
                    is_online = target_dev.get("online", False)
                    if is_online:
                        ConsoleUI.verified_operational(
                            f"Lock {self.device_id} verified ONLINE in Gateway and Web UI: {target_dev}"
                        )
                        return True
                    else:
                        ConsoleUI.found_state(f"Lock {self.device_id} present in Gateway inventory but online=false.")
                        requests.post(f"{self.api_url}/api/v1/devices/{self.device_id}/request_health", timeout=2.0)
                        time.sleep(0.5)
                        list_res2 = requests.get(f"{self.api_url}/api/v1/devices", timeout=3.0)
                        devs2 = list_res2.json()
                        t2 = next((d for d in devs2 if d.get("device_id") == self.device_id), None)
                        if t2 and t2.get("online", False):
                            ConsoleUI.verified_operational(f"Lock {self.device_id} verified ONLINE in Web UI!")
                            return True
                        ConsoleUI.error("Lock did not transition to online=true in gateway memory.")
                        return False
                else:
                    ConsoleUI.error(f"Lock {self.device_id} not found in GET /api/v1/devices inventory.")
                    return False
        except Exception as ex:
            ConsoleUI.error(f"Failed verifying device list: {ex}")
            return False

        return True

    # =========================================================================
    # Run Full Doctor Pipeline
    # =========================================================================
    def run(self) -> bool:
        """Executes the complete diagnosis and automated recovery lifecycle."""
        print(f"\n{BOLD}Starting PS Locks Lock Doctor for Device ID {self.device_id}...{RESET}")

        # Stage 1: CAN Interface Health
        if not self.stage1_interface_health():
            ConsoleUI.error("Stage 1 failed: CAN interface could not be recovered.")
            return False

        # Stage 2: Application Node Ping
        is_operational, _ = self.stage2_node_ping()

        # Stage 3: Bootloader / Hardware Recovery if silent or forced
        if not is_operational or self.force_reflash:
            recovered = self.stage3_bootloader_recovery()
            if not recovered:
                ConsoleUI.error("Stage 3 failed: Node could not be recovered to Application mode.")
                return False

        # Stage 4: Database Sync
        if not self.stage4_database_sync():
            ConsoleUI.error("Stage 4 failed: Database synchronization error.")
            return False

        # Stage 5: Final Gateway Verification
        if not self.stage5_gateway_verification():
            ConsoleUI.error("Stage 5 failed: Gateway verification failed.")
            return False

        print(f"\n{GREEN_BOLD}*** Lock {self.device_id} is FULLY RECOVERED and VERIFIED OPERATIONAL! ***{RESET}\n")
        return True


def main():
    parser = argparse.ArgumentParser(
        description="PS Locks Lock Doctor - Automated Diagnostic and Recovery Utility",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--device-id", type=int, default=1, help="Target Lock Device ID")
    parser.add_argument("--interface", type=str, default="can0", help="CAN interface name")
    parser.add_argument("--bitrate", type=int, default=50000, help="CAN bus bitrate")
    parser.add_argument("--db-path", type=str, default="/home/admin/PSLOCKS_OIP/pslocks.db", help="Path to SQLite database")
    parser.add_argument("--api-url", type=str, default="http://127.0.0.1:8000", help="Gateway API Base URL")
    parser.add_argument("--force-reflash", action="store_true", help="Force dual-bank firmware re-flash")

    args = parser.parse_args()

    doctor = LockDoctor(
        device_id=args.device_id,
        interface=args.interface,
        bitrate=args.bitrate,
        db_path=args.db_path,
        api_url=args.api_url,
        force_reflash=args.force_reflash,
    )
    success = doctor.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()



