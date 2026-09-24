
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
|  |  Background Workers (CANListener, Beacon, Asynchronous Log Worker)|  |
|  +───────────────────────────────────────────────────────────────────+  |
|                                   │                                     |
|  +───────────────────────────────────────────────────────────────────+  |
|  |  Storage Engine (SQLite: Device Config, Custom Names, Audit Logs) |  |
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
- **Data Persistence & Custom Aliases**: Non-volatile storage of device configuration, operational modes, and user-assigned names (e.g., "Locker 12") via SQLite.
- **Asynchronous Audit Logging**: Non-blocking database tracking of access decisions, card scans, state changes, and hardware errors with real-time SSE event dispatching.  

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

### Production Deployment (Systemd Services)

For 24/7 production operation on a Raspberry Pi or dedicated Linux host, systemd service units automate CAN interface configuration and ensure the FastAPI gateway automatically starts on system boot and restarts upon failure.

The deployment configuration assets are located in the `deploy/` directory:
- `deploy/can0.service`: A systemd oneshot service that brings up SocketCAN `can0` cleanly on boot at 50 kbit/s with `txqueuelen 1000`.
- `deploy/pslocks-gateway.service`: A systemd service running the FastAPI application as the `admin` user with automatic restart policies and dependency on `can0.service`.
- `deploy/install_services.sh`: An automated deployment script that validates root privileges, installs the units into `/etc/systemd/system/`, reloads systemd, enables autostart on boot, and starts both services immediately.

#### 1. Automated Installation & Enabling

Install and enable the systemd services using the deployment script:

```bash
sudo ./deploy/install_services.sh
```

The installer performs the following actions:
1. Validates root/sudo execution.
2. Copies `can0.service` and `pslocks-gateway.service` to `/etc/systemd/system/`.
3. Runs `systemctl daemon-reload` to register the new units.
4. Enables and starts both services via `systemctl enable --now can0.service pslocks-gateway.service`.
5. Displays real-time status feedback using `systemctl is-active`.

#### 2. Monitoring Service Logs

Inspect and stream service logs in real time via `journalctl`:

```bash
# Follow FastAPI gateway logs in real time
journalctl -u pslocks-gateway.service -f

# View CAN interface initialization logs
journalctl -u can0.service --no-pager
```

#### 3. Manual Service Management via systemctl

You can manually inspect, start, stop, or restart the services at any time:

```bash
# Check service status
systemctl status can0.service pslocks-gateway.service

# Verify if services are actively running
systemctl is-active can0.service pslocks-gateway.service

# Restart the FastAPI gateway
sudo systemctl restart pslocks-gateway.service

# Stop the services
sudo systemctl stop pslocks-gateway.service
sudo systemctl stop can0.service

# Start the services manually
sudo systemctl start can0.service
sudo systemctl start pslocks-gateway.service
```

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

#### `PATCH /devices/{id}`

Updates local lock settings such as the custom display name.

* **Body**:

```json
{
  "name": "Locker 12"
}
```
* **Response**:
```json
{
  "status": "success",
  "device_id": 1,
  "name": "Locker 12"
}
```
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

### 5.8 Activity & Audit Logging

The platform logs critical physical, authentication, configuration, and firmware events to an internal SQLite database (`pslocks.db`) using an asynchronous worker queue. This guarantees that database writes and disk I/O never block time-critical CAN bus transmissions or firmware flashing loops.

#### Audit Log Event Lifecycle & Schema

Every access decision, actuation pulse, whitelist change, and OTA firmware stage is tracked with structured metadata:

| Event Type | Category | Description | Details Payload / Context |
| --- | --- | --- | --- |
| `OTA_START` | Firmware | Asynchronous OTA flashing sequence initiated | JSON: `current_slot`, `target_slot`, `version`, `binary_name` |
| `OTA_SUCCESS` | Firmware | Firmware flashed, CRC32 verified, and bank activated | JSON: `active_slot`, `crc32`, `binary_name`, `execution_time`, `duration_s` |
| `OTA_FAILED` | Firmware | Flashing interrupted by handshake, streaming, or CRC error | JSON: `stage`, `error`, `binary_name`, `execution_time` |
| `WHITELIST_MUTATION` | Access Policy | Whitelist credential write, deletion, clear, or policy update | String: e.g., `Slot 2 written, TTL 15d`, `Slot 2 deleted`, `Whitelist cleared` |
| `MANUAL_OPEN` | Actuation | Remote pulse open, hold open, or reset release commanded | String: e.g., `Pulse open commanded`, `Hold open commanded`, `Lock reset commanded` |
| `RFID_SCAN` | Access | Unregistered or registered RFID card detected at reader | Captured `card_uid` hex string |
| `ACCESS_GRANTED` | Access | Local whitelist match (Usecase 2) or cloud-approved entry | `card_uid` hex string and authorization reason |
| `ACCESS_DENIED` | Access | Whitelist miss or cloud-rejected entry | `card_uid` hex string and denial reason |
| `LOCK_STATUS` | Telemetry | Physical bolt or door contact sensor transition | Transition string (e.g., `State changed to LOCKED`) |

#### Non-Blocking Logging Architecture

```
[CAN Bus Listener / OTA Service]
               │
               ▼ (non-blocking call: can_listener.add_log() / _safe_record_audit_log())
      [In-Memory Log Queue]
               │
               ▼ (Asynchronous Background Log Worker)
      [SQLite pslocks.db] ──► [Server-Sent Events: /api/v1/stream (log_entry)]
```

Logging failures (such as disk full or locked database) are safely caught and logged without raising exceptions to callers (`_safe_record_audit_log`), ensuring CAN bus transactions and physical lock actuation proceed uninterrupted.

#### Visual Representation in the Web Dashboard (Web UI)

The Web Dashboard features a real-time **Activity & Audit Log** table populated automatically via Server-Sent Events (`/api/v1/stream`):
- **Live Event Badges**: Visual color-coded tags allow instant identification of event types:
  - `OTA_START`: Bold purple badge with border (`bg-purple-100 text-purple-800 border-purple-300 font-bold`).
  - `OTA_SUCCESS`: Bold blue badge with border (`bg-blue-100 text-blue-800 border-blue-300 font-bold`).
  - `OTA_FAILED`: Bold red badge with border (`bg-red-100 text-red-800 border-red-300 font-bold`).
  - `MANUAL_OPEN`: Purple badge (`bg-purple-100 text-purple-800`).
  - `ACCESS_GRANTED`: Green badge (`bg-green-100 text-green-800`).
  - `ACCESS_DENIED` / `ERROR` / `ALARM`: Red badge (`bg-red-100 text-red-800`).
  - `RFID_SCAN`: Blue badge (`bg-blue-100 text-blue-800`).
  - `WHITELIST_MUTATION`: Clean neutral badge (`bg-gray-100 text-gray-800`).
- **Structured JSON Parsing**: Complex metadata (e.g., OTA duration, target slot, CRC32) is automatically parsed client-side and rendered into pipe-separated key-value summaries (`key: val | key: val`), with the complete unescaped JSON string available via HTML hover tooltips (`title` attribute).

#### `GET /api/v1/logs`

Retrieves a paginated list of recorded audit log events in reverse-chronological order.

* **Query Parameters**:
  * `limit` (int, default: 50): Maximum number of records to return.
  * `device_id` (int, optional): Filter logs for a specific lock node.

* **Response**:

```json
[
  {
    "id": 14,
    "timestamp": "2026-09-24T21:05:12.180200Z",
    "device_id": 1,
    "device_name": "Locker 1",
    "event_type": "OTA_SUCCESS",
    "card_uid": null,
    "details": "{\"active_slot\": 1, \"crc32\": \"0x63E4A0E1\", \"binary_name\": \"slot_b.bin\", \"execution_time\": \"1.42s\", \"duration_s\": 1.42}"
  },
  {
    "id": 13,
    "timestamp": "2026-09-24T21:05:10.750100Z",
    "device_id": 1,
    "device_name": "Locker 1",
    "event_type": "OTA_START",
    "card_uid": null,
    "details": "{\"current_slot\": 0, \"target_slot\": 1, \"version\": \"1.0.0\", \"binary_name\": \"slot_b.bin\"}"
  },
  {
    "id": 12,
    "timestamp": "2026-09-24T20:37:47.617179Z",
    "device_id": 1,
    "device_name": "Locker 1",
    "event_type": "WHITELIST_MUTATION",
    "card_uid": "04A1B2C3",
    "details": "Slot 2 written, TTL 30d"
  }
]
```


### 5.9 Real-Time Event Streaming (SSE)

#### `GET /api/v1/stream`

Server-Sent Events endpoint streaming real-time status frames and live audit events to connected frontends or client applications.

* **Events**:
  * `device_state`: Dispatched on hardware telemetry and status changes.
  * `log_entry`: Dispatched immediately when a new audit event is stored.




### 5.10 Firmware Management & Over-The-Air (OTA) Updates

The gateway implements a unified single-package release architecture for STM32C092 dual-bank bootloaders. Users upload a single `.ota` container, and the gateway autonomously manages staging, binary verification, slot targeting, and flashing without requiring manual slot selection.

#### 1. Unified Single-File Package Format (`.ota`)

Release packages (`pslocks_firmware_vX.Y.Z.ota`) are standard ZIP archives produced by `tools/package_firmware.py` (or `make package_ota`):

```
pslocks_firmware_v1.0.0.ota (ZIP Container)
├── manifest.json       (Release metadata, target MCU, sizes, IEEE 802.3 CRC32 checksums)
├── slot_a.bin          (Binary linked for Flash Bank A: 0x08004000, 112 KB limit)
└── slot_b.bin          (Binary linked for Flash Bank B: 0x08020000, 112 KB limit)
```

**Manifest Schema (`manifest.json`):**
```json
{
  "version": "1.0.0",
  "timestamp": "2026-09-21T20:19:47Z",
  "target_mcu": "STM32C092",
  "binaries": {
    "slot_a": {
      "filename": "slot_a.bin",
      "crc32": "0x63E4A0E1",
      "crc32_int": 1675927777,
      "size": 71520
    },
    "slot_b": {
      "filename": "slot_b.bin",
      "crc32": "0xEF071C1A",
      "crc32_int": 4010220570,
      "size": 71520
    }
  }
}
```

#### 2. Autonomous Dual-Slot Resolution & Synchronization

The gateway dynamically resolves which slot binary to flash based on live telemetry directly reported by the lock hardware:
- **Lock running on Slot A (`active_slot: 0`)**: Autonomously flashes `slot_b.bin` into Bank B (`0x08020000`).
- **Lock running on Slot B (`active_slot: 1`)**: Autonomously flashes `slot_a.bin` into Bank A (`0x08004000`).

**Hardware-Driven Bank State Synchronization:**
- Microcontroller firmware evaluates its Vector Table Offset Register `(SCB->VTOR >= 0x08020000) ? 1 : 0` to determine live execution bank with 100% hardware certainty.
- Real-time broadcast: Emitted in every `STATUS_BASIC` frame (`0x200 | devId`, Byte 4: `active_slot`) and `HEALTH_P3` version frame (`0x300 | devId`, Byte 6: `active_slot`).
- Bootloader Handshake Verification: On `CMD_OTA_START` handshake, the bootloader transmits `ACK_OTA_START` with Byte 2 containing its active slot. The gateway verifies and dynamically re-aligns the binary before streaming chunk 0, completely preventing bank inversion.

#### 3. Dual-Bank Ping-Pong Lifecycle & Safety Interlock

The STM32C092 dual-bank flash architecture provides seamless, zero-downtime over-the-air updates through an alternating ping-pong execution scheme:

```
+─────────────────────────────────────────────────────────────────────────────+
|                          Flash Memory Map (256 KB)                          |
+───────────────────────────────────┬─────────────────────────────────────────+
| Bank A: 0x08004000 (112 KB)       | Bank B: 0x08020000 (112 KB)             |
| [Slot A - Active Application]     | [Slot B - Target Inactive Bank]         |
+───────────────────────────────────┴─────────────────────────────────────────+
                                    │
                  1. Handshake: ACK_OTA_START reports active_slot=0
                  2. Erase: Bootloader erases target Bank B (56 pages)
                  3. Stream: 5-byte chunks paced @ ~6 ms delay
                  4. Verify: Hardware IEEE 802.3 CRC32 calculation
                  5. Activate: Atomic swap in Metadata Page 120 (0x0803C000)
                  6. Reboot: MCU executes reset into Bank B (active_slot=1)
                  7. Confirm: Gateway verifies online & emits BUZZ_OK chirp
                                    │
                                    ▼
+───────────────────────────────────┬─────────────────────────────────────────+
| Bank A: 0x08004000 (112 KB)       | Bank B: 0x08020000 (112 KB)             |
| [Slot A - Standby Fallback Bank]  | [Slot B - Active Updated Application]   |
+───────────────────────────────────┴─────────────────────────────────────────+
```

- **Zero-Brick Invariant**: Because firmware is written to the currently unexecuted bank, incomplete transfers or power losses during flashing leave the active bank completely intact.
- **Safety Interlock (HTTP 503)**: Any incoming operational lock control requests (such as `POST /api/v1/devices/{id}/open`, `POST /devices/{id}/hold`, or `POST /devices/{id}/release`) are strictly blocked with `HTTP 503 Service Unavailable` while an OTA update is actively in progress. This prevents motor actuator movements or electrical voltage dips during flash writes.
- **Audible Post-Flash Confirmation**: Following successful activation and MCU reset, the gateway verifies that the lock re-attaches to the CAN bus in the updated slot and triggers an audible confirmation chirp (`BUZZ_OK`).

#### `POST /api/v1/ota/upload`

Uploads and validates a `.ota` release bundle or stages a raw `.bin` file.

- **Request**: Multipart form data with `file` field (`pslocks_firmware_vX.Y.Z.ota`).
- **Validation**: Verifies ZIP integrity, parses `manifest.json`, validates `target_mcu`, and checks IEEE 802.3 CRC32 checksums of both binaries. Rejects mismatched bundles with HTTP 400.
- **Storage**: Unpacks into versioned directory `uploads/releases/vX.Y.Z/` and updates `uploads/releases/latest_release.json`.
- **Response**:
```json
{
  "status": "staged",
  "type": "bundle",
  "version": "v1.0.0",
  "target_mcu": "STM32C092",
  "release_dir": "/home/admin/PSLOCKS_OIP/uploads/releases/v1.0.0",
  "filename": "pslocks_firmware_v1.0.0.ota",
  "slot_a": { "path": ".../slot_a.bin", "size": 71520, "crc32": "0x63E4A0E1" },
  "slot_b": { "path": ".../slot_b.bin", "size": 71520, "crc32": "0xEF071C1A" }
}
```

#### `GET /api/v1/ota/release`

Retrieves metadata of the active staged firmware release bundle.

```json
{
  "has_release": true,
  "version": "v1.0.0",
  "raw_version": "1.0.0",
  "target_mcu": "STM32C092",
  "timestamp": "2026-09-21T20:19:47Z",
  "release_dir": ".../uploads/releases/v1.0.0",
  "binaries": {
    "slot_a": { "filename": "slot_a.bin", "path": ".../slot_a.bin", "crc32": "0x63E4A0E1", "size": 71520 },
    "slot_b": { "filename": "slot_b.bin", "path": ".../slot_b.bin", "crc32": "0xEF071C1A", "size": 71520 }
  }
}
```

#### `POST /api/v1/devices/{id}/ota/start`

Initiates an asynchronous background OTA update for a target lock node.

- **Body**: `{"binary_path": "auto"}` (or empty `{}`). The gateway autonomously resolves the target bank binary.
- **Response**:
```json
{
  "status": "started",
  "task_id": "ota_task_1",
  "device_id": 1,
  "binary_path": "/home/admin/PSLOCKS_OIP/uploads/releases/v1.0.0/slot_b.bin"
}
```

#### `GET /api/v1/devices/{id}/ota/status`

Polls real-time OTA progress, operational states, and slot metadata for a target lock node.

```json
{
  "device_id": 1,
  "progress": 65,
  "state": "FLASH",
  "error": null,
  "active_slot": 0,
  "target_slot": 1,
  "target_binary": "slot_b.bin"
}
```
*States*: `IDLE`, `QUEUED`, `ERASE`, `FLASH`, `VERIFY`, `COMPLETE`, `ERROR`.

#### `POST /api/v1/devices/{id}/ota/reset`

Resets the in-memory OTA task status back to `IDLE 0%`.

#### `POST /api/v1/ota/batch/start`

Enqueues sequential batch flashing across all online locks or a specified list of node IDs. Flashes one lock at a time to prevent bus congestion.

- **Body**:
```json
{
  "device_ids": [1, 2, 9],
  "binary_path": "auto"
}
```

#### `GET /api/v1/ota/batch/status`

Returns authoritative global batch progress across all queued devices.

```json
{
  "running": true,
  "current_device_id": 1,
  "current_index": 0,
  "total_devices": 3,
  "completed_devices": [],
  "failed_devices": [],
  "device_ids": [1, 2, 9],
  "state": "RUNNING"
}
```

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
| **OTA_COMMAND** | `0x780` | Gateway -> Device | Dual-bank bootloader command (`0x780 \| devId`) |
| **OTA_ACK** | `0x790` | Device -> Gateway | Bootloader flow-control & status ACK (`0x790 \| devId`) |

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


### 6.5 Normative OTA Bootloader Protocol (Dual-Bank STM32C092)

The lock node microcontroller utilizes a dedicated dual-bank bootloader residing in Flash Page 0..7 (`0x08000000`).

#### Memory Mapping (256 KB Flash, 30 KB RAM)

| Region | Address Range | Size / Pages | Description |
| --- | --- | --- | --- |
| **Bootloader** | `0x08000000 - 0x08003FFF` | 16 KB (Pages 0..7) | CAN Bootloader & Fail-Safe Engine |
| **Slot A (Bank A)** | `0x08004000 - 0x0801FFFF` | 112 KB (Pages 8..63) | Primary Application Slot |
| **Slot B (Bank B)** | `0x08020000 - 0x0803BFFF` | 112 KB (Pages 64..119) | Secondary Application Slot |
| **Bootloader Metadata**| `0x0803C000 - 0x0803C7FF` | 2 KB (Page 120) | Active slot, image sizes, CRC32 checksums |
| **Config & Whitelist** | `0x0803C800 - 0x0803FFFF` | 14 KB (Pages 121..127) | Device configuration and local whitelist |

#### Flash Metadata Layout (`BootloaderMetadata_t` @ 0x0803C000)

```c
typedef struct {
    uint32_t magic;              // Validation magic word (0x424F4F54 = 'BOOT')
    uint32_t active_slot;        // Active slot index (0 = Slot A, 1 = Slot B)
    uint32_t slot_a_size;        // Firmware size in Slot A in bytes
    uint32_t slot_a_crc32;       // IEEE 802.3 CRC32 checksum for Slot A
    uint32_t slot_b_size;        // Firmware size in Slot B in bytes
    uint32_t slot_b_crc32;       // IEEE 802.3 CRC32 checksum for Slot B
    uint32_t update_pending;     // Pending activation flag
    uint32_t boot_attempt_count; // Watchdog boot attempt counter
} BootloaderMetadata_t;
```

#### OTA Command & Acknowledgment Opcodes

| Opcode | Identifier | Direction | CAN DLC | Payload Layout |
| --- | --- | --- | --- | --- |
| `0x01` | `CMD_OTA_START` | GW $\rightarrow$ Dev | 8 | `[0]=0x01`, `[1..3]=size` (24-bit LE), `[4..7]=crc32` (32-bit LE) |
| `0x81` | `ACK_OTA_START` | Dev $\rightarrow$ GW | 3 | `[0]=0x81`, `[1]=status` (`0x00`=OK, `0x01`=Err), `[2]=active_slot` (0/1) |
| `0x02` | `CMD_OTA_DATA` | GW $\rightarrow$ Dev | 4..8 | `[0]=0x02`, `[1..2]=chunk_idx` (16-bit LE), `[3..7]=payload` (1..5 bytes) |
| `0x82` | `ACK_OTA_DATA` | Dev $\rightarrow$ GW | 3 | `[0]=0x82`, `[1..2]=next_expected_chunk` (16-bit LE flow control) |
| `0x03` | `CMD_OTA_VERIFY` | GW $\rightarrow$ Dev | 1 | `[0]=0x03` (Triggers target bank CRC32 hardware check) |
| `0x83` | `ACK_OTA_VERIFY` | Dev $\rightarrow$ GW | 2 | `[0]=0x83`, `[1]=status` (`0x00`=CRC Valid, `0x01`=Mismatch) |
| `0x04` | `CMD_OTA_ACTIVATE` | GW $\rightarrow$ Dev | 1 | `[0]=0x04` (Saves metadata, toggles slot, executes reset) |
| `0x84` | `ACK_OTA_ACTIVATE` | Dev $\rightarrow$ GW | 1 | `[0]=0x84` (Reboot confirmation) |

#### RAM Trigger & Entry Sequence

1. During normal operation, the lock node listens on `0x780 | devId`.
2. When `CMD_OTA_START` (`0x01`) is received, the running application writes `0xDEADBEEF` (`FITNET_OTA_TRIGGER_MAGIC`) to persistent RAM at `0x20007000` (`FITNET_OTA_TRIGGER_RAM_ADDR`) and executes `HAL_NVIC_SystemReset()`.
3. The bootloader executes on startup, detects the RAM trigger, clears it, and enters dedicated OTA mode.
4. Target bank erase (56 pages) executes, and `ACK_OTA_START` is emitted.
5. The gateway streams 5-byte chunks paced at ~6 ms delay (`CMD_OTA_DATA`), acknowledged chunk-by-chunk by `ACK_OTA_DATA`.
6. CRC32 is verified via `CMD_OTA_VERIFY` $\rightarrow$ `ACK_OTA_VERIFY`.
7. `CMD_OTA_ACTIVATE` atomically toggles `active_slot`, writes metadata to Page 120, and reboots the target into the newly flashed slot.

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

## 8. Testing Strategy, QA Framework & Diagnostics

The PS Locks Open Integration Platform implements a tiered validation strategy spanning hardware-independent unit regression, fully automated end-to-end integration, bench-level interactive hardware acceptance, and autonomous lock triage and recovery.

```
+─────────────────────────────────────────────────────────────────────────────+
|                         Validation & Testing Pyramid                        |
+─────────────────────────────────────────────────────────────────────────────+
|  [Lock Doctor]                Automated Triage & MCU Recovery (Stages 1-5)  |
|  [Hardware Acceptance CLI]    Physical Bench Acceptance (NFC, Sensor, Audio)|
|  [Automated E2E Suite]        Hybrid Live Actuation + Isolated OTA (Pytest) |
|  [Unit & Regression Tests]    Opcodes, Protocol Parsers & Mock REST Endpoints|
+─────────────────────────────────────────────────────────────────────────────+
```

---

### 8.1 Unit & Regression Tests

The unit test suite validates protocol parsing, CAN frame builders, command serialization/deserialization, and API route handling using mock SocketCAN interfaces without requiring physical hardware:

```bash
# Execute standard hardware-independent test suite
pytest tests/ -v
```

Tests run in milliseconds and verify:
- CRC32 calculation and byte-level packing across all CAN opcodes.
- Parser handling of basic telemetry (`0x200`), version info, and error codes (`0x300`).
- FastAPI route contracts, schema validations, and mock responses.

---

### 8.2 Automated End-to-End (E2E) Test Suite (`tests/e2e/test_system_automated.py`)

The automated E2E test suite executes comprehensive system-level validation covering lock actuation, telemetry synchronization, Whitelist V2 lifecycles, OTA dual-bank flashing, and audit logging.

#### Hybrid Testing Architecture

To enable safe, non-destructive execution in continuous integration (CI) as well as on live test benches:
1. **Real Actuation & Live Telemetry (Lock 1)**: Actuator pulses, open hold, release reset, and LED/buzzer overrides are exercised against physical Lock 1 (or the live gateway daemon state), verifying genuine CAN bus acknowledgments (`LED_SET`, `BUZZ_PLAY`).
2. **Safe Isolated Simulation (Device ID 99)**: The OTA safety interlock and complete dual-bank ping-pong flash cycle run on a dedicated virtual test node (`dev_id = 99`). This guarantees that bench hardware is never accidentally flash-erased, corrupted, or trapped in bootloader loops during test runs.

#### The `CAN_SIMULATION` Safety Guard

Running automated tests on the same host machine as the live Gateway daemon can lead to SocketCAN binding conflicts or duplicate frame consumption. The E2E suite isolates its test process socket by enforcing:

```python
os.environ["CAN_SIMULATION"] = "1"
```

This safety guard routes test-process CAN frames through the internal mock bus while allowing the HTTP client (`SystemE2EClient`) to communicate directly with the live Gateway daemon (`http://127.0.0.1:8000`) or fall back gracefully to FastAPI's in-process `TestClient`.

#### Execution Commands

```bash
# Run the automated E2E test suite locally
pytest tests/e2e/test_system_automated.py -v

# Run against a live Gateway instance running on a specific IP/port
OIP_API_URL=http://127.0.0.1:8000 pytest tests/e2e/test_system_automated.py -v
```

#### Test Coverage Matrix (6 Test Phases)

| Phase | Test Name | Description |
| --- | --- | --- |
| **1. System & Device Inventory** | `test_system_health` | Verifies `GET /api/v1/health` status, timestamps, and uptime. |
| | `test_device_inventory_and_telemetry` | Validates `GET /api/v1/devices/1` attributes (`is_locked`, `is_door_closed`, `active_slot`, `status_text`). |
| **2. Actuator & Command Lifecycle** | `test_actuator_remote_open` | Triggers pulse unlock (`POST /devices/1/open`) and verifies state update. |
| | `test_actuator_open_hold_and_release` | Tests permanent hold open (`/hold`) followed by reset release (`/release`). |
| | `test_led_and_buzzer_remote_overrides` | Emits LED and buzzer overrides; asserts CAN command ACKs (`LED_SET`, `BUZZ_PLAY`). |
| **3. Whitelist V2 & Policy** | `test_whitelist_v2_slot_lifecycle` | Writes Slot 2, configures execution policy, reads back, deletes slot, clears all slots, and verifies occupancy reset. Preserves initial state upon teardown. |
| **4. OTA Dual-Bank Lifecycle** | `test_ota_safety_interlock` | Asserts that operational lock commands return `HTTP 503 Service Unavailable` while OTA is actively in progress. |
| | `test_ota_ping_pong_flash_cycle` | Executes complete flash cycle with bootloader responder; asserts state `COMPLETE`, 100% progress, and active slot toggle. |
| **5. Audit Log Persistence** | `test_audit_log_persistence` | Asserts database persistence and API retrieval of `MANUAL_OPEN`, `WHITELIST_MUTATION`, `OTA_START`, and `OTA_SUCCESS` with validated metadata. |
| **6. Teardown & Recovery** | `safe_lock_recovery` (fixture) | Guarantees that upon test completion (pass or fail), the physical lock is left unlocked with all alarms and LEDs cleared. |

---

### 8.3 Interactive Hardware Acceptance CLI (`tools/hardware_acceptance_cli.py`)

The **Hardware Acceptance CLI** is an interactive bench testing and verification tool designed for manufacturing, assembly validation, and bench QA technicians.

#### Purpose & Capabilities
- Validates physical RFID antenna coupling and cloud-gated provisioning.
- Measures reed switch door sensor open/close transition latency (< 500 ms).
- Exercises acoustic sound generators and optical LEDs with structured operator confirmation prompts.
- Provides a headless simulation mode (`--simulate`) enabling automated non-interactive runs in CI/CD pipelines.

#### Usage & Command-Line Options

```bash
# Interactive bench testing of Lock 1 (prompts operator at each step)
python3 tools/hardware_acceptance_cli.py --device-id 1

# Non-interactive headless simulation mode (for CI/CD validation)
python3 tools/hardware_acceptance_cli.py --simulate

# Run a specific verification section
python3 tools/hardware_acceptance_cli.py --device-id 1 --section nfc
python3 tools/hardware_acceptance_cli.py --device-id 1 --section sensor
python3 tools/hardware_acceptance_cli.py --device-id 1 --section audio_visual
```

**CLI Arguments:**
- `--url`: Base URL of the PS Locks Gateway API (default: `http://127.0.0.1:8000`).
- `--device-id`: Target lock device ID (default: `1`).
- `--section`: Test section to execute (`all`, `nfc`, `sensor`, `audio_visual`).
- `--simulate`: Non-interactive mode that auto-confirms operator prompts and simulates card taps/sensor transitions.
- `--timeout`: Per-step timeout in seconds (default: `30.0`).

#### The 3 Verification Phases

##### Phase 1: NFC / RFID Hardware Check
1. **Unregistered RFID Card & Cloud-Gated Close (Step 1.1)**:
   - Operator presents an unregistered card to the lock antenna.
   - CLI reads the scan, transmits a simulated cloud authorization (`result: 1, action: 1`), writes the card to Slot 1 (ephemeral guest credential), and moves the bolt to locked.
2. **Local Whitelist Opening Verification (Step 1.2)**:
   - Operator presents the **same card** again.
   - Lock validates the card against its local whitelist (Usecase 2) and unlocks bolt with ultra-low latency (**< 150 ms**) without requiring cloud/gateway round-trip latency.
3. **Unauthorized Card Presentation & Denial Signal (Step 1.3)**:
   - Operator presents an unlisted/unauthorized card.
   - CLI transmits a cloud rejection decision (`result: 0, action: 0`), verifying the lock emits a solid red LED and a low-pitched reject tone.

##### Phase 2: Sensor & Door Contact Check
1. **Door Sensor Open Contact Transition (Step 2.1)**:
   - Operator pulls the magnet / separates the reed switch door contact.
   - CLI monitors gateway telemetry and verifies that the `is_door_closed: false` transition is detected within the **< 500 ms** specification limit.
2. **Door Close Guard Security System Check (Step 2.2)**:
   - CLI configures Lock Mode 2 with active Door Close Guard (`behavior_flags: 1`, warning delay: 2s).
   - Once the door remains open past the 2-second timeout, the lock triggers the pulsing **ALARM1 siren** acoustic pattern.
   - Operator closes the door contact, and the CLI verifies that the alarm clears and normal closed state is restored.

##### Phase 3: Visual & Acoustic Verification
The CLI triggers four distinct acoustic feedback profiles paired with optical LED overrides, requiring operator confirmation of pitch, tone progression, and cadence:

| Sound Profile | Sound Code | Acoustic Pattern Description | Visual LED Mode | Verification Criteria |
| --- | --- | --- | --- | --- |
| **OK Tone** | `1` (`BUZZ_OK`) | Ascending two-tone chirp (low pitch $\rightarrow$ high pitch) | Solid Green (`LED_GREEN`) | Positive acceptance chirp with solid green light. |
| **NOT_OK Tone**| `2` (`BUZZ_NOT_OK`) | Two short, low-pitched beeps (low $\rightarrow$ low reject tone) | Solid Red (`LED_RED`) | Rejection tone with solid red light. |
| **ERROR Tone** | `3` (`BUZZ_ERROR`) | Five rapid, staccato warning beeps (`beep-beep-beep-beep-beep`) | Red Blink (`LED_RED_BLINK`) | High-urgency warning burst with flashing red light. |
| **ALARM1 Siren**| `4` (`BUZZ_ALARM1`)| Loud, continuous pulsing alarm siren pattern | Fast Green Blink (`LED_GREEN_FAST`)| Intrusion / Door Close Guard alarm siren pattern. |

##### Safety Teardown
Upon test completion or early exit, the CLI automatically transmits unlock pulses and resets all buzzer and LED states, guaranteeing that the bench lock is left in an unlocked, safe operational state.

---

### 8.4 Lock Doctor & Diagnostics Utility (`tools/lock_doctor.py`)

The **PS Locks Lock Doctor** is an autonomous, single-command triage and recovery utility engineered to diagnose, recover, and re-commission unresponsive, degraded, or stuck locks without requiring manual JTAG/SWD cabling or bench disassembly.

```
+─────────────────────────────────────────────────────────────────────────────+
|                         Lock Doctor 5-Stage Recovery                        |
+─────────────────────────────────────────────────────────────────────────────+
| Stage 1: CAN Interface Repair   Probes link; resets BUS-OFF / down states   |
| Stage 2: Node Ping & Telemetry  Probes application mode (0x02 / 0xFE)       |
| Stage 3: Bootloader Recovery    Probes 0x781; slot activate or full re-flash|
| Stage 4: Database Sync          Creates missing DB record; syncs last_seen  |
| Stage 5: Gateway Verification   Validates online=true in REST API & Web UI  |
+─────────────────────────────────────────────────────────────────────────────+
```

#### Execution

```bash
# Execute automated triage and recovery on Lock 1
python3 tools/lock_doctor.py --device-id 1

# Force complete dual-bank firmware re-flash even if node is responsive
python3 tools/lock_doctor.py --device-id 1 --force-reflash

# Advanced configuration options
python3 tools/lock_doctor.py \
  --device-id 1 \
  --interface can0 \
  --bitrate 50000 \
  --db-path /home/admin/PSLOCKS_OIP/pslocks.db \
  --api-url http://127.0.0.1:8000
```

#### The 5 Recovery Stages Explained

##### Stage 1: CAN Interface Health & Bus-Off Recovery
- Inspects the Linux SocketCAN interface state via `ip -details link show can0`.
- Automatically detects if the controller is in `DOWN`, `STOPPED`, `ERROR-PASSIVE`, or `BUS-OFF` state.
- If degraded, cleanly brings down the interface, configures hardware bitrate (`50000`), configures transmit queue length (`txqueuelen 1000`), and brings the link up into healthy `ERROR-ACTIVE` mode.

##### Stage 2: Node Ping & Telemetry Probe
- Transmits an application status request (`REQUEST_STATUS` `0x02` to CAN ID `0x100 | devId`) and awaits response on `0x200 | devId` or `0x300 | devId` with a 1.0-second timeout.
- If silent, transmits device information request (`REQUEST_DEVICE_INFO` `0xFE`).
- If the node responds, it is confirmed in normal application mode and advances to Stage 4. If completely silent, Lock Doctor initiates Stage 3 bootloader recovery.

##### Stage 3: Bootloader Auto-Activation & Firmware Re-Flash
- **Bootloader Detection**: Probes the node's dedicated OTA CAN ID (`0x780 | devId`, e.g., `0x781`) by transmitting `CMD_OTA_ACTIVATE` (`0x04`).
- **Slot Activation Reboot**: If the bootloader acknowledges with `ACK_OTA_ACTIVATE` (`0x84`), the node was stuck in bootloader mode. Lock Doctor waits 1.0s for the MCU system reset and probes application ping.
- **Autonomous Release Bundle Re-Flash**: If the node remains unresponsive or `--force-reflash` was requested, Lock Doctor resolves the latest firmware release bundle from `uploads/releases/latest_release.json`, determines the target bank (`slot_a.bin` or `slot_b.bin`), streams 5-byte chunks paced at ~6 ms delay, validates CRC32 via `CMD_OTA_VERIFY`, and reboots the lock.
- **Hardware SWD OpenOCD Fallback**: If the CAN transceiver is completely unresponsive, Lock Doctor invokes OpenOCD over SWD (`interface/stlink.cfg`, `target/stm32c0x.cfg`) to inspect CPU registers (`PC`, `VTOR`) and execute a clean hardware `reset run`.

##### Stage 4: SQLite Database Synchronization
- Connects to `pslocks.db` to verify the lock node is cataloged in the `devices` table.
- If missing, automatically registers the device with default parameters (`name: Schloss {id}`, `lock_mode: 1`, `auto_close_timeout: 3`, `active_slot: 1`).
- If present, refreshes the `last_seen` timestamp to current epoch time.

##### Stage 5: Final Gateway REST API Verification
- Requests a health refresh via Gateway REST API (`POST /api/v1/devices/{id}/request_health`).
- Queries `GET /api/v1/devices/{id}` to verify lock telemetry (`status_text`, `is_locked`, `active_slot`).
- Queries `GET /api/v1/devices` inventory to confirm the device is flagged with `online: true`, verifying full restoration across both the CAN bus and the Web Dashboard.

---

### 8.5 Interactive Bench Integration Script

For quick ad-hoc hardware bench validation without interactive prompts, the repository includes an interactive hardware exercise script:

```bash
python3 tests/exhaustive_hardware_test.py
```

---

## 9. License & Support

Developed for **PS GmbH** (Melisau 1255, 6863 Egg / Austria).

For integration support, contact [info@pslocks.com](mailto:info@pslocks.com).