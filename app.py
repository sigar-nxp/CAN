"""
Main application entry point for the PS Locks Open Integration Platform.

Initializes the FastAPI application, sets up routing, ensures database
tables are created, and manages background service lifecycles (CAN bus
listener and Master Beacon) via startup/shutdown events.
"""

import asyncio
from datetime import datetime
import platform
import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from database.database import Base, engine
from api.device import router as devices_router
from api.system import router as system_router
from config.globals import can_service, can_listener, beacon_service
from api.stream import router as stream_router
from routers.ota import router as ota_router


# Import models to ensure they are registered with Base.metadata
from models.device import Device
from models.log import EventLog
from api.logs import router as logs_router

# Create database tables
Base.metadata.create_all(bind=engine)


app = FastAPI(
    title="PS Locks Open Integration Platform",
    description="Open Integration Platform für Zutrittskontrolle und Gebäudeautomation",
    version="0.3.0"
)

@app.get("/")
def root(request: Request):
    if "text/html" in request.headers.get("accept", ""):
        template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
        if os.path.exists(template_path):
            with open(template_path, "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
    return {
        "project": "PS Locks OIP",
        "status": "online"
    }


@app.on_event("startup")
def startup():
    """
    Startup event handler for FastAPI.
    
    Initializes the centralized CANManager with the running event loop,
    starts the background reader, device listener, and Master Beacon.
    """
    print("PS Locks OIP gestartet")
    try:
        loop = asyncio.get_running_loop()
        can_service.set_loop(loop)
    except RuntimeError:
        pass
    can_service.start()
    can_listener.start()
    beacon_service.start()

@app.on_event("shutdown")
def shutdown():
    """
    Shutdown event handler for FastAPI.
    
    Gracefully stops the CAN listener, the Master Beacon background
    service, and the central CAN manager.
    """
    can_listener.stop()
    beacon_service.stop()
    can_service.stop()


app.include_router(
    devices_router,
    prefix="/api/v1",
    tags=["Devices"]
)

app.include_router(
    system_router,
    prefix="/api/v1"
)

app.include_router(
    stream_router,
    prefix="/api/v1"
)

app.include_router(
    logs_router,
    prefix="/api/v1",
    tags=["Logs"]
)

app.include_router(
    ota_router
)
