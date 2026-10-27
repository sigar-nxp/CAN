import asyncio
import json
from typing import Any, Dict, Tuple
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from config.globals import can_listener, beacon_service

router = APIRouter(tags=["Stream"])


def extract_unassigned_devices(raw_unassigned: Any) -> Dict[str, Tuple[float, Dict[str, Any]]]:
    """
    Extracts and normalizes unassigned devices from various possible container types
    (dict of dicts/objects/timestamps, list of objects/dicts/strings, set, etc.)
    into a dictionary mapping uid32_hex -> (last_seen, dict_payload).
    """
    normalized: Dict[str, Tuple[float, Dict[str, Any]]] = {}
    if not raw_unassigned:
        return normalized

    if isinstance(raw_unassigned, dict):
        items = list(raw_unassigned.items())
    elif isinstance(raw_unassigned, (list, set, tuple)):
        items = [(None, item) for item in raw_unassigned]
    else:
        try:
            items = [(None, item) for item in iter(raw_unassigned)]
        except TypeError:
            items = []

    for key, val in items:
        # Determine uid32_hex, last_seen, and base dict representation
        if hasattr(val, "to_dict"):
            d_dict = val.to_dict()
            uid_hex = getattr(val, "uid32_hex", None) or d_dict.get("uid32_hex") or (str(key) if key is not None else "")
            last_seen = getattr(val, "last_seen", None) or d_dict.get("last_seen", 0.0)
        elif isinstance(val, dict):
            d_dict = dict(val)
            uid_hex = d_dict.get("uid32_hex") or (str(key) if key is not None else "")
            last_seen = d_dict.get("last_seen", 0.0)
        elif isinstance(val, (int, float)) and key is not None:
            uid_hex = f"{key:08X}" if isinstance(key, int) else str(key)
            last_seen = float(val)
            d_dict = {"uid32_hex": uid_hex, "last_seen": last_seen}
        elif isinstance(val, str):
            uid_hex = val
            last_seen = 0.0
            d_dict = {"uid32_hex": uid_hex, "last_seen": last_seen}
        elif isinstance(val, int):
            uid_hex = f"{val:08X}"
            last_seen = 0.0
            d_dict = {"uid32_hex": uid_hex, "last_seen": last_seen}
        else:
            uid_hex = str(key if key is not None else val)
            last_seen = getattr(val, "last_seen", 0.0)
            d_dict = {"uid32_hex": uid_hex, "last_seen": last_seen}

        # Normalize uid32 integer representation for frontend compatibility
        d_dict["uid32_hex"] = uid_hex
        if "uid32" not in d_dict:
            try:
                d_dict["uid32"] = int(uid_hex, 16) if uid_hex else 0
            except (ValueError, TypeError):
                d_dict["uid32"] = 0
        if "last_seen" not in d_dict:
            d_dict["last_seen"] = last_seen

        normalized[uid_hex] = (last_seen, d_dict)

    return normalized


stream_subscribers = set()


def broadcast_sse_event(event: str, data: dict):
    """Broadcast an SSE event directly to all active event_stream subscribers."""
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    for q in list(stream_subscribers):
        try:
            q.put_nowait(msg)
        except Exception:
            pass


async def event_stream(request: Request):
    subscriber_queue = asyncio.Queue()
    stream_subscribers.add(subscriber_queue)
    last_device_seen = {}
    last_device_online = {}
    last_unassigned_seen = {}
    last_beacon_sent = -1
    last_log_idx = 0

    try:
        while can_listener.running and not await request.is_disconnected():
            # Drain any direct broadcast events
            while not subscriber_queue.empty():
                try:
                    yield subscriber_queue.get_nowait()
                except Exception:
                    break

            # Check devices
            current_device_ids = set(can_listener.devices.keys())
            # Detect removed devices
            for dev_id in list(last_device_seen.keys()):
                if dev_id not in current_device_ids:
                    del last_device_seen[dev_id]
                    last_device_online.pop(dev_id, None)
                    yield f"event: device_removed\ndata: {json.dumps({'device_id': dev_id})}\n\n"

            for dev_id, dev in list(can_listener.devices.items()):
                is_online = dev.online
                if (dev_id not in last_device_seen 
                    or last_device_seen[dev_id] != dev.last_seen 
                    or last_device_online.get(dev_id) != is_online):
                    
                    last_device_seen[dev_id] = dev.last_seen
                    last_device_online[dev_id] = is_online
                    
                    details = {
                        "name": getattr(dev, "name", f"Lock {dev_id}"),
                        "is_locked": dev.is_locked,
                        "is_door_closed": dev.is_door_closed,
                        "status_text": dev.status_text,
                        "error_text": dev.error_text,
                        "last_scanned_card": dev.last_scanned_card,
                        "door_close_guard_supported": dev.door_close_guard_supported,
                        "hardware_uid": dev.hardware_uid,
                        "full_uid": dev.full_uid,
                    }
                    health = {
                        "health_short": dev.health_short,
                        "health_extended": dev.health_extended,
                        "health_diag_info": dev.health_diag_info,
                        "runtime_state": dev.runtime_state,
                        "diag_extended": dev.diag_extended
                    }
                    data = {
                        "device_id": dev_id,
                        "online": is_online,
                        "is_online": is_online,
                        "status": "OFFLINE" if not is_online else dev.status_text,
                        "details": details,
                        "health": health
                    }
                    yield f"event: device_update\ndata: {json.dumps(data)}\n\n"

            # Check unassigned
            raw_unassigned = can_listener.get_unassigned_devices()
            normalized = extract_unassigned_devices(raw_unassigned)
            current_seen = {uid: info[0] for uid, info in normalized.items()}

            if current_seen != last_unassigned_seen:
                last_unassigned_seen = current_seen
                result = [info[1] for info in normalized.values()]
                yield f"event: unassigned_update\ndata: {json.dumps(result)}\n\n"

            # Check beacon
            if beacon_service.total_beacons_sent != last_beacon_sent:
                last_beacon_sent = beacon_service.total_beacons_sent
                beacon_data = {
                    "running": beacon_service.running,
                    "interval_sec": beacon_service.interval_sec,
                    "total_beacons_sent": beacon_service.total_beacons_sent
                }
                yield f"event: beacon_update\ndata: {json.dumps(beacon_data)}\n\n"

            # Check logs
            current_logs = can_listener.recent_logs
            if last_log_idx == 0 and current_logs:
                # First iteration, just catch up to the latest log
                last_log_idx = current_logs[-1]["id"]
            elif current_logs:
                for log in current_logs:
                    if log["id"] > last_log_idx:
                        log_data = dict(log)
                        if "direction" not in log_data or not log_data["direction"]:
                            log_data["direction"] = "RX"
                        if "corrId" not in log_data and "corr_id" in log_data:
                            log_data["corrId"] = log_data["corr_id"]
                        if "command" not in log_data:
                            log_data["command"] = log_data.get("event_type")
                        yield f"event: log_entry\ndata: {json.dumps(log_data)}\n\n"
                        last_log_idx = log["id"]

            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        pass
    finally:
        stream_subscribers.discard(subscriber_queue)

@router.get("/stream")
async def stream(request: Request):
    return StreamingResponse(event_stream(request), media_type="text/event-stream")

