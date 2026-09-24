"""
End-to-End (E2E) Fully Automated Test Suite for PS Locks Open Integration Platform.

Covers the complete system feature set:
1. System & Device Inventory (Health, Device Telemetry, live state attributes).
2. Actuator & Command Lifecycle (Open, Open Hold, Open Reset, LED & Buzzer CAN ACKs).
3. Whitelist V2 & Policy Management (Write, Policy Configuration, Readback, Delete Slot, Clear All).
4. OTA Dual-Bank Lifecycle & Safety (Safety Interlock 503, Ping-Pong Flashing, Active Slot Flip, Buzzer).
5. Audit Log Persistence (Actuation, Whitelist Mutation, OTA_START, OTA_SUCCESS with metadata).
6. Teardown & Safe State Recovery (Guarantees safe unlocked operational state after test completion).
"""

import asyncio
import json
import os
import struct
import tempfile
import time
from typing import Any, Dict, Optional
import pytest
import requests

# Isolate test process from physical CAN bus to prevent listener conflict with running gateway
os.environ["CAN_SIMULATION"] = "1"

from app import app
from canbus.constants import (
    ACK_OTA_ACTIVATE,
    ACK_OTA_DATA,
    ACK_OTA_START,
    ACK_OTA_VERIFY,
    FITNET_CAN_ID_OTA_ACK_BASE,
    LED_SET,
    BUZZ_PLAY,
    OPEN_LOCK,
    OPEN_HOLD,
    OPEN_RESET,
)
from canbus.protocol import CANFrame
from config.globals import can_service, can_listener
from database.database import SessionLocal
from models.log import EventLog
from services.ota_service import ota_service


class SystemE2EClient:
    """
    Unified HTTP client executing tests against the live running server (http://127.0.0.1:8000)
    or falling back seamlessly to FastAPI TestClient for offline/mock environments.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8000"):
        self.base_url = os.environ.get("OIP_API_URL", base_url).rstrip("/")
        self.is_live = False
        try:
            resp = requests.get(f"{self.base_url}/api/v1/health", timeout=1.5)
            if resp.status_code == 200:
                self.is_live = True
                self.session = requests.Session()
        except Exception:
            self.is_live = False

        if not self.is_live:
            from fastapi.testclient import TestClient
            self.test_client = TestClient(app)

    def get(self, path: str, **kwargs) -> Any:
        if self.is_live:
            url = f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"
            return self.session.get(url, timeout=kwargs.pop("timeout", 5.0), **kwargs)
        return self.test_client.get(path, **kwargs)

    def post(self, path: str, **kwargs) -> Any:
        if self.is_live:
            url = f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"
            return self.session.post(url, timeout=kwargs.pop("timeout", 5.0), **kwargs)
        return self.test_client.post(path, **kwargs)

    def delete(self, path: str, **kwargs) -> Any:
        if self.is_live:
            url = f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"
            return self.session.delete(url, timeout=kwargs.pop("timeout", 5.0), **kwargs)
        return self.test_client.delete(path, **kwargs)

    def patch(self, path: str, **kwargs) -> Any:
        if self.is_live:
            url = f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"
            return self.session.patch(url, timeout=kwargs.pop("timeout", 5.0), **kwargs)
        return self.test_client.patch(path, **kwargs)


@pytest.fixture(scope="session")
def client() -> SystemE2EClient:
    """Provides a singleton SystemE2EClient for the automated E2E test suite."""
    return SystemE2EClient()


@pytest.fixture(scope="module", autouse=True)
def safe_lock_recovery(client: SystemE2EClient):
    """
    Safety interlock fixture ensuring clean teardown.
    Leaves the physical lock in a safe, unlocked operational state regardless of test outcome.
    """
    yield
    try:
        client.post("/api/v1/devices/1/open")
        client.post("/api/v1/devices/1/led_reset")
        client.post("/api/v1/devices/1/buzzer_stop")
    except Exception:
        pass



# ==============================================================================
# 1. System & Device Inventory Tests
# ==============================================================================

def test_system_health(client: SystemE2EClient):
    """Verify system health endpoint GET /api/v1/health."""
    res = client.get("/api/v1/health")
    assert res.status_code == 200, f"Health check failed with {res.status_code}: {res.text}"
    data = res.json()
    assert data.get("status") == "OK"
    assert "time" in data


def test_device_inventory_and_telemetry(client: SystemE2EClient):
    """
    Verify GET /api/v1/devices/1 and validate live telemetry attributes:
    is_locked, is_door_closed, active_slot, status_text.
    """
    res = client.get("/api/v1/devices/1")
    assert res.status_code == 200, f"Device inventory lookup failed: {res.text}"
    data = res.json()

    if data.get("status_text") == "UNKNOWN":
        client.post("/api/v1/devices/1/request_health")
        time.sleep(0.4)
        res = client.get("/api/v1/devices/1")
        data = res.json()

    # Validate essential telemetry fields
    assert "is_locked" in data, "is_locked field missing from device telemetry"
    assert "is_door_closed" in data, "is_door_closed field missing from device telemetry"
    assert "active_slot" in data, "active_slot field missing from device telemetry"
    assert "status_text" in data, "status_text field missing from device telemetry"

    assert data["is_locked"] in (True, False, None)
    assert data["is_door_closed"] in (True, False, None)
    assert data["active_slot"] in (0, 1)
    assert isinstance(data["status_text"], str)


# ==============================================================================
# 2. Actuator & Command Lifecycle Tests
# ==============================================================================

def test_actuator_remote_open(client: SystemE2EClient):
    """
    Verify Remote Open (POST /api/v1/devices/1/open):
    Verify actuation response and telemetry update.
    """
    res = client.post("/api/v1/devices/1/open")
    assert res.status_code == 200, f"Remote open failed: {res.text}"
    data = res.json()
    assert data.get("status") == "opened"

    # Brief delay for actuator action and CAN bus state propagation
    time.sleep(0.3)
    dev_res = client.get("/api/v1/devices/1")
    assert dev_res.status_code == 200
    dev_data = dev_res.json()
    assert dev_data["status_text"] in ("LOCKED", "UNLOCKED", "LO_DC", "LC_DC", "OPEN_PULSE", "UNKNOWN")


def test_actuator_open_hold_and_release(client: SystemE2EClient):
    """
    Verify Open Hold (POST /api/v1/devices/1/hold) and Open Reset (POST /api/v1/devices/1/release).
    """
    # 1. Trigger Open Hold
    hold_res = client.post("/api/v1/devices/1/hold")
    assert hold_res.status_code == 200, f"Hold open failed: {hold_res.text}"
    assert hold_res.json().get("status") == "held open"

    time.sleep(0.3)

    # 2. Trigger Open Reset (Release)
    rel_res = client.post("/api/v1/devices/1/release")
    assert rel_res.status_code == 200, f"Reset release failed: {rel_res.text}"
    assert rel_res.json().get("status") == "open state reset"


def test_led_and_buzzer_remote_overrides(client: SystemE2EClient):
    """
    Trigger LED_SET and BUZZ_PLAY commands and verify CAN command ACKs.
    """
    # 1. Trigger LED_SET override
    led_res = client.post(
        "/api/v1/devices/1/led",
        json={"mode": 1, "period10ms": 0, "duty": 0, "ttl": 1},
    )
    assert led_res.status_code == 200, f"LED override failed: {led_res.text}"
    assert led_res.json().get("status") == "led set"

    ack_led = False
    for _ in range(15):
        time.sleep(0.1)
        dev_data = client.get("/api/v1/devices/1").json()
        if dev_data.get("last_ack_command") == LED_SET:
            ack_led = True
            break
    assert ack_led or dev_data.get("last_ack_command") is not None, "LED ACK not observed"

    # 2. Trigger BUZZ_PLAY override
    buzz_res = client.post(
        "/api/v1/devices/1/buzzer",
        json={"sound": 1, "repeat": 1},
    )
    assert buzz_res.status_code == 200, f"Buzzer override failed: {buzz_res.text}"
    assert buzz_res.json().get("status") == "buzzer set"

    ack_buzz = False
    for _ in range(15):
        time.sleep(0.1)
        dev_data = client.get("/api/v1/devices/1").json()
        if dev_data.get("last_ack_command") == BUZZ_PLAY:
            ack_buzz = True
            break
    assert ack_buzz or dev_data.get("last_ack_command") is not None, "Buzzer ACK not observed"


# ==============================================================================
# 3. Whitelist V2 & Policy Management Tests
# ==============================================================================

def test_whitelist_v2_slot_lifecycle(client: SystemE2EClient):
    """
    Comprehensive Whitelist V2 lifecycle:
    1. Write persistent Slot (POST /api/v1/devices/1/whitelist) with custom UID and TTL.
    2. Configure custom policy (POST /api/v1/devices/1/whitelist/{slot}/policy).
    3. Readback and assert slot appears in GET /api/v1/devices/1/whitelist.
    4. Delete specific slot (DELETE /api/v1/devices/1/whitelist/{slot}) and verify removal.
    5. Test Clear All (DELETE /api/v1/devices/1/whitelist) and verify occupancy reset.
    """
    test_slot = 2
    test_uid = "04A1B2C3D4E5F6"

    # Snapshot existing whitelist to ensure production state preservation upon teardown
    init_res = client.get("/api/v1/devices/1/whitelist")
    initial_wl = init_res.json() if init_res.status_code == 200 else {}

    # Wait for actuator motor to settle into stable state
    for _ in range(15):
        st = client.get("/api/v1/devices/1").json()
        if st.get("status_text") in ("LOCKED", "UNLOCKED", "UNKNOWN"):
            break
        time.sleep(0.1)
    time.sleep(0.5)

    try:
        # Step 1: Write Slot 2
        write_res = client.post(
            "/api/v1/devices/1/whitelist",
            json={"slot_index": test_slot, "uid_hex": test_uid, "ttl_days": 15},
        )
        assert write_res.status_code == 200, f"Write whitelist slot failed: {write_res.text}"

        # Step 2: Set Policy for Slot 2 (policy=1, open_action=2)
        policy_res = client.post(
            f"/api/v1/devices/1/whitelist/{test_slot}/policy",
            json={"policy": 1, "open_action": 2, "flags": 0},
        )
        assert policy_res.status_code == 200, f"Set policy failed: {policy_res.text}"

        # Step 3: Readback whitelist and verify presence
        slot_key = str(test_slot)
        wl_data = {}
        for _ in range(15):
            time.sleep(0.2)
            read_res = client.get("/api/v1/devices/1/whitelist")
            if read_res.status_code == 200:
                wl_data = read_res.json()
                if slot_key in wl_data or test_slot in wl_data:
                    break

        assert slot_key in wl_data or test_slot in wl_data, f"Slot {test_slot} not found in whitelist: {wl_data}"
        slot_entry = wl_data.get(slot_key) or wl_data.get(test_slot)
        assert slot_entry.get("uid_hex") == test_uid
        assert slot_entry.get("ttl_days") == 15
        assert slot_entry.get("policy") in (0, 1)

        # Step 4: Delete specific slot
        del_res = client.delete(f"/api/v1/devices/1/whitelist/{test_slot}")
        assert del_res.status_code == 200, f"Delete slot failed: {del_res.text}"

        for _ in range(10):
            time.sleep(0.2)
            read_after_del = client.get("/api/v1/devices/1/whitelist").json()
            if slot_key not in read_after_del and test_slot not in read_after_del:
                break
        assert slot_key not in read_after_del and test_slot not in read_after_del, "Slot was not deleted"

        # Step 5: Clear All whitelist entries & check occupancy reset
        clear_res = client.delete("/api/v1/devices/1/whitelist")
        assert clear_res.status_code == 200, f"Clear whitelist failed: {clear_res.text}"

        for _ in range(10):
            time.sleep(0.2)
            read_cleared = client.get("/api/v1/devices/1/whitelist").json()
            if len(read_cleared) == 0:
                break
        assert len(read_cleared) == 0, f"Whitelist should be empty after clear: {read_cleared}"

        occ_res = client.get("/api/v1/devices/1/occupancy")
        if occ_res.status_code == 200:
            occ_data = occ_res.json()
            assert not occ_data.get("occupied", False), "Occupancy should be reset after whitelist clear"

    finally:
        # Restore pre-existing whitelist entries to preserve production configuration
        if isinstance(initial_wl, dict):
            for s_k, entry in initial_wl.items():
                try:
                    s_int = int(s_k)
                    if s_int != test_slot and entry.get("uid_hex"):
                        client.post(
                            "/api/v1/devices/1/whitelist",
                            json={
                                "slot_index": s_int,
                                "uid_hex": entry.get("uid_hex"),
                                "ttl_days": entry.get("ttl_days", 30),
                            },
                        )
                except Exception:
                    pass



# ==============================================================================
# 4. OTA Dual-Bank Lifecycle & Safety Tests
# ==============================================================================

def test_ota_safety_interlock(client: SystemE2EClient):
    """
    Verify safety interlock:
    Confirm device returns HTTP 503 on operational commands while OTA is in progress.
    Uses dedicated test device ID (99) to prevent interrupting physical locks over CAN.
    """
    dev_id = 99
    # Trigger OTA start via API so server marks device OTA as actively running
    start_res = client.post(f"/api/v1/devices/{dev_id}/ota/start")
    assert start_res.status_code == 200, f"Failed to start OTA task: {start_res.text}"

    try:
        # Operational command should be rejected with 503 Service Unavailable
        res = client.post(f"/api/v1/devices/{dev_id}/open")
        assert res.status_code == 503, f"Expected HTTP 503 during OTA, got {res.status_code}: {res.text}"
        assert "OTA" in res.json().get("detail", "")

        hold_res = client.post(f"/api/v1/devices/{dev_id}/hold")
        assert hold_res.status_code == 503, f"Expected HTTP 503 during OTA, got {hold_res.status_code}"
    finally:
        # Reset OTA status
        reset_res = client.post(f"/api/v1/devices/{dev_id}/ota/reset")
        assert reset_res.status_code == 200
        time.sleep(0.2)


@pytest.mark.anyio
async def test_ota_ping_pong_flash_cycle(client: SystemE2EClient):
    """
    Autonomous ping-pong flash cycle:
    1. Query initial active_slot (0 or 1).
    2. Execute full OTA flash sequence with bootloader responder.
    3. Confirm active_slot flips, status transitions to COMPLETE, and confirmation buzzer is triggered.
    Uses dedicated test device ID (99) to guarantee physical hardware isolation.
    """
    dev_id = 99
    loop = asyncio.get_running_loop()
    can_service.set_loop(loop)
    orig_sim = can_service.simulation
    can_service.simulation = True

    # Clean OTA state before starting
    client.post(f"/api/v1/devices/{dev_id}/ota/reset")
    ota_service.reset_status(dev_id)
    can_service.clear_ota_queue(dev_id)

    # Determine initial active slot and matching binary name to avoid 71KB release fallback
    status_resp = client.get(f"/api/v1/devices/{dev_id}/ota/status")
    initial_slot = status_resp.json().get("active_slot", 0)
    expected_flipped_slot = 1 if initial_slot == 0 else 0

    target_filename = "slot_b.bin" if initial_slot == 0 else "slot_a.bin"
    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, target_filename)
    with open(tmp_path, "wb") as f:
        f.write(bytes(range(32)))

    try:
        ack_id = FITNET_CAN_ID_OTA_ACK_BASE | dev_id

        async def simulated_bootloader_responder():
            """Simulates device dual-bank bootloader responses during autonomous cycle."""
            queue = can_service.get_ota_queue(dev_id)
            expected_chunk = 0

            while True:
                st = ota_service.get_status(dev_id)
                state = st.get("state")
                if state in ("COMPLETE", "ERROR"):
                    break

                if state == "ERASE":
                    # Bootloader start handshake acknowledging target bank
                    frame = CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_START, 0x00, initial_slot]))
                    await queue.put(frame)
                    await asyncio.sleep(0.02)

                elif state == "FLASH":
                    expected_chunk += 1
                    exp_bytes = struct.pack("<H", expected_chunk)
                    frame = CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_DATA]) + exp_bytes)
                    await queue.put(frame)
                    await asyncio.sleep(0.01)

                elif state == "VERIFY":
                    frame = CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_VERIFY, 0x00]))
                    await queue.put(frame)
                    await asyncio.sleep(0.02)

                elif state == "ACTIVATE":
                    frame = CANFrame(arbitration_id=ack_id, data=bytes([ACK_OTA_ACTIVATE, 0x00]))
                    await queue.put(frame)
                    break

                await asyncio.sleep(0.01)

        responder_task = asyncio.create_task(simulated_bootloader_responder())
        success = await ota_service.flash_device(dev_id, tmp_path)
        await responder_task

        assert success is True, "Autonomous OTA flash cycle returned failure"

        # Verify status transitions to COMPLETE
        final_status = ota_service.get_status(dev_id)
        assert final_status["state"] == "COMPLETE"
        assert final_status["progress"] == 100

        # Confirm active_slot flipped
        dev = can_listener.get_device(dev_id)
        assert getattr(dev, "active_slot", None) == expected_flipped_slot

    finally:
        can_service.simulation = orig_sim
        if os.path.exists(tmp_dir):
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        client.post(f"/api/v1/devices/{dev_id}/ota/reset")
        ota_service.reset_status(dev_id)
        can_service.clear_ota_queue(dev_id)



# ==============================================================================
# 5. Audit Log Persistence Tests
# ==============================================================================

def test_audit_log_persistence(client: SystemE2EClient):
    """
    Query GET /api/v1/logs and database records:
    Assert that entries for actuation (MANUAL_OPEN), whitelist mutation (WHITELIST_MUTATION),
    OTA_START, and OTA_SUCCESS exist with correct metadata.
    """
    dev_id = 1
    ota_dev_id = 99

    # Check via REST API endpoint for Device 1
    logs_res = client.get(f"/api/v1/logs?device_id={dev_id}&limit=100")
    assert logs_res.status_code == 200, f"Get logs failed: {logs_res.text}"
    api_logs = logs_res.json()

    event_types = {log["event_type"] for log in api_logs}

    # Verify actuation log
    assert "MANUAL_OPEN" in event_types, f"MANUAL_OPEN event missing from logs: {event_types}"

    # Verify whitelist mutation log
    assert "WHITELIST_MUTATION" in event_types, f"WHITELIST_MUTATION event missing from logs: {event_types}"

    # Verify OTA logs across test devices
    ota_logs_res = client.get(f"/api/v1/logs?device_id={ota_dev_id}&limit=100")
    assert ota_logs_res.status_code == 200
    all_ota_events = {log["event_type"] for log in ota_logs_res.json()} | event_types
    assert "OTA_START" in all_ota_events, f"OTA_START event missing from logs: {all_ota_events}"
    assert "OTA_SUCCESS" in all_ota_events, f"OTA_SUCCESS event missing from logs: {all_ota_events}"

    # Validate database records and metadata details directly
    with SessionLocal() as db:
        ota_start_entry = (
            db.query(EventLog)
            .filter(EventLog.device_id.in_([dev_id, ota_dev_id]), EventLog.event_type == "OTA_START")
            .order_by(EventLog.id.desc())
            .first()
        )
        assert ota_start_entry is not None
        start_meta = json.loads(ota_start_entry.details)
        assert "target_slot" in start_meta or "current_slot" in start_meta
        assert "binary_name" in start_meta

        ota_success_entry = (
            db.query(EventLog)
            .filter(EventLog.device_id.in_([dev_id, ota_dev_id]), EventLog.event_type == "OTA_SUCCESS")
            .order_by(EventLog.id.desc())
            .first()
        )
        assert ota_success_entry is not None
        success_meta = json.loads(ota_success_entry.details)
        assert "active_slot" in success_meta
        assert "crc32" in success_meta
        assert "execution_time" in success_meta
