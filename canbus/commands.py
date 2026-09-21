"""
PS Locks OIP
CAN Command Builder

Constructs raw CAN protocol commands and settings updates to be sent
over the CAN network to physical devices. Incorporates thread-safe
correlation IDs to track command acknowledgments.
"""

import threading
from typing import Optional

from .constants import *
from .protocol import CANFrame

_corr_id_lock = threading.Lock()
_corr_id_counter = 1

def _next_corr_id() -> int:
    """
    Generates a thread-safe rolling correlation ID (1-255).
    """
    global _corr_id_counter
    with _corr_id_lock:
        val = _corr_id_counter
        _corr_id_counter += 1
        if _corr_id_counter > 255:
            _corr_id_counter = 1
        return val


def _frame(can_id: int, payload: list[int]) -> CANFrame:

    return CANFrame(
        arbitration_id=can_id,
        data=bytes(payload),
    )


# ---------------------------------------------------------
# LOCK
# ---------------------------------------------------------

def open_lock(device_id: int, lock_id: int = 0) -> CANFrame:
    """
    Constructs an OPEN command (0x01) for a specific device.
    
    Triggers a temporary pulse open that auto-relocks.
    
    Args:
        device_id (int): Target node ID.
        lock_id (int): Logical lock index (0-based).
        
    Returns:
        CANFrame: The constructed CAN frame to transmit.
    """
    return _frame(
        CAN_COMMAND | device_id,
        [
            lock_id,
            OPEN_LOCK,
            _next_corr_id(),
        ],
    )


def request_status(device_id: int, lock_id: int = 0) -> CANFrame:
    """
    Constructs a REQUEST STATUS command (0x02) for a specific device.
    
    Requests the current basic lock state and error code.
    
    Args:
        device_id (int): Target node ID.
        lock_id (int): Logical lock index (0-based).
        
    Returns:
        CANFrame: The constructed CAN frame.
    """
    return _frame(
        CAN_COMMAND | device_id,
        [
            lock_id,
            REQUEST_STATUS,
            _next_corr_id(),
        ],
    )


def open_hold(device_id: int, lock_id: int = 0) -> CANFrame:
    """
    Constructs an OPEN HOLD command (0x04) for a specific device.
    
    Forces the lock to stay permanently unlocked (Daueroffen).
    
    Args:
        device_id (int): Target node ID.
        lock_id (int): Logical lock index.
        
    Returns:
        CANFrame: The constructed CAN frame.
    """
    return _frame(
        CAN_COMMAND | device_id,
        [
            lock_id,
            OPEN_HOLD,
            _next_corr_id(),
        ],
    )


def open_reset(device_id: int, lock_id: int = 0) -> CANFrame:
    """
    Constructs an OPEN RESET command (0x05) for a specific device.
    
    Cancels an open hold state and forces the lock to shut (Verriegeln).
    
    Args:
        device_id (int): Target node ID.
        lock_id (int): Logical lock index.
        
    Returns:
        CANFrame: The constructed CAN frame.
    """
    return _frame(
        CAN_COMMAND | device_id,
        [
            lock_id,
            OPEN_RESET,
            _next_corr_id(),
        ],
    )


# ---------------------------------------------------------
# LED
# ---------------------------------------------------------

def led_set(
    device_id: int,
    mode: int,
    period10ms: int = 100,
    duty: int = 50,
    ttl: int = 5,
    lock_id: int = 0,
) -> CANFrame:
    """
    Constructs an LED configuration command.

    Args:
        device_id (int): Target node ID.
        mode (int): LED mode identifier (e.g., LED_GREEN, LED_RED).
        period10ms (int): Blink period in units of 10 ms (default 100 = 1 s).
        duty (int): PWM duty cycle in percent (0-100, default 50).
        ttl (int): Time-to-live in seconds before auto-off (default 5 s).
        lock_id (int): Logical lock index.

    Returns:
        CANFrame: The constructed CAN frame.
    """
    return _frame(
        CAN_COMMAND | device_id,
        [
            lock_id,
            LED_SET,
            _next_corr_id(),
            mode,
            period10ms,
            duty,
            ttl,
        ],
    )


def led_reset(
    device_id: int,
    lock_id: int = 0,
) -> CANFrame:
    """
    Constructs an LED reset command to turn off active optical indicators.

    Args:
        device_id (int): Target node ID.
        lock_id (int): Logical lock index.

    Returns:
        CANFrame: The constructed CAN frame.
    """
    return _frame(
        CAN_COMMAND | device_id,
        [
            lock_id,
            LED_RESET,
            _next_corr_id(),
        ],
    )


# ---------------------------------------------------------
# BUZZER
# ---------------------------------------------------------

def buzz_play(
    device_id: int,
    sound: int,
    repeat: int = 1,
    lock_id: int = 0,
) -> CANFrame:
    """
    Constructs an acoustic buzzer playback command.

    Args:
        device_id (int): Target node ID.
        sound (int): Sound pattern identifier (e.g., BUZZ_OK, BUZZ_DENY).
        repeat (int): Number of repetition cycles.
        lock_id (int): Logical lock index.

    Returns:
        CANFrame: The constructed CAN frame.
    """
    return _frame(
        CAN_COMMAND | device_id,
        [
            lock_id,
            BUZZ_PLAY,
            _next_corr_id(),
            sound,
            repeat,
        ],
    )


def buzz_stop(
    device_id: int,
    lock_id: int = 0,
) -> CANFrame:
    """
    Constructs an acoustic buzzer stop command.

    Args:
        device_id (int): Target node ID.
        lock_id (int): Logical lock index.

    Returns:
        CANFrame: The constructed CAN frame.
    """
    return _frame(
        CAN_COMMAND | device_id,
        [
            lock_id,
            BUZZ_STOP,
            _next_corr_id(),
        ],
    )


# ---------------------------------------------------------
# SYSTEM
# ---------------------------------------------------------

def device_reset(
    device_id: int,
    lock_id: int = 0,
) -> CANFrame:
    """
    Constructs a software reboot command for the target device.

    Args:
        device_id (int): Target node ID.
        lock_id (int): Logical lock index.

    Returns:
        CANFrame: The constructed CAN frame.
    """
    return _frame(
        CAN_COMMAND | device_id,
        [
            lock_id,
            DEVICE_RESET,
            _next_corr_id(),
        ],
    )


def factory_reset(
    device_id: int,
    lock_id: int = 0,
) -> CANFrame:
    """
    Constructs a factory reset command restoring default settings and unprovisioning the node.

    Args:
        device_id (int): Target node ID.
        lock_id (int): Logical lock index.

    Returns:
        CANFrame: The constructed CAN frame.
    """
    return _frame(
        CAN_COMMAND | device_id,
        [
            lock_id,
            FACTORY_RESET,
            _next_corr_id(),
        ],
    )


def request_health(
    device_id: int,
    lock_id: int = 0,
) -> CANFrame:
    """
    Constructs a diagnostic health telemetry request.

    Args:
        device_id (int): Target node ID.
        lock_id (int): Logical lock index.

    Returns:
        CANFrame: The constructed CAN frame.
    """
    return _frame(
        CAN_COMMAND | device_id,
        [
            lock_id,
            REQUEST_HEALTH,
            _next_corr_id(),
        ],
    )


def request_version(
    device_id: int,
    lock_id: int = 0,
) -> CANFrame:
    """
    Constructs a firmware version query command.

    Args:
        device_id (int): Target node ID.
        lock_id (int): Logical lock index.

    Returns:
        CANFrame: The constructed CAN frame.
    """
    return _frame(
        CAN_COMMAND | device_id,
        [
            lock_id,
            REQUEST_VERSION,
            _next_corr_id(),
        ],
    )


def request_device_info(
    device_id: int,
    lock_id: int = 0,
) -> CANFrame:
    """
    Constructs a hardware info query command.

    Args:
        device_id (int): Target node ID.
        lock_id (int): Logical lock index.

    Returns:
        CANFrame: The constructed CAN frame.
    """
    return _frame(
        CAN_COMMAND | device_id,
        [
            lock_id,
            REQUEST_DEVICE_INFO,
            _next_corr_id(),
        ],
    )


# ---------------------------------------------------------
# PROVISIONING
# ---------------------------------------------------------

def request_full_uid(
    uid32: bytes,
    corr_id: Optional[int] = None,
    lock_id: int = 0,
) -> CANFrame:
    """
    Requests the full hardware unique ID for an unprovisioned node using its 32-bit broadcast UID.

    Args:
        uid32 (bytes): 4-byte truncated unique ID.
        corr_id (Optional[int]): Correlation ID.
        lock_id (int): Logical lock index.

    Returns:
        CANFrame: The constructed CAN frame.
    """
    if len(uid32) != 4:
        raise ValueError("uid32 must be 4 bytes.")

    if corr_id is None:
        corr_id = _next_corr_id()

    return _frame(
        CAN_COMMAND | DEFAULT_DEVICE_ID,
        [
            lock_id,
            REQUEST_FULL_UID,
            corr_id,
            *uid32,
        ],
    )


def assign_id(
    uid32: bytes,
    new_device_id: int,
    bitrate_code: int = BITRATE_50K,
    flags: int = 0,
) -> CANFrame:
    """
    Assigns a persistent device ID and CAN bus bitrate to an unprovisioned node.

    Args:
        uid32 (bytes): 4-byte truncated unique ID matching the target node.
        new_device_id (int): Assigned node ID (1-126).
        bitrate_code (int): Configured CAN bitrate (default BITRATE_50K).
        flags (int): Configuration flags.

    Returns:
        CANFrame: The constructed CAN frame.
    """
    if len(uid32) != 4:
        raise ValueError("uid32 must be 4 bytes.")

    return _frame(
        CAN_MASTER | DEFAULT_DEVICE_ID,
        [
            MASTER_ASSIGN_ID,
            *uid32,
            new_device_id,
            bitrate_code,
            flags,
        ],
    )


def build_master_beacon_frame(
    version: int = 1,
    role: int = 1,
    state: int = 1,
    interval_ms: int = 1000,
    failover_ms: int = 5000,
    takeover_ms: int = 2000,
    flags: int = 0,
) -> CANFrame:
    """
    Constructs the Master Beacon frame (0xB0) on CAN ID 0x47F.

    Args:
        version (int): Protocol version.
        role (int): Master role identifier.
        state (int): Master synchronization state.
        interval_ms (int): Heartbeat interval in milliseconds.
        failover_ms (int): Failover timeout in milliseconds.
        takeover_ms (int): Master takeover timeout in milliseconds.
        flags (int): Control flags.

    Returns:
        CANFrame: The constructed CAN frame.
    """
    return _frame(
        CAN_MASTER | DEFAULT_DEVICE_ID,
        [
            MASTER_BEACON,
            version,
            role,
            state,
            interval_ms // 100,
            failover_ms // 100,
            takeover_ms // 100,
            flags,
        ],
    )



# ---------------------------------------------------------
# WHITELIST & SETTINGS
# ---------------------------------------------------------

def request_wl_info(device_id: int, lock_id: int = 0) -> CANFrame:
    """Requests whitelist memory utilization and slot capacity."""
    return _frame(CAN_COMMAND | device_id, [lock_id, REQUEST_WL_INFO, _next_corr_id()])


def request_wl_list_v2(device_id: int, start_index: int = 1, page_size: int = 32, lock_id: int = 0) -> CANFrame:
    """Requests a paged list of stored whitelist entries."""
    return _frame(CAN_COMMAND | device_id, [lock_id, REQUEST_WL_LIST_V2, _next_corr_id(), start_index, page_size])


def wl_write_uid(device_id: int, slot_index: int, uid_len: int, uid_bytes: bytes, lock_id: int = 0) -> CANFrame:
    """Writes the first part of a whitelist entry (slot, length, and UID bytes 0-2)."""
    corr_id = _next_corr_id()
    payload = [lock_id, WL_WRITE_UID, corr_id, slot_index, uid_len]
    payload.extend(list(uid_bytes[:3]))
    while len(payload) < 8:
        payload.append(0)
    return _frame(CAN_COMMAND | device_id, payload)


def wl_write_uid_part2(device_id: int, uid_bytes: bytes, ttl_days: int = 0, lock_id: int = 0) -> CANFrame:
    """Writes the remaining bytes (bytes 3-6) and TTL of a whitelist entry."""
    corr_id = _next_corr_id()
    payload = [lock_id, WL_WRITE_UID_PART2, corr_id]
    payload.extend(list(uid_bytes[3:7]))
    while len(payload) < 7:
        payload.append(0)
    payload.append(ttl_days)
    return _frame(CAN_COMMAND | device_id, payload)


def delete_wl_slot(device_id: int, slot_index: int, lock_id: int = 0) -> CANFrame:
    """Deletes the whitelist entry stored at the specified slot index."""
    return _frame(CAN_COMMAND | device_id, [lock_id, DELETE_WL_SLOT, _next_corr_id(), slot_index])


def wl_clear(device_id: int, lock_id: int = 0) -> CANFrame:
    """Clears all stored whitelist entries in persistent memory."""
    return _frame(CAN_COMMAND | device_id, [lock_id, WL_CLEAR, _next_corr_id()])


def set_wl_slot_policy(device_id: int, slot_index: int, policy: int, open_action: int, flags: int = 0, lock_id: int = 0) -> CANFrame:
    """Configures access policy and door opening action for a specific whitelist slot."""
    return _frame(CAN_COMMAND | device_id, [lock_id, SET_WL_SLOT_POLICY, _next_corr_id(), slot_index, policy, open_action, flags])


def set_lock_mode(
    device_id: int,
    lock_mode: int,
    auto_close_timeout_s: int = 3,
    behavior_flags: int = 0,
    door_warning_delay_s: int = 2,
    door_release_delay_s: int = 5,
    lock_id: int = 0,
) -> CANFrame:
    """Configures the lock operating mode (Standard, Auto-Close, Office) and timeout parameters."""
    return _frame(
        CAN_COMMAND | device_id,
        [
            lock_id,
            SET_LOCK_MODE,
            _next_corr_id(),
            lock_mode,
            auto_close_timeout_s,
            behavior_flags,
            door_warning_delay_s,
            door_release_delay_s,
        ],
    )


def request_lock_mode(device_id: int, lock_id: int = 0) -> CANFrame:
    """Requests current lock operating mode configuration."""
    return _frame(CAN_COMMAND | device_id, [lock_id, REQUEST_LOCK_MODE, _next_corr_id()])


def request_occupancy_state(device_id: int, lock_id: int = 0) -> CANFrame:
    """Requests locker occupancy status telemetry."""
    return _frame(CAN_COMMAND | device_id, [lock_id, REQUEST_OCCUPANCY_STATE, _next_corr_id()])


def auth_resp(device_id: int, result: int, action: int, lock_id: int = 0) -> CANFrame:
    """Sends host authorization response for an online card access challenge."""
    return _frame(CAN_COMMAND | device_id, [lock_id, AUTH_RESP, _next_corr_id(), result, action])
