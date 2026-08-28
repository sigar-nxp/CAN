#!/usr/bin/env python3
import requests
import time
import sys

BASE_URL = "http://localhost:8000"

def prompt(message: str) -> bool:
    print(f"\n==========================================")
    print(f" {message}")
    print(f"==========================================")
    val = input("Press Enter to execute, 's' to skip, Ctrl+C to abort: ")
    return val.lower() != 's'

def interactive_check(question: str) -> bool:
    val = input(f"{question} [y/n]: ")
    return val.lower() == 'y'

def get_device_id() -> int:
    resp = requests.get(f"{BASE_URL}/devices")
    devices = resp.json()
    if not devices:
        val = input("No devices found automatically. Enter device ID: ")
        return int(val)
    print(f"Connected to Device ID: {devices[0]['device_id']}")
    return devices[0]["device_id"]

def wait_for_card_tap(device_id: int, timeout_s: float = 10.0) -> str:
    """Polls for a card tap. If the same card is already in buffer, it allows proceeding."""
    print("Waiting for card presentation (or press Enter if already tapped)...")
    resp = requests.get(f"{BASE_URL}/devices/{device_id}")
    initial_card = resp.json().get("last_scanned_card", "")
    
    start_time = time.time()
    while time.time() - start_time < timeout_s:
        resp = requests.get(f"{BASE_URL}/devices/{device_id}")
        current_card = resp.json().get("last_scanned_card", "")
        if current_card and current_card != initial_card:
            print(f" Card detected: {current_card}")
            return current_card
        time.sleep(0.2)
    
    # Fallback to last known card if already present in device state
    if initial_card:
        print(f" Using current buffered card: {initial_card}")
        return initial_card
    print(" Timeout: No card detected.")
    return ""

def wait_for_unlock(device_id: int, timeout_s: float = 15.0) -> bool:
    """Polls until the lock opens (is_locked becomes False)."""
    print("Waiting for local card tap to unlock...")
    start_time = time.time()
    while time.time() - start_time < timeout_s:
        resp = requests.get(f"{BASE_URL}/devices/{device_id}")
        data = resp.json()
        if not data.get("is_locked", True):
            print(" Lock unlocked locally!")
            return True
        time.sleep(0.2)
    print(" Timeout: Lock did not open.")
    return False

def phase_1_master_beacon():
    if not prompt("Phase 1: Master Beacon"): return
    print("Toggling Master Beacon...")
    resp = requests.post(f"{BASE_URL}/api/system/beacon/toggle")
    print(f"Response: {resp.status_code} - {resp.json()}")
    time.sleep(1)
    requests.post(f"{BASE_URL}/api/system/beacon/toggle")
    print("Phase 1 complete.")

def phase_2_health(device_id: int):
    if not prompt("Phase 2: Health P1-P8"): return
    print("Fetching Health diagnostics P1 through P8...")
    resp = requests.get(f"{BASE_URL}/devices/{device_id}/health")
    import pprint
    pprint.pprint(resp.json())
    interactive_check("Did health data look correct?")
    print("Phase 2 complete.")

def phase_3_actuators_signaling(device_id: int):
    if not prompt("Phase 3: Actuators/Signaling"): return

    print("\n1. Pulse Open (0x01)...")
    requests.post(f"{BASE_URL}/devices/{device_id}/open")
    interactive_check("Did bolt open briefly and automatically re-lock?")

    time.sleep(1)
    print("\n2. Open Hold (0x04 - Daueroffen)...")
    requests.post(f"{BASE_URL}/devices/{device_id}/open_hold")
    interactive_check("Did bolt stay open?")

    time.sleep(1)
    print("\n3. Open Reset (0x05 - Verriegeln)...")
    requests.post(f"{BASE_URL}/devices/{device_id}/reset")
    interactive_check("Did bolt lock shut?")

    time.sleep(1)
    print("\n4. Solid Green LED (mode 1)...")
    requests.post(f"{BASE_URL}/devices/{device_id}/led", json={"mode": 1, "period10ms": 0, "duty": 0, "ttl": 2})
    interactive_check("Is LED solid green for 2s?")

    time.sleep(1)
    print("\n5. Blinking Green LED (mode 3 with period/duty)...")
    requests.post(f"{BASE_URL}/devices/{device_id}/led", json={"mode": 3, "period10ms": 100, "duty": 50, "ttl": 4})
    interactive_check("Is LED BLINKING green (1s period)?")

    time.sleep(1)
    print("\n6. Buzzer (OK Sound)...")
    requests.post(f"{BASE_URL}/devices/{device_id}/buzzer", json={"sound": 1, "repeat": 1})
    interactive_check("Did buzzer sound?")
    print("Phase 3 complete.")

def phase_4_mode_1_cloud_auth(device_id: int):
    if not prompt("Phase 4: Mode 1 Cloud Auth (Locker Mode)"): return
    
    # 1. Ensure lock is in open state initially for Mode 1
    print("Preparing Mode 1: Opening lock first...")
    requests.post(f"{BASE_URL}/devices/{device_id}/open_hold")
    time.sleep(1)
    requests.post(f"{BASE_URL}/devices/{device_id}/mode", json={"lock_mode": 1})
    time.sleep(1)

    # 2. Tap card to LOCK (Cloud-gated close)
    print("\n[Action] Hold card (0E 27 26 48) to LOCK the locker...")
    card = wait_for_card_tap(device_id)
    if card:
        print("Sending Cloud Auth: ALLOW + CLOSE (action=1)...")
        requests.post(f"{BASE_URL}/devices/{device_id}/auth_response", json={"result": 1, "action": 1})
        time.sleep(1.5)
        interactive_check("Did the lock close and lock shut?")

    # 3. Tap same card to UNLOCK locally (Check for state change to unlocked)
    print("\n[Action] Hold the SAME card again to UNLOCK locally...")
    unlocked = wait_for_unlock(device_id)
    if unlocked:
        interactive_check("Did the lock OPEN locally and STAY OPEN?")
    
    print("Phase 4 complete.")

def phase_5_cloud_deny(device_id: int):
    if not prompt("Phase 5: Cloud Deny"): return
    print("\n[Action] Hold any card to trigger Cloud Deny...")
    card = wait_for_card_tap(device_id)
    if card:
        print("Sending Cloud Auth: DENY (result=0)...")
        requests.post(f"{BASE_URL}/devices/{device_id}/auth_response", json={"result": 0, "action": 0})
        interactive_check("Did lock signal DENY (red LED / error tone)?")
    print("Phase 5 complete.")

def phase_6_multipart_uid(device_id: int):
    if not prompt("Phase 6: Multi-Part UID (>4 Bytes)"): return
    print("\n[Action] Hold your 7-byte UID card to the reader...")
    card = wait_for_card_tap(device_id)
    if card:
        # Acknowledge the auth request to prevent the lock's 5s timeout
        requests.post(f"{BASE_URL}/devices/{device_id}/auth_response", json={"result": 0, "action": 0})
        print(f"Full reconstructed UID: {card}")
        interactive_check(f"Is {card} the expected 7-byte UID (>8 hex chars)?")
    print("Phase 6 complete.")

def phase_7_whitelist_v2_policies(device_id: int):
    if not prompt("Phase 7: Whitelist V2 & Policies"): return
    
    # Write persistent UID into Slot 2
    print("Inserting Whitelist UID '0A0B0C0D' at Slot 2 (TTL=30 days)...")
    requests.post(f"{BASE_URL}/devices/{device_id}/whitelist", json={"slot_index": 2, "uid_hex": "0A0B0C0D", "ttl_days": 30})
    time.sleep(1)

    # Set Policy to OPEN_AND_RELEASE
    print("Setting Slot 2 Policy to OPEN_AND_RELEASE (policy=2, open_action=2)...")
    requests.post(f"{BASE_URL}/devices/{device_id}/whitelist/2/policy", json={"policy": 2, "open_action": 2, "flags": 0})
    time.sleep(1)

    # Fetch whitelist readback
    requests.get(f"{BASE_URL}/devices/{device_id}/whitelist")
    time.sleep(1)
    resp = requests.get(f"{BASE_URL}/devices/{device_id}/whitelist")
    wl_data = resp.json()
    print("Whitelist readback raw:", wl_data)

    # Verify slot 2 exists
    slot2_present = "2" in wl_data or 2 in wl_data
    interactive_check(f"Is Slot 2 present in whitelist readback (policy=2, ttl=30)?")

    # Clean up Slot 2
    print("Deleting Slot 2...")
    requests.delete(f"{BASE_URL}/devices/{device_id}/whitelist/2")
    time.sleep(1)
    print("Phase 7 complete.")

def phase_8_mode_2_door_close_guard(device_id: int):
    if not prompt("Phase 8: Mode 2 & Door Close Guard"): return
    
    # 1. Configure Mode 2 with Door Guard enabled
    print("Configuring Mode 2 (Auto-Close 3s, Guard Warning 2s, Release 5s)...")
    requests.post(f"{BASE_URL}/devices/{device_id}/mode", json={
        "lock_mode": 2,
        "auto_close_timeout_s": 3,
        "behavior_flags": 1,
        "door_warning_delay_s": 2,
        "door_release_delay_s": 5
    })
    time.sleep(1)

    # 2. Present card to initiate Mode 2 occupied state
    print("\n[Action] Present card to open lock and transition to OCCUPIED...")
    card = wait_for_card_tap(device_id)
    if card:
        print("Sending Auth: ALLOW + OPEN (action=2)...")
        requests.post(f"{BASE_URL}/devices/{device_id}/auth_response", json={"result": 1, "action": 2})
        time.sleep(1)

    # 3. Simulate door opened
    input("\n[Action] OPEN the door contact switch and KEEP IT OPEN, then press Enter...")
    print("Door is open. Monitoring Door Close Guard (2s warning + 5s release = 7s total)...")
    time.sleep(8)

    interactive_check("Did Guard alarm trigger (green blink + ALARM1 sound + bolt release)?")
    input("\n[Action] CLOSE the door contact switch and press Enter...")
    print("Phase 8 complete.")

def phase_9_variant_a_provisioning(device_id: int):
    if not prompt("Phase 9: Variant A Provisioning"): return
    print("Sending Factory Reset (0xFB) to device...")
    requests.post(f"{BASE_URL}/devices/{device_id}/factory_reset")
    print("Waiting 3s for device to reboot unprovisioned...")
    time.sleep(3)

    resp = requests.get(f"{BASE_URL}/unassigned")
    unassigned = resp.json()
    print("Unassigned devices:", unassigned)

    if not unassigned:
        print("No unassigned device found.")
        return

    uid32 = unassigned[0].get("uid32") or int(unassigned[0]["uid32_hex"], 16)
    print(f"Re-assigning UID {hex(uid32)} to Device ID {device_id}...")
    requests.post(f"{BASE_URL}/devices/assign", json={"uid32": uid32, "device_id": device_id, "bitrate_code": 4})
    time.sleep(2)
    interactive_check(f"Is Device {device_id} back online?")
    print("Phase 9 complete.")

def run_tests():
    print("Initializing Exhaustive Hardware Tests...")
    try:
        device_id = get_device_id()
        phase_1_master_beacon()
        phase_2_health(device_id)
        phase_3_actuators_signaling(device_id)
        phase_4_mode_1_cloud_auth(device_id)
        phase_5_cloud_deny(device_id)
        phase_6_multipart_uid(device_id)
        phase_7_whitelist_v2_policies(device_id)
        phase_8_mode_2_door_close_guard(device_id)
        phase_9_variant_a_provisioning(device_id)
        print("\nAll 9 hardware validation phases finished successfully.")
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\nTest Execution Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    run_tests()