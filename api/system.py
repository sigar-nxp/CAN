"""
FastAPI endpoints for system-level and gateway diagnostics.

Provides REST APIs for querying system health, gateway version information,
active hardware interface details, and managing global behaviors like the
master beacon broadcast.
"""


from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from datetime import datetime
import platform
import os

from config.globals import beacon_service

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