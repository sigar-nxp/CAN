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

    if len(uid32) != 4:
        raise ValueError("uid32 muss 4 Byte haben.")

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
    bitrate_code: int = BITRATE_50K,   #For production change to 50K. This is just for testing with current Test Hardware
    flags: int = 0,
) -> CANFrame:

    if len(uid32) != 4:
        raise ValueError("uid32 muss 4 Byte haben.")

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
    Constructs the Master Beacon frame (0xB0) on 0x47F.
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
    return _frame(CAN_COMMAND | device_id, [lock_id, REQUEST_WL_INFO, _next_corr_id()])

def request_wl_list_v2(device_id: int, start_index: int = 1, page_size: int = 32, lock_id: int = 0) -> CANFrame:
    return _frame(CAN_COMMAND | device_id, [lock_id, REQUEST_WL_LIST_V2, _next_corr_id(), start_index, page_size])

def wl_write_uid(device_id: int, slot_index: int, uid_len: int, uid_bytes: bytes, lock_id: int = 0) -> CANFrame:
    corr_id = _next_corr_id()
    payload = [lock_id, WL_WRITE_UID, corr_id, slot_index, uid_len]
    payload.extend(list(uid_bytes[:3]))
    while len(payload) < 8:
        payload.append(0)
    return _frame(CAN_COMMAND | device_id, payload)

def wl_write_uid_part2(device_id: int, uid_bytes: bytes, ttl_days: int = 0, lock_id: int = 0) -> CANFrame:
    corr_id = _next_corr_id()
    payload = [lock_id, WL_WRITE_UID_PART2, corr_id]
    payload.extend(list(uid_bytes[3:7]))
    while len(payload) < 7:
        payload.append(0)
    payload.append(ttl_days)
    return _frame(CAN_COMMAND | device_id, payload)

def delete_wl_slot(device_id: int, slot_index: int, lock_id: int = 0) -> CANFrame:
    return _frame(CAN_COMMAND | device_id, [lock_id, DELETE_WL_SLOT, _next_corr_id(), slot_index])

def wl_clear(device_id: int, lock_id: int = 0) -> CANFrame:
    return _frame(CAN_COMMAND | device_id, [lock_id, WL_CLEAR, _next_corr_id()])

def set_wl_slot_policy(device_id: int, slot_index: int, policy: int, open_action: int, flags: int = 0, lock_id: int = 0) -> CANFrame:
    return _frame(CAN_COMMAND | device_id, [lock_id, SET_WL_SLOT_POLICY, _next_corr_id(), slot_index, policy, open_action, flags])

def set_lock_mode(device_id: int, lock_mode: int, auto_close_timeout_s: int = 3, behavior_flags: int = 0, door_warning_delay_s: int = 2, door_release_delay_s: int = 5, lock_id: int = 0) -> CANFrame:
    return _frame(CAN_COMMAND | device_id, [lock_id, SET_LOCK_MODE, _next_corr_id(), lock_mode, auto_close_timeout_s, behavior_flags, door_warning_delay_s, door_release_delay_s])

def request_lock_mode(device_id: int, lock_id: int = 0) -> CANFrame:
    return _frame(CAN_COMMAND | device_id, [lock_id, REQUEST_LOCK_MODE, _next_corr_id()])

def request_occupancy_state(device_id: int, lock_id: int = 0) -> CANFrame:
    return _frame(CAN_COMMAND | device_id, [lock_id, REQUEST_OCCUPANCY_STATE, _next_corr_id()])

def auth_resp(device_id: int, result: int, action: int, lock_id: int = 0) -> CANFrame:
    return _frame(CAN_COMMAND | device_id, [lock_id, AUTH_RESP, _next_corr_id(), result, action])
