import asyncio
import json
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from config.globals import can_listener, beacon_service

router = APIRouter(tags=["Stream"])

async def event_stream():
    last_device_seen = {}
    last_unassigned_seen = {}
    last_beacon_sent = -1

    while True:
        # Check devices
        for dev_id, dev in list(can_listener.devices.items()):
            if dev_id not in last_device_seen or last_device_seen[dev_id] != dev.last_seen:
                last_device_seen[dev_id] = dev.last_seen
                
                details = {
                    "name": getattr(dev, "name", f"Lock {dev_id}"),
                    "is_locked": dev.is_locked,
                    "is_door_closed": dev.is_door_closed,
                    "status_text": dev.status_text,
                    "error_text": dev.error_text,
                    "last_scanned_card": dev.last_scanned_card,
                    "door_close_guard_supported": dev.door_close_guard_supported
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
                    "online": dev.online,
                    "details": details,
                    "health": health
                }
                yield f"event: device_update\ndata: {json.dumps(data)}\n\n"

        # Check unassigned
        current_unassigned = can_listener.get_unassigned_devices()
        unassigned_changed = False
        if len(current_unassigned) != len(last_unassigned_seen):
            unassigned_changed = True
        else:
            for d in current_unassigned:
                if d.uid32_hex not in last_unassigned_seen or last_unassigned_seen[d.uid32_hex] != d.last_seen:
                    unassigned_changed = True
                    break
        
        if unassigned_changed:
            last_unassigned_seen = {d.uid32_hex: d.last_seen for d in current_unassigned}
            result = []
            for d in current_unassigned:
                if hasattr(d, "to_dict"):
                    result.append(d.to_dict())
                else:
                    uid_int = int(d, 16) if d else 0
                    result.append({"uid32_hex": d, "uid32": uid_int})
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

        await asyncio.sleep(0.2)

@router.get("/stream")
async def stream():
    return StreamingResponse(event_stream(), media_type="text/event-stream")
