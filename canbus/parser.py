"""
PS Locks OIP CAN Bus Parser.

Responsible for decoding raw CAN frames into specific dataclass structures
representing status updates, telemetry, whitelist synchronization data,
and health information.
"""

import struct
from dataclasses import dataclass
from typing import Optional


from .protocol import CANFrame
from .constants import *


# ==========================================================
# Dataclasses
# ==========================================================

@dataclass(slots=True)
class StatusRFID:
    """Decoded RFID card tap event (Part 1, bytes 0-3 of UID)."""
    device_id: int
    lock_id: int
    action_type: int
    result_or_action: int
    uid_len: int
    uid: bytes


@dataclass(slots=True)
class StatusRFIDPart2:
    """Supplementary frame for 7-byte / 10-byte card UIDs (bytes 4-9)."""
    device_id: int
    lock_id: int
    uid_part2: bytes


@dataclass(slots=True)
class EventWlAutoDelete:
    """Notification of expired ephemeral whitelist slot deletion."""
    device_id: int
    lock_id: int


@dataclass(slots=True)
class HealthShort:
    """Primary telemetry packet: supply voltage (mV) and MCU temperature (°C)."""
    device_id: int
    vcc_mv: int
    temp_c: int


@dataclass(slots=True)
class HealthExtended:
    """Secondary telemetry packet: available RAM heap (bytes) and uptime (s)."""
    device_id: int
    free_ram: int
    uptime_s: int


@dataclass(slots=True)
class HealthDiagInfo:
    """Hardware diagnostics: reset flags, brownout/watchdog counters, and CAN error flags."""
    device_id: int
    reset_reason: int
    brownout_cnt: int
    watchdog_cnt: int
    can_tx_err: int
    can_rx_err: int


@dataclass(slots=True)
class HealthSecurityStatus:
    """Security and cryptographic subsystem status report."""
    device_id: int
    state: int
    supported: int
    pending: int
    last_counter: int


@dataclass(slots=True)
class HealthVersionInfo:
    """Firmware version string and currently active execution flash bank/slot."""
    device_id: int
    version_str: str
    active_slot: Optional[int] = None


@dataclass(slots=True)
class HealthDeviceInfo:
    """Hardware type, PCB revision, and capability flags (e.g. Door Close Guard)."""
    device_id: int
    hw_info_str: str
    door_close_guard_supported: bool = False


@dataclass(slots=True)
class IdRequest:
    """Unprovisioned node discovery broadcast containing the 32-bit hardware UID."""
    device_id: int
    uid32: bytes


@dataclass(slots=True)
class StatusBasic:
    """Primary lock state report (physical state, error code, and active flash slot)."""
    device_id: int
    lock_id: int
    lock_state: int
    lock_error: int
    active_slot: Optional[int] = None


@dataclass(slots=True)
class CommandAck:
    """Command acknowledgment frame confirming execution and correlation ID."""
    device_id: int
    lock_id: int
    command: int
    corr_id: int


@dataclass(slots=True)
class StatusError:
    """Error frame returned when a command is rejected or execution fails."""
    device_id: int
    error_code: int
    lock_id: int = 0
    cmd: int = 0
    corr_id: int = 0


@dataclass(slots=True)
class UIDPart1:
    """First chunk of 64-bit/96-bit MCU UID."""
    device_id: int
    uid: bytes


@dataclass(slots=True)
class UIDPart2:
    """Second chunk of 64-bit/96-bit MCU UID."""
    device_id: int
    uid: bytes


@dataclass(slots=True)
class HealthStatus:
    """Raw unparsed health telemetry frame payload."""
    device_id: int
    data: bytes


@dataclass(slots=True)
class VersionInfo:
    """Raw version frame payload."""
    device_id: int
    data: bytes


@dataclass(slots=True)
class DeviceInfo:
    """Raw device info frame payload."""
    device_id: int
    data: bytes


# ==========================================================
# Parser
# ==========================================================


@dataclass(slots=True)
class WlInfoReport:
    """Whitelist storage allocation report (used slots, max capacity, active ephemeral entries)."""
    device_id: int
    used_persistent_slots: int
    max_persistent_capacity: int
    ephemeral_active: int


@dataclass(slots=True)
class LockModeReport:
    """Configured operating mode and timing parameters (auto-close, door warning/release delays)."""
    device_id: int
    lock_mode: int
    auto_close_timeout_s: int
    behavior_flags: int = 0
    door_warning_delay_s: int = 2
    door_release_delay_s: int = 5


@dataclass(slots=True)
class RuntimeStateSnapshot:
    """Real-time lock hardware snapshot: state revision, occupancy, errors, and LED status."""
    device_id: int
    lock_id: int
    physical_state: int
    lock_error: int
    occupancy_flags: int
    led_status: int
    led_remaining_s: int
    state_revision: int


@dataclass(slots=True)
class DiagExtended:
    """Extended diagnostic report tracking bus-off events and recovery stages."""
    device_id: int
    lock_id: int
    bus_off_counter: int
    last_recovery_stage: int
    diag_flags: int


@dataclass(slots=True)
class WlListV2Item:
    """Whitelist slot header entry: slot index, entry type, UID length, policy, action, TTL."""
    device_id: int
    slot_index: int
    entry_type: int
    uid_len: int
    policy: int
    open_action: int
    ttl_days: int


@dataclass(slots=True)
class WlListV2UidPart1:
    """Whitelist slot UID bytes 0-3."""
    device_id: int
    slot_index: int
    uid_bytes: bytes


@dataclass(slots=True)
class WlListV2UidPart2:
    """Whitelist slot UID bytes 4-9."""
    device_id: int
    slot_index: int
    uid_bytes: bytes


@dataclass(slots=True)
class OccupancyStateReport:
    """Locker occupancy status telemetry: occupancy flag, owner present flag, and counter."""
    device_id: int
    occupied: bool
    owner_present: bool
    source: int
    state_counter_24: int


@dataclass(slots=True)
class OccupancyOwnerShort:
    """Truncated owner UID (first 4 bytes) for currently assigned locker occupant."""
    device_id: int
    owner_uid_short: bytes


@dataclass(slots=True)
class PolicyActionReport:
    """Outcome report for local policy evaluation upon card presentation."""
    device_id: int
    policy: int
    open_action: int
    local_result: int
    occupancy_effect: int


class CANParser:

    def parse(self, frame: CANFrame):

        msg_type = frame.message_type
        dev_id = frame.device_id
        data = frame.data

        # --------------------------------------------------
        # STATUS
        # --------------------------------------------------

        if msg_type == CAN_ID_REQUEST:
            if len(data) >= 4:
                return IdRequest(
                    device_id=dev_id,
                    uid32=bytes(data[0:4]),
                )
            return None

        if msg_type == CAN_STATUS:

            if len(data) == 0:
                return None

            sub = data[0]

            if sub == STATUS_BASIC and len(data) >= 4:
                active_slot = data[4] if len(data) >= 5 else None
                return StatusBasic(
                    device_id=dev_id,
                    lock_id=data[1],
                    lock_state=data[2],
                    lock_error=data[3],
                    active_slot=active_slot,
                )

            if sub == STATUS_COMMAND_ACK and len(data) >= 4:

                return CommandAck(
                    device_id=dev_id,
                    lock_id=data[1],
                    command=data[2],
                    corr_id=data[3],
                )

            
            if sub == STATUS_BASIC and len(data) >= 3:

                return StatusBasic(
                    device_id=dev_id,
                    lock_id=data[1],
                    lock_state=data[2],
                    lock_error=data[3] if len(data) >= 4 else 0,
                )
            
            if sub == STATUS_ERROR and len(data) >= 3:
                return StatusError(
                    device_id=dev_id,
                    error_code=data[1],
                    lock_id=data[2],
                    cmd=data[3] if len(data) >= 5 else 0,
                    corr_id=data[4] if len(data) >= 5 else 0
                )

            
            if sub == STATUS_RFID_AUTH_REQ_PART2 and len(data) >= 4:
                return StatusRFIDPart2(
                    device_id=dev_id,
                    lock_id=data[1],
                    uid_part2=bytes(data[3:])
                )

            if sub == STATUS_EVENT_WL_AUTO_DELETE and len(data) >= 2:
                return EventWlAutoDelete(
                    device_id=dev_id,
                    lock_id=data[1]
                )

            if sub in (0x10, 0x11) and len(data) >= 4:
                return StatusRFID(
                    device_id=dev_id,
                    lock_id=data[1],
                    action_type=sub,
                    result_or_action=data[2],
                    uid_len=data[3],
                    uid=bytes(data[4:]),
                )


            if sub == STATUS_WL_INFO_REPORT and len(data) >= 6:
                return WlInfoReport(dev_id, data[3], data[4], data[5])
            if sub == STATUS_LOCK_MODE_REPORT and len(data) >= 8:
                return LockModeReport(
                    device_id=dev_id,
                    lock_mode=data[3],
                    auto_close_timeout_s=data[4],
                    behavior_flags=data[5],
                    door_warning_delay_s=data[6],
                    door_release_delay_s=data[7]
                )
            elif sub == STATUS_LOCK_MODE_REPORT and len(data) >= 5:
                return LockModeReport(
                    device_id=dev_id,
                    lock_mode=data[3],
                    auto_close_timeout_s=data[4]
                )
            if sub == STATUS_WL_LIST_V2_ITEM and len(data) >= 7:
                return WlListV2Item(
                    dev_id, data[3], 
                    (data[4] >> 4) & 0x0F, data[4] & 0x0F,
                    (data[5] >> 4) & 0x0F, data[5] & 0x0F,
                    data[6]
                )
            if sub == STATUS_WL_LIST_V2_UID_PART1 and len(data) >= 5:
                return WlListV2UidPart1(dev_id, data[3], bytes(data[4:]))
            if sub == STATUS_WL_LIST_V2_UID_PART2 and len(data) >= 5:
                return WlListV2UidPart2(dev_id, data[3], bytes(data[4:]))
            if sub == STATUS_OCCUPANCY_STATE_REPORT and len(data) >= 8:
                state_cnt = (data[5] << 16) | (data[6] << 8) | data[7]
                return OccupancyStateReport(dev_id, bool(data[2]), bool(data[3]), data[4], state_cnt)
            if sub == STATUS_OCCUPANCY_OWNER_SHORT and len(data) >= 7:
                return OccupancyOwnerShort(dev_id, bytes(data[3:7]))
            if sub == STATUS_RFID_POLICY_ACTION and len(data) >= 5:
                return PolicyActionReport(
                    dev_id, (data[2] >> 4) & 0x0F, data[2] & 0x0F, data[3], data[4]
                )
            return data

        # --------------------------------------------------
        # HEALTH
        # --------------------------------------------------

        if msg_type == CAN_HEALTH:
            if len(data) > 0:
                sub = data[0]
                if sub == HEALTH_SHORT and len(data) >= 4:
                    vcc = (data[1] << 8) | data[2]
                    temp = data[3]
                    if temp > 127:
                        temp -= 256
                    return HealthShort(device_id=dev_id, vcc_mv=vcc, temp_c=temp)
                elif sub == HEALTH_EXTENDED and len(data) >= 7:
                    ram = struct.unpack(">H", data[1:3])[0]
                    uptime = struct.unpack(">I", data[3:7])[0]
                    return HealthExtended(device_id=dev_id, free_ram=ram, uptime_s=uptime)
                elif sub == HEALTH_VERSION and len(data) >= 4:
                    version_str = f"{data[1]}.{data[2]}.{data[3]}"
                    active_slot = data[6] if len(data) >= 7 and data[6] in (0, 1) else None
                    return HealthVersionInfo(device_id=dev_id, version_str=version_str, active_slot=active_slot)
                elif sub == HEALTH_DEVICE_INFO and len(data) >= 5:
                    hw_info_str = f"Type:{data[3]} Rev:{data[4]}"
                    door_close_guard_supported = False
                    if len(data) >= 8:
                        cap_flags = (data[5] << 16) | (data[6] << 8) | data[7]
                        door_close_guard_supported = bool(cap_flags & (1 << 14))
                    return HealthDeviceInfo(
                        device_id=dev_id, 
                        hw_info_str=hw_info_str,
                        door_close_guard_supported=door_close_guard_supported
                    )
                elif sub == HEALTH_DIAG_INFO and len(data) >= 6:
                    return HealthDiagInfo(
                        device_id=dev_id,
                        reset_reason=data[1],
                        brownout_cnt=data[2],
                        watchdog_cnt=data[3],
                        can_tx_err=data[4],
                        can_rx_err=data[5]
                    )
                elif sub == HEALTH_SECURITY_STATUS and len(data) >= 8:
                    last_cnt = (data[4] << 24) | (data[5] << 16) | (data[6] << 8) | data[7]
                    return HealthSecurityStatus(
                        device_id=dev_id,
                        state=data[1],
                        supported=data[2],
                        pending=data[3],
                        last_counter=last_cnt
                    )
                elif sub == HEALTH_RUNTIME_STATE and len(data) >= 8:
                    return RuntimeStateSnapshot(
                        device_id=dev_id,
                        lock_id=data[1],
                        physical_state=data[2],
                        lock_error=data[3],
                        occupancy_flags=data[4],
                        led_status=data[5],
                        led_remaining_s=data[6],
                        state_revision=data[7]
                    )
                elif sub == HEALTH_DIAG_EXTENDED and len(data) >= 5:
                    return DiagExtended(
                        device_id=dev_id,
                        lock_id=data[1],
                        bus_off_counter=data[2],
                        last_recovery_stage=data[3],
                        diag_flags=data[4]
                    )

            return HealthStatus(
                device_id=dev_id,
                data=data,
            )

        # --------------------------------------------------
        # UID PART 1
        # --------------------------------------------------

        if msg_type == CAN_UID_PART1:

            return UIDPart1(
                device_id=dev_id,
                uid=bytes(data),
            )

        # --------------------------------------------------
        # UID PART 2
        # --------------------------------------------------

        if msg_type == CAN_UID_PART2:

            return UIDPart2(
                device_id=dev_id,
                uid=bytes(data),
            )

        return None
