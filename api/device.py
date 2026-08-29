"""
FastAPI endpoints for device management and operation.

Defines the core REST API routes for discovering, provisioning, controlling,
and configuring physical locks. This includes endpoints for basic actions
(open, close, pulse), settings (whitelist, lock mode), and real-time state.
"""


from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

from config.globals import can_listener, can_service, beacon_service
from services.lock_service import LockService
from canbus.commands import wl_write_uid, wl_write_uid_part2

router = APIRouter()
lock_service = LockService()

# Request Models
class LockModeReq(BaseModel):
    lock_mode: int
    auto_close_timeout_s: int = 3
    behavior_flags: int = 0
    door_warning_delay_s: int = 2
    door_release_delay_s: int = 5

class PolicyReq(BaseModel):
    policy: int
    open_action: int
    flags: int = 0

class WhitelistWriteReq(BaseModel):
    slot_index: int
    uid_hex: str
    ttl_days: int = 0

class AuthRespReq(BaseModel):
    result: int
    action: int

class LEDRequest(BaseModel):
    mode: int
    period10ms: int = 0
    duty: int = 0
    ttl: int = 0

class BuzzerRequest(BaseModel):
    sound: int
    repeat: int = 1

class AssignRequest(BaseModel):
    uid32: int
    device_id: int
    bitrate_code: int = 4

# Routes
@router.get("/unassigned")
def get_unassigned():
    devices = can_listener.get_unassigned_devices()
    result = []
    for d in devices:
        if hasattr(d, "to_dict"):
            result.append(d.to_dict())
        elif isinstance(d, dict):
            result.append(d)
        elif isinstance(d, str):
            # UID string format
            uid_int = int(d, 16) if d else 0
            result.append({"uid32_hex": d, "uid32": uid_int})
        else:
            result.append({"raw": str(d)})
    return result

@router.post("/devices/assign")
def assign_device(req: AssignRequest):
    uid32_bytes = req.uid32.to_bytes(4, byteorder='big')
    from canbus.commands import assign_id
    lock_service.send(assign_id(uid32_bytes, req.device_id, req.bitrate_code))
    return {"status": "assigned", "device_id": req.device_id}

@router.get("/devices")
def get_devices():
    devices = can_listener.devices.values()
    return [{"device_id": dev.device_id, "status": dev.status_text, "online": dev.online} for dev in devices]

@router.get("/devices/{id}")
def get_device(id: int):
    dev = can_listener.get_device(id)
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    
    return {
        "is_locked": dev.is_locked,
        "is_door_closed": dev.is_door_closed,
        "status_text": dev.status_text,
        "error_text": dev.error_text,
        "last_scanned_card": dev.last_scanned_card,
        "door_close_guard_supported": dev.door_close_guard_supported
    }

@router.get("/devices/{id}/health")
def get_health(id: int):
    lock_service.request_health(id)
    dev = can_listener.get_device(id)
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    return {
        "health_short": dev.health_short,
        "health_extended": dev.health_extended,
        "health_diag_info": dev.health_diag_info,
        "runtime_state": dev.runtime_state,
        "diag_extended": dev.diag_extended
    }

@router.get("/devices/{id}/version")
def get_version(id: int):
    dev = can_listener.get_device(id)
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    return {
        "firmware_version": dev.firmware_version,
        "hardware_version": dev.hardware_version
    }

@router.get("/devices/{id}/occupancy")
def get_occupancy(id: int):
    dev = can_listener.get_device(id)
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    return dev.occupancy_state

@router.post("/devices/{id}/open")
def open_device(id: int):
    lock_service.open(id)
    return {"status": "opened"}

@router.post("/devices/{id}/open_hold")
def open_hold_device(id: int):
    lock_service.open_hold(id)
    return {"status": "held open"}

@router.post("/devices/{id}/reset")
def reset_device(id: int):
    lock_service.open_reset(id)
    return {"status": "open state reset"}

@router.post("/devices/{id}/led")
def led_device(id: int, req: LEDRequest):
    from canbus.commands import led_set
    lock_service.send(led_set(id, req.mode, req.period10ms, req.duty, req.ttl))
    return {"status": "led set"}

@router.post("/devices/{id}/buzzer")
def buzzer_device(id: int, req: BuzzerRequest):
    from canbus.commands import buzz_play
    lock_service.send(buzz_play(id, req.sound, req.repeat))
    return {"status": "buzzer set"}

@router.post("/devices/{id}/mode")
def set_mode(id: int, req: LockModeReq):
    lock_service.set_lock_mode(
        id, 
        req.lock_mode, 
        req.auto_close_timeout_s, 
        req.behavior_flags, 
        req.door_warning_delay_s, 
        req.door_release_delay_s
    )
    return {"status": "mode set"}

@router.get("/devices/{id}/mode")
def get_mode(id: int):
    lock_service.request_lock_mode(id)
    dev = can_listener.get_device(id)
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    return {
        "lock_mode": dev.lock_mode,
        "auto_close_timeout": dev.auto_close_timeout,
        "behavior_flags": dev.lock_mode_behavior_flags,
        "door_warning_delay_s": dev.door_warning_delay_s,
        "door_release_delay_s": dev.door_release_delay_s
    }

@router.get("/devices/{id}/whitelist")
def get_whitelist(id: int):
    lock_service.request_wl_list_v2(id)
    dev = can_listener.get_device(id)
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    return dev.whitelist_items

@router.post("/devices/{id}/whitelist")
def add_whitelist(id: int, req: WhitelistWriteReq):
    uid_bytes = bytes.fromhex(req.uid_hex)
    uid_len = len(uid_bytes)
    
    frame1 = wl_write_uid(id, req.slot_index, uid_len, uid_bytes)
    lock_service.send(frame1)
    
    if uid_len > 3:
        frame2 = wl_write_uid_part2(id, uid_bytes, req.ttl_days)
        lock_service.send(frame2)
        
    return {"status": "whitelist added"}

@router.delete("/devices/{id}/whitelist/{slot_index}")
def delete_whitelist_slot(id: int, slot_index: int):
    lock_service.delete_wl_slot(id, slot_index)
    return {"status": "whitelist slot deleted"}

@router.delete("/devices/{id}/whitelist")
def clear_whitelist(id: int):
    lock_service.wl_clear(id)
    return {"status": "whitelist cleared"}

@router.post("/devices/{id}/whitelist/{slot_index}/policy")
def set_policy(id: int, slot_index: int, req: PolicyReq):
    lock_service.set_wl_slot_policy(id, slot_index, req.policy, req.open_action, req.flags)
    return {"status": "policy set"}

@router.post("/devices/{id}/auth_response")
def auth_response(id: int, req: AuthRespReq):
    lock_service.auth_resp(id, req.result, req.action)
    return {"status": "auth response processed"}

@router.post("/devices/{id}/device_reset")
def device_reset(id: int):
    lock_service.device_reset(id)
    return {"status": "device reset"}

@router.post("/devices/{id}/factory_reset")
def factory_reset(id: int):
    lock_service.factory_reset(id)
    return {"status": "factory reset"}

@router.post("/devices/{id}/buzzer_stop")
def buzzer_stop_device(id: int):
    from canbus.commands import buzz_stop
    lock_service.send(buzz_stop(id))
    return {"status": "buzzer stopped"}

@router.post("/devices/{id}/led_reset")
def led_reset_device(id: int):
    from canbus.commands import led_reset
    lock_service.send(led_reset(id))
    return {"status": "led reset"}
