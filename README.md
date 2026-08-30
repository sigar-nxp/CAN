
# PS Locks OIP REST API & Integration SDK

The **PS Locks Open Integration Platform (OIP)** provides a unified REST API and SDK to commission, control, monitor, and manage PS Locks electronic locking systems over Linux SocketCAN.

---

## 1. System Overview & Architecture

The Gateway acts as the central bridge between IP networks and the low-level CAN bus:

```
+-------------------------------------------------------------------------+
|                        Third-Party Applications                         |
|                 (REST Clients, Web Browsers, Cloud Backend)             |
+-------------------------------------------------------------------------+
                                    │ HTTP / JSON
                                    ▼
+-------------------------------------------------------------------------+
|                  PS Locks OIP Gateway (Raspberry Pi)                    |
|                                                                         |
|  +───────────────────────+   +───────────────────────────────────────+  |
|  |  FastAPI REST API     |   |  Responsive Web Dashboard (Live UI)   |  |
|  |  (Swagger UI @ /docs) |   |  (Locker Tiles, Provisioning Banner)  |  |
|  +───────────────────────+   +───────────────────────────────────────+  |
|                                   │                                     |
|  +───────────────────────────────────────────────────────────────────+  |
|  |  Background Workers (CANListener Daemon & Master Beacon Heartbeat)|  |
|  +───────────────────────────────────────────────────────────────────+  |
|                                   │                                     |
|  +───────────────────────────────────────────────────────────────────+  |
|  |  Linux SocketCAN Abstraction Layer (`can0`)                       |  |
|  +───────────────────────────────────────────────────────────────────+  |
+-------------------------------------------------------------------------+
                                    │ CAN 2.0A (11-Bit) @ 50 kbit/s
                                    ▼
+─────────────────────────────────────────────────────────────────────────+
|               PS Locks CAN Bus (Lock Nodes 1..126)                      |
+─────────────────────────────────────────────────────────────────────────+
```

### Key Capabilities
- **Automatic Discovery & Commissioning**: Real-time identification of unassigned locks (`0x7F`) by UID with single-click/single-call ID provisioning.
- **Bi-Directional Lock Control**: Immediate unlock pulse, permanent hold open, and manual motor reset.
- **Whitelist Management**: Slot-based RFID access management (Slot 1 Ephemeral, Slots 2..N Persistent) with custom validity (TTL in days) and local policy overrides.
- **Door Close Guard**: Active security monitoring that triggers local optical/acoustic alarms and safe releases if doors remain open.
- **Full Telemetry & Diagnostics**: Real-time sensing of door position, bolt state, battery/supply voltage (VCC), temperature, error counters, and bus-off recovery metrics.

---

## 2. Core Concepts & Operational Logic

### 1. Actuator Mechanics & Locking Actions
- **Timed Unlock Pulse (`OPEN_LOCK`, 0x01)**: Retracts the bolt, maintains open state for dwell duration (2–3s), and automatically commands the motor to lock.
- **Permanent Open (`OPEN_HOLD`, 0x04)**: Retracts the bolt and keeps the locking mechanism permanently open (e.g. for maintenance, daytime open-door mode, or emergency release).
- **Lock Shut / Relock (`OPEN_RESET`, 0x05)**: Cancels any active hold-open state immediately and drives the motor to bolt the lock.

### 2. Lock Operating Modes (Mode 1 vs Mode 2)
- **Mode 1 (Locker / Free-Choice Mode)**: Lock remains open by default. Presenting a card claims the locker (locks bolt, stores UID in Slot 1 ephemeral credential storage). Presenting the identical card unlocks the bolt and clears ownership.
- **Mode 2 (Auto-Close / Access Door Mode)**: Lock remains locked by default. Presenting an authorized credential retracts the bolt for a timed window (`auto_close_timeout_s`, default 3s) and automatically re-locks once the door contact closes.

### 3. Door Close Guard Security System
- Solves motor wear and open-door security breaches when doors are left ajar.
- **Warning Phase (`door_warning_delay_s`)**: When door contact remains open beyond threshold, triggers optical green flash and ALARM1 sound pattern.
- **Safe-Release Phase (`door_release_delay_s`)**: If door stays open after warning, device executes safe bolt release, resets occupancy, wipes Slot 1 guest credential, and flags `0x0D: DOOR_GUARD_TIMEOUT`.

### 4. Optical & Acoustic Signaling Subsystem
- **LED Patterns (Modes 0–5)**:
  - `0`: OFF
  - `1`: Solid Green (success / normal unlock)
  - `2`: Solid Red (error / cloud deny)
  - `3`: Blinking Green (customizable period and duty cycle via CAN parameters)
  - `4`: Blinking Red
  - `5`: Fast Blinking Green
  - `TTL`: Configurable duration in seconds (0 = persistent until next command).
- **Buzzer Patterns (Sounds 1–5)**:
  - `1`: OK Tone (short positive chirp)
  - `2`: NOT_OK Tone (two medium reject beeps)
  - `3`: ERROR Tone (continuous failure tone)
  - `4`: ALARM1 Pattern (intermittent Door Close Guard warning sequence)
  - `5`: ALARM2 Pattern (continuous emergency acoustic alarm)

### 5. Tiered Whitelist Architecture & Execution Policies
- **Slot 1 (Ephemeral / Guest Credential)**: Dynamically bound upon claiming a locker; atomically cleared on unlock, master override, or guard timeout.
- **Slots 2..N (Persistent / Staff / Master Keys)**: Stored in non-volatile EEPROM with configurable validity (`ttl_days`).
- **Execution Policies**:
  - `NORMAL (0)`: Default unlock according to active lock mode.
  - `OPEN_KEEP_OCCUPANCY (1)`: Timed pulse unlock without clearing current guest reservation (for inspection/audit).
  - `OPEN_AND_RELEASE (2)`: Master override; permanently unlocks bolt, atomically wipes Slot 1 guest key, and resets locker occupancy.

### 6. Zero-Config Commissioning (Variant A Discovery)
- Unprovisioned devices boot onto the bus with default ID `0x7F` emitting `0x57F ID_REQUEST` with their 32-bit hardware UID.
- The Gateway automatically tracks unassigned nodes in memory (`/unassigned`) and provisions permanent Device IDs (1..126) and bitrates via atomic command.

### 7. Multi-Frame RFID UID Assembly
- Supports standard 4-byte UIDs as well as 7-byte / 8-byte extended credentials (e.g. Mifare DESFire). Split CAN frames (`UID_PART1` / `UID_PART2`) are reassembled transparently by the gateway.

### 8. Master Beacon & Bus Synchronization (`0xB0`)
- Global 1 Hz broadcast providing real-time bus timing, brown-out detection, master failover signaling, and node synchronization.

---

## 3. Physical Layer & CAN Bus Configuration

The communication strictly follows the **PS Locks OIP CAN Protocol** (Standard 11-Bit Identifier, CAN 2.0A).

### Physical Requirements
- **Bit Rate (Production)**: **50 kbit/s**, Sample Point 87.5% (100 kbit/s supported for lab environments).
- **Bus Termination**: Exactly two 120-Ohm termination resistors at the physical ends of the bus (total line resistance: 56–60 Ohm). Lock nodes do not contain onboard termination.
- **Topology**: Linear trunk with short stubs (target <= 1.5 m, max 2.0 m).
- **Sensors**: Door contact debounced for 500 ms in firmware.

### SocketCAN Initialization
Initialize the CAN interface on the host Raspberry Pi with an increased transmit queue length to avoid transmit buffer overflows:
```bash
sudo ip link set can0 down
sudo ip link set can0 txqueuelen 1000
sudo ip link set can0 up type can bitrate 50000
```

Verify the interface status:

```bash
ip link show can0
# Expected: can0: <UP,LOWER_UP,ECHO>

```

---

## 4. Installation & Quick Start

### Prerequisites
- Raspberry Pi 4 / 5 or Linux host with Python 3.11+.
- Active SocketCAN interface (`can0`).

### Setup
```bash
# 1. Clone repository & enter directory
git clone git@github.com:sigar-nxp/CAN.git
cd CAN

# 2. Set up virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start the Gateway service
uvicorn app:app --host 0.0.0.0 --port 8000
```

### Accessing Interfaces

* **Web Dashboard (Commissioning & Testing)**: `http://<ip-or-hostname>:8000/`
* **Interactive OpenAPI Documentation (REST Integration)**: `http://<ip-or-hostname>:8000/docs`

---

## 5. REST API Documentation

### 5.1 System & Gateway Heartbeat

#### `GET /api/system/beacon`

Queries the status of the Master Beacon heartbeat service.

```json
{
  "running": true,
  "interval_sec": 1.0,
  "total_beacons_sent": 1420
}
```

#### `POST /api/system/beacon/toggle`

Toggles the Master Beacon background service on or off.

* **Body**: `{"enable": true}`


---

### 5.2 Device Discovery & Provisioning

#### `GET /devices`

Lists all known and assigned devices actively tracked on the CAN bus.

```json
[
  {
    "device_id": 1,
    "status": "LOCKED",
    "online": true
  }
]
```

#### `GET /unassigned`

Lists all unprovisioned locks currently on the bus emitting `0x57F ID_REQUEST`.

```json
[
  {
    "uid32_hex": "28005800",
    "uid32": 671111168
  }
]
```

#### `POST /devices/assign`

Assigns a permanent Device ID (1..126) and bit rate code to an unprovisioned lock via `ASSIGN_ID (0x01)`.

* **Body**:

```json
{
  "uid32": 671111168,
  "device_id": 1,
  "bitrate_code": 4
}
```

(Bitrate codes: `0=1k`, `1=5k`, `2=10k`, `3=25k`, `4=50k (Default)`, `5=100k`, `6=250k`, `7=500k`)

---

### 5.3 Lock Control & Actuators

#### `POST /devices/{id}/open`

Sends a timed unlocking pulse (`OPEN_LOCK`, `0x01`). The bolt retracts, stays open for the dwell duration (default 2–3s), and relocks automatically.

#### `POST /devices/{id}/open_hold`

Retracts the bolt and keeps the lock permanently open (`OPEN_HOLD`, `0x04`).

#### `POST /devices/{id}/reset`

Cancels permanent hold and commands the motor to lock (`OPEN_RESET`, `0x05`).

#### `POST /devices/{id}/buzzer`

Plays an acoustic pattern on the device buzzer.

* **Body**:

```json
{
  "sound": 1,
  "repeat": 2
}
```

*Sounds:* `1` = OK, `2` = NOT_OK, `3` = ERROR, `4` = ALARM1 (Door Guard warning), `5` = ALARM2 (Continuous).

#### `POST /devices/{id}/led`

Sets an optical override pattern on the lock's LED.

* **Body**:

```json
{
  "mode": 3,
  "period10ms": 100,
  "duty": 50,
  "ttl": 4
}
```

*Modes:* `0` = OFF, `1` = Green Solid, `2` = Red Solid, `3` = Green Blink (1s/1s), `4` = Red Blink, `5` = Green Fast Blink. `ttl=0` applies indefinitely until reset.

---

### 5.4 Cloud Authentication & RFID Responses

#### `POST /devices/{id}/auth_response`

Sends the gateway/cloud authorization decision following an `RFID_AUTH_REQ` (0x11).

* **Body**:

```json
{
  "result": 1,
  "action": 1
}
```

Field Definitions:

* `result`: `0` = DENY (triggers local red LED + reject tone), `1` = ALLOW.


* `action`: `0` = NONE, `1` = CLOSE (Locker Mode 1: locks bolt & creates Slot 1 ephemeral credential), `2` = OPEN / OPEN_HOLD.



---

### 5.5 Operating Modes & Door Close Guard (v0.4r4)

#### `POST /devices/{id}/mode`

Configures base operating mode, auto-close timers, and Door Close Guard security parameters.

* **Body**:

```json
{
  "lock_mode": 2,
  "auto_close_timeout_s": 3,
  "behavior_flags": 1,
  "door_warning_delay_s": 2,
  "door_release_delay_s": 5
}
```

Field Definitions:

* `lock_mode`: `1` = FIT Standard / Locker (Default OPEN), `2` = Auto-Close / Door Guard (Default CLOSED).


* `behavior_flags`: `1` (Bit 0) activates Door Close Guard.


* `door_warning_delay_s`: Time door may remain open before ALARM1 starts (default: 2s).


* `door_release_delay_s`: Duration of warning alarm before bolt safe-release & Slot 1 wipe (default: 5s).



#### `GET /devices/{id}/mode`

Returns the currently stored lock mode and door guard configuration.

---

### 5.6 Whitelist Management (V2)

Slot 1 is reserved for dynamic/ephemeral cards (guest users), while Slots 2..N store permanent credentials.

#### `GET /devices/{id}/whitelist`

Queries active whitelist slots from the lock.

```json
{
  "2": {
    "slot_index": 2,
    "entry_type": 2,
    "uid_hex": "0A0B0C0D",
    "uid_len": 4,
    "policy": 2,
    "open_action": 2,
    "ttl_days": 30
  }
}
```

#### `POST /devices/{id}/whitelist`

Writes or replaces a specific whitelist slot (Slots 2..N for persistent credentials).

* **Body**:

```json
{
  "slot_index": 2,
  "uid_hex": "0A0B0C0D",
  "ttl_days": 30
}
```

#### `DELETE /devices/{id}/whitelist/{slot_index}`

Deletes a specific slot. Deleting Slot 1 clears the active guest without touching master keys.

#### `DELETE /devices/{id}/whitelist`

Performs `WL_CLEAR (0x32)`: Wipes all slots, clears runtime occupancy, and stops any active Door Close Guard.

#### `POST /devices/{id}/whitelist/{slot_index}/policy`

Configures local execution policy for a persistent slot.

* **Body**:

```json
{
  "policy": 2,
  "open_action": 2,
  "flags": 0
}
```

Profiles:

* `policy=0, open_action=0`: **NORMAL** (Standard mode-dependent behavior).


* `policy=1, open_action=1`: **OPEN_KEEP_OCCUPANCY** (Pulse open, keep occupied).


* `policy=2, open_action=2`: **OPEN_AND_RELEASE** (Permanent hold open, atomically wipe Slot 1 and clear occupancy).



---

### 5.7 Telemetry & Diagnostics

#### `GET /devices/{id}`

Returns live status, door sensors, and the last detected RFID UID.

```json
{
  "is_locked": true,
  "is_door_closed": true,
  "status_text": "LOCKED",
  "error_text": "NONE",
  "last_scanned_card": "044426c2ff7180",
  "door_close_guard_supported": true
}
```

#### `GET /devices/{id}/version`

Returns firmware and hardware version registers (P3 Telemetry).

```json
{
  "firmware_version": "0.14.1",
  "hardware_version": "Type:1 Rev:1"
}

```

#### `GET /devices/{id}/health`

Reads structured diagnostics P1 through P8 from the controller.

```json
{
  "health_short": { "vcc_mv": 2989, "temp_c": 30 },
  "health_extended": { "free_ram": 15048, "uptime_s": 128 },
  "health_diag_info": { "reset_reason": 3, "brownout_cnt": 0, "watchdog_cnt": 0, "can_tx_err": 0, "can_rx_err": 0 },
  "runtime_state": { "lock_id": 1, "physical_state": 1, "lock_error": 0, "occupancy_flags": 0, "led_status": 0, "led_remaining_s": 0, "state_revision": 1 },
  "diag_extended": { "lock_id": 1, "bus_off_counter": 0, "last_recovery_stage": 0, "diag_flags": 3 }
}
```

#### `GET /devices/{id}/occupancy`

Queries runtime occupancy state independent of whitelist entries.

#### `POST /devices/{id}/device_reset`

Performs a warm reboot of the lock node controller.

#### `POST /devices/{id}/factory_reset`

Reverts the lock to unprovisioned state (`0x7F`), resetting Device ID and flash memory.


---

## 6. CAN Protocol Reference (Standard 11-Bit)

### 6.1 11-Bit CAN Identifier Layout

Formula: `CAN_ID = Base_ID | Device_ID` (Default / Unprovisioned ID = `0x7F`).

| Type | Base ID | Direction | Description |
| --- | --- | --- | --- |
| **COMMAND** | `0x100` | Gateway -> Device | Master command payload |
| **STATUS_REPORT** | `0x200` | Device -> Gateway | State events, ACKs, RFID triggers |
| **HEALTH_REPORT** | `0x300` | Device -> Gateway | Telemetry, version, diagnostics |
| **MASTER_RESPONSE** | `0x400` | Gateway -> Device | Commissioning assign, Auth Tag, Beacon |
| **ID_REQUEST** | `0x500` | Device -> Gateway | Unprovisioned boot broadcast |
| **UID_PART1** | `0x600` | Device -> Gateway | Hardware UID Bytes 0..7 |
| **UID_PART2** | `0x700` | Device -> Gateway | Hardware UID Bytes 8..11 |

---

### 6.2 Diagnostic Health Payloads (0x300 | devId)

| Payload Code | Name | Description & Key Fields |
| --- | --- | --- |
| `0x01` | **P1 Basic Health** | `[1..2]` VCC mV (BE), `[3]` Chip Temperature °C |
| `0x02` | **P2 Extended Health** | `[1..2]` Free RAM Bytes, `[3..6]` System Uptime in seconds |
| `0x03` | **P3 Version Info** | `[1]` Major, `[2]` Minor, `[3]` Patch, `[4]` Flags, `[5]` HW Rev |
| `0x04` | **P4 Device Info** | `[1..2]` Mfg ID, `[3]` Dev Type, `[4]` HW Rev, `[5..7]` Cap Flags (24-bit BE) |
| `0x05` | **P5 Diag Info** | `[1]` Reset Reason, `[2]` Brown-out Cnt, `[3]` Watchdog Cnt, `[4..5]` CAN TX/RX Errors |
| `0x07` | **P7 Runtime State** | `[2]` Physical State, `[3]` Lock Error, `[4]` Occ Flags, `[5..6]` LED Status/TTL, `[7]` State Revision |
| `0x08` | **P8 Diag Extended** | `[2]` Bus-Off Counter, `[3]` Last Recovery Stage (1.5s / 5s / 15s / 60s), `[4]` Diag Flags |

---

### 6.3 Normative Error Codes (`STATUS_ERROR 0xFF`)

| Error Code | Identifier | Description |
| --- | --- | --- |
| `0x01` | `INVALID_LENGTH` | Payload length does not match command specification |
| `0x02` | `INVALID_PARAM` | Parameter out of allowable range or invalid combination |
| `0x03` | `UNSUPPORTED_CMD` | Command byte not implemented by firmware |
| `0x04` | `BUSY` | Lock actuator/guard actively running; command rejected |
| `0x05` | `NOT_FOUND` | Queried item or record does not exist |
| `0x06` | `SLOT_OUT_OF_RANGE` | Whitelist slot exceeds device capacity (valid: `1..MAX`) |
| `0x07` | `UID_MISMATCH` | UID provided does not match addressed target |
| `0x08` | `AUTH_TIMEOUT` | Cloud RFID authentication exceeded 5.0s timeout window |
| `0x09` | `AUTH_DENY` | Cloud backend denied access for presented RFID card |
| `0x0A` | `SECURE_REQUIRED` | Command requires active authenticated security mode |
| `0x0B` | `SECURE_TAG_INVALID` | Authentication tag verification failed |
| `0x0C` | `MODE_CONFLICT` | Requested operation conflicts with configured lock mode |
| `0x0D` | `DOOR_GUARD_TIMEOUT` | Door Close Guard timed out; door remained open after alarm |

---

### 6.4 Physical & Decoded Lock States

| State Code | Identifier | Bolt State | Door Contact Sensor |
| --- | --- | --- | --- |
| `0x01` | `LC_DC` | Locked (Bolted) | Closed |
| `0x02` | `LO_DC` | Unlocked (Retracted) | Closed |
| `0x03` | `LC_DO` | Locked (Bolted) | Open |
| `0x04` | `LO_DO` | Unlocked (Retracted) | Open |
| `0x05` | `OPEN_PULSE` | Actuator moving (Timed unlock pulse) | Dynamic |
| `0x06` | `OPEN_HOLD_DO` | Permanently Unlocked | Open |
| `0x07` | `OPEN_HOLD_DC` | Permanently Unlocked | Closed |

---

## 7. Python Integration Example

Below is a complete script demonstrating how to discover an unassigned lock, commission it, write a persistent RFID card to its whitelist, and monitor door status:

```python
import time
import requests

GATEWAY_URL = "http://localhost:8000/api/v1/"

def main():
    # 1. Check Gateway Heartbeat
    beacon = requests.get(f"{GATEWAY_URL}/system/beacon").json()
    print(f"Gateway Master Beacon Running: {beacon['running']} (Total sent: {beacon['total_beacons_sent']})")

    # 2. Discover Unassigned Devices
    print("Scanning for unprovisioned locks...")
    unassigned = requests.get(f"{GATEWAY_URL}/unassigned").json()
    
    if not unassigned:
        print("No unassigned locks found. Assuming Device ID 1 is already provisioned.")
        device_id = 1
    else:
        target_uid = unassigned[0]["uid32"]
        print(f"Found unassigned lock with UID: {target_uid}. Provisioning as Device ID 1...")
        assign_resp = requests.post(
            f"{GATEWAY_URL}/devices/assign",
            json={"uid32": target_uid, "device_id": 1, "bitrate_code": 4} # 4 = 50 kbit/s
        )
        if assign_resp.status_code == 200:
            print("Successfully provisioned Device ID 1!")
            device_id = 1
        else:
            print(f"Provisioning failed: {assign_resp.text}")
            return

    # 3. Configure Lock Mode (Auto-Close with Door Close Guard)
    print("Configuring Lock Mode 2 with Door Close Guard...")
    mode_payload = {
        "lock_mode": 2,
        "auto_close_timeout_s": 3,
        "behavior_flags": 1,      # Bit 0 = Door Close Guard active
        "door_warning_delay_s": 2, # Warn after 2s open
        "door_release_delay_s": 5  # Safe release after 5s alarm
    }
    requests.post(f"{GATEWAY_URL}/devices/{device_id}/mode", json=mode_payload)

    # 4. Add Permanent Card to Whitelist (Slot 2, 30 Days TTL)
    card_uid = "04A1B2C3"
    print(f"Writing card {card_uid} to Whitelist Slot 2...")
    wl_resp = requests.post(
        f"{GATEWAY_URL}/devices/{device_id}/whitelist",
        json={"slot_index": 2, "uid_hex": card_uid, "ttl_days": 30}
    )
    print(f"Whitelist Write Status: {wl_resp.status_code}")

    # 5. Trigger Opening Pulse and Monitor State
    print("Triggering opening pulse...")
    requests.post(f"{GATEWAY_URL}/devices/{device_id}/open")

    for i in range(5):
        time.sleep(1)
        state = requests.get(f"{GATEWAY_URL}/devices/{device_id}").json()
        print(f"[{i+1}s] Status: {state['status_text']} | Locked: {state['is_locked']} | Door Closed: {state['is_door_closed']}")

if __name__ == "__main__":
    main()
```

---

## 8. Testing

### Hardware-Independent Unit Tests
The project includes a comprehensive, hardware-independent test suite covering CAN opcodes, parser logic, and REST API endpoints (using mock interfaces). To run the suite locally:
```bash
pytest tests/ -v

```

### Hardware & Integration Testing

An interactive test suite is included to verify CAN communication, RFID flows, door sensors, and actuator timings against connected hardware:

```bash
python3 tests/exhaustive_hardware_test.py

```

---

## 9. License & Support

Developed for **PS GmbH** (Melisau 1255, 6863 Egg / Austria).

For integration support, contact [info@pslocks.com](mailto:info@pslocks.com).