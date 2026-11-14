"""
FastAPI endpoints for system-level and gateway diagnostics.

Provides REST APIs for querying system health, gateway version information,
active hardware interface details, and managing global behaviors like the
master beacon broadcast.
"""


from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from datetime import datetime
import platform

from config import settings
from config.globals import beacon_service, can_listener
from api.stream import broadcast_sse_event

router = APIRouter(tags=["System"])


@router.get("/health")
def health():
    return {
        "status": "OK",
        "time": datetime.now().isoformat()
    }


@router.get("/version")
def version():
    return {
        "version": "0.3.0"
    }


@router.get("/info")
def info():
    return {
        "hostname": platform.node(),
        "system": platform.system(),
        "python": platform.python_version()
    }


@router.get("/system/beacon")
def get_beacon_status():
    """Return current status of the Master Beacon."""
    return {
        "running": beacon_service.running,
        "interval_sec": beacon_service.interval_sec,
        "total_beacons_sent": beacon_service.total_beacons_sent
    }


@router.post("/system/beacon/toggle")
def toggle_beacon():
    """Enable or disable the Master Beacon dynamically."""
    if beacon_service.running:
        beacon_service.stop()
    else:
        beacon_service.start()
    return {
        "running": beacon_service.running,
        "interval_sec": beacon_service.interval_sec,
        "total_beacons_sent": beacon_service.total_beacons_sent
    }


class LockerModeReq(BaseModel):
    mode: str


def _locker_mode_payload() -> dict:
    return {
        "mode": settings.get_locker_operation_mode(),
        "available_modes": list(settings.VALID_LOCKER_MODES),
        "staging_node_id": settings.STAGING_NODE_ID,
        "node_id_range": [settings.GYM_NODE_ID_MIN, settings.GYM_NODE_ID_MAX],
    }


@router.get("/system/locker-mode")
def get_locker_mode():
    """Return the active locker operation mode ("gym" or "standard")."""
    return _locker_mode_payload()


@router.put("/system/locker-mode")
def set_locker_mode(req: LockerModeReq):
    """
    Switch the locker operation mode at runtime and persist it in SQLite.
    """
    try:
        settings.set_locker_operation_mode(req.mode, persist=True)
        if settings.is_gym_mode():
            can_listener.purge_staging_node()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    payload = _locker_mode_payload()
    broadcast_sse_event("locker_mode_update", payload)
    return payload
