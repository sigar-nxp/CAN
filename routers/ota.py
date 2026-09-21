"""
PS Locks OIP OTA Update Router.

Provides endpoints for unified firmware bundle (.ota / .zip) ingestion,
binary staging, single-device OTA flashing, sequential batch updates,
and real-time status polling.
"""

import binascii
import json
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional
import zipfile

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel

from config.globals import can_listener
from database.database import SessionLocal
from models.device import Device
from services.ota_service import ota_service

router = APIRouter(tags=["OTA"])

UPLOAD_DIRECTORY = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads")
RELEASES_DIRECTORY = os.path.join(UPLOAD_DIRECTORY, "releases")
LATEST_RELEASE_FILE = os.path.join(RELEASES_DIRECTORY, "latest_release.json")

os.makedirs(UPLOAD_DIRECTORY, exist_ok=True)
os.makedirs(RELEASES_DIRECTORY, exist_ok=True)


class StartOtaPayload(BaseModel):
    binary_path: Optional[str] = None


class BatchOtaPayload(BaseModel):
    device_ids: Optional[List[int]] = None
    binary_path: Optional[str] = None


def parse_crc32(val: Any) -> int:
    """Parses CRC32 checksum from integer or hexadecimal string."""
    if isinstance(val, int):
        return val & 0xFFFFFFFF
    if isinstance(val, str):
        val_str = val.strip()
        if val_str.lower().startswith("0x"):
            return int(val_str, 16) & 0xFFFFFFFF
        return int(val_str, 16) & 0xFFFFFFFF
    raise ValueError(f"Invalid CRC32 representation: {val}")


def get_active_release_info() -> Optional[Dict[str, Any]]:
    """Reads latest_release.json if it exists."""
    if os.path.isfile(LATEST_RELEASE_FILE):
        try:
            with open(LATEST_RELEASE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _resolve_binary_path(binary_path: Optional[str]) -> str:
    """Resolves relative, release-managed, or uploaded binary path."""
    if not binary_path or binary_path.strip().lower() == "auto":
        rel = get_active_release_info()
        if rel and "binaries" in rel and "slot_a" in rel["binaries"]:
            slot_a_path = rel["binaries"]["slot_a"].get("path")
            if slot_a_path and os.path.exists(slot_a_path):
                return slot_a_path

        default_path = os.path.join(UPLOAD_DIRECTORY, "slot_a.bin")
        if os.path.exists(default_path):
            return default_path
        alt_path = os.path.join(os.path.dirname(UPLOAD_DIRECTORY), "slot_a.bin")
        if os.path.exists(alt_path):
            return alt_path
        raise HTTPException(status_code=400, detail="No binary_path specified and no default slot_a.bin found.")

    if os.path.exists(binary_path):
        return binary_path

    candidate = os.path.join(UPLOAD_DIRECTORY, os.path.basename(binary_path))
    if os.path.exists(candidate):
        return candidate

    raise HTTPException(status_code=404, detail=f"Firmware binary file not found: {binary_path}")

def _process_ota_bundle(file: UploadFile) -> Dict[str, Any]:
    """Extracts, validates, and stages an .ota firmware release package."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ota") as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        if not zipfile.is_zipfile(tmp_path):
            raise HTTPException(status_code=400, detail="Uploaded bundle is not a valid zip archive.")

        with zipfile.ZipFile(tmp_path, "r") as zf:
            file_list = zf.namelist()
            if "manifest.json" not in file_list:
                raise HTTPException(status_code=400, detail="Missing manifest.json in firmware package.")

            try:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            except Exception as ex:
                raise HTTPException(status_code=400, detail=f"Failed to parse manifest.json: {ex}")

            version = manifest.get("version")
            if not version:
                raise HTTPException(status_code=400, detail="manifest.json missing 'version' field.")

            target_mcu = manifest.get("target_mcu", "STM32C092")
            binaries_meta = manifest.get("binaries", {})
            if "slot_a" not in binaries_meta or "slot_b" not in binaries_meta:
                raise HTTPException(status_code=400, detail="manifest.json must have 'slot_a' and 'slot_b'.")

            slot_a_info = binaries_meta["slot_a"]
            slot_b_info = binaries_meta["slot_b"]
            slot_a_fn = slot_a_info.get("filename", "slot_a.bin")
            slot_b_fn = slot_b_info.get("filename", "slot_b.bin")

            if slot_a_fn not in file_list or slot_b_fn not in file_list:
                raise HTTPException(status_code=400, detail="Bundle missing required slot binary files.")

            raw_slot_a = zf.read(slot_a_fn)
            raw_slot_b = zf.read(slot_b_fn)
            crc_a = binascii.crc32(raw_slot_a) & 0xFFFFFFFF
            crc_b = binascii.crc32(raw_slot_b) & 0xFFFFFFFF

            exp_crc_a = parse_crc32(slot_a_info.get("crc32_int", slot_a_info.get("crc32")))
            exp_crc_b = parse_crc32(slot_b_info.get("crc32_int", slot_b_info.get("crc32")))

            if crc_a != exp_crc_a:
                raise HTTPException(status_code=400, detail=f"Slot A CRC mismatch: 0x{exp_crc_a:08X} != 0x{crc_a:08X}")
            if crc_b != exp_crc_b:
                raise HTTPException(status_code=400, detail=f"Slot B CRC mismatch: 0x{exp_crc_b:08X} != 0x{crc_b:08X}")

            clean_ver = version if str(version).startswith("v") else f"v{version}"
            ver_dir = os.path.join(RELEASES_DIRECTORY, clean_ver)
            os.makedirs(ver_dir, exist_ok=True)

            slot_a_path = os.path.join(ver_dir, "slot_a.bin")
            slot_b_path = os.path.join(ver_dir, "slot_b.bin")

            with open(slot_a_path, "wb") as f_a:
                f_a.write(raw_slot_a)
            with open(slot_b_path, "wb") as f_b:
                f_b.write(raw_slot_b)
            with open(os.path.join(ver_dir, "manifest.json"), "w", encoding="utf-8") as f_m:
                json.dump(manifest, f_m, indent=2)

            shutil.copy2(slot_a_path, os.path.join(UPLOAD_DIRECTORY, "slot_a.bin"))
            shutil.copy2(slot_b_path, os.path.join(UPLOAD_DIRECTORY, "slot_b.bin"))

            release_record = {
                "version": clean_ver,
                "raw_version": str(version),
                "target_mcu": target_mcu,
                "timestamp": manifest.get("timestamp"),
                "release_dir": ver_dir,
                "binaries": {
                    "slot_a": {"filename": "slot_a.bin", "path": slot_a_path, "crc32": f"0x{crc_a:08X}", "size": len(raw_slot_a)},
                    "slot_b": {"filename": "slot_b.bin", "path": slot_b_path, "crc32": f"0x{crc_b:08X}", "size": len(raw_slot_b)},
                },
                "manifest": manifest,
            }
            with open(LATEST_RELEASE_FILE, "w", encoding="utf-8") as f_rel:
                json.dump(release_record, f_rel, indent=2)

            return {
                "status": "staged",
                "type": "bundle",
                "version": clean_ver,
                "target_mcu": target_mcu,
                "release_dir": ver_dir,
                "filename": file.filename or "package.ota",
                "slot_a": {"path": slot_a_path, "size": len(raw_slot_a), "crc32": f"0x{crc_a:08X}"},
                "slot_b": {"path": slot_b_path, "size": len(raw_slot_b), "crc32": f"0x{crc_b:08X}"},
            }
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)




@router.post("/api/ota/upload")
@router.post("/api/v1/ota/upload")
async def upload_firmware(file: UploadFile = File(...)) -> Dict[str, Any]:
    """
    Stages a firmware binary file or processes a unified .ota / .zip release package.
    """
    filename = file.filename or "firmware.bin"
    if filename.lower().endswith((".ota", ".zip")):
        return _process_ota_bundle(file)

    destination_path = os.path.join(UPLOAD_DIRECTORY, filename)
    try:
        with open(destination_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        file_size = os.path.getsize(destination_path)
        return {
            "status": "staged",
            "filename": filename,
            "path": destination_path,
            "size": file_size,
        }
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"Failed to save binary: {ex}")


@router.get("/api/ota/release")
@router.get("/api/v1/ota/release")
async def get_latest_release() -> Dict[str, Any]:
    """Retrieves metadata of current active firmware release package."""
    release = get_active_release_info()
    if not release:
        return {
            "has_release": False,
            "version": None,
            "message": "No firmware release package uploaded yet.",
        }
    return {
        "has_release": True,
        "version": release.get("version"),
        "raw_version": release.get("raw_version"),
        "target_mcu": release.get("target_mcu"),
        "timestamp": release.get("timestamp"),
        "release_dir": release.get("release_dir"),
        "binaries": release.get("binaries"),
    }



@router.post("/api/devices/{device_id}/ota/start")
@router.post("/api/v1/devices/{device_id}/ota/start")
async def start_device_ota(
    device_id: int,
    payload: Optional[StartOtaPayload] = None,
    binary_path: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Triggers an asynchronous background OTA update for a target lock node."""
    if ota_service.is_task_running(device_id):
        current_status = ota_service.get_status(device_id)
        raise HTTPException(
            status_code=409,
            detail=f"OTA update already active for device {device_id} (state: {current_status['state']})",
        )

    target_path = binary_path or (payload.binary_path if payload else None)
    try:
        resolved_path = ota_service.resolve_target_binary(device_id, target_path)
    except FileNotFoundError as ex:
        raise HTTPException(status_code=404, detail=str(ex))

    task_id = ota_service.start_ota_task(device_id, resolved_path)
    return {
        "status": "started",
        "task_id": task_id,
        "device_id": device_id,
        "binary_path": resolved_path,
    }


@router.get("/api/devices/{device_id}/ota/status")
@router.get("/api/v1/devices/{device_id}/ota/status")
async def get_device_ota_status(device_id: int) -> Dict[str, Any]:
    """Retrieves real-time progress and status of an OTA update with slot metadata."""
    status = dict(ota_service.get_status(device_id))
    dev = can_listener.get_device(device_id)
    active_slot = getattr(dev, "active_slot", 0) or 0
    target_slot = 1 if active_slot == 0 else 0
    target_binary = "slot_b.bin" if active_slot == 0 else "slot_a.bin"
    status["active_slot"] = active_slot
    status["target_slot"] = target_slot
    status["target_binary"] = target_binary
    return status


@router.post("/api/ota/batch/start")
@router.post("/api/v1/ota/batch/start")
async def start_batch_ota(
    payload: Optional[BatchOtaPayload] = None,
    binary_path: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Enqueues all configured locks sequentially for firmware flashing."""
    if ota_service.is_batch_running():
        raise HTTPException(
            status_code=409,
            detail="Batch OTA update is already running.",
        )

    target_path = binary_path or (payload.binary_path if payload else None)
    resolved_path = target_path or "auto"

    device_ids: List[int] = []
    if payload and payload.device_ids:
        device_ids = payload.device_ids
    else:
        with SessionLocal() as db:
            db_devices = db.query(Device.device_id).all()
            db_ids = [d[0] for d in db_devices]

        cached_ids = list(can_listener.devices.keys())
        device_ids = sorted(list(set(db_ids + cached_ids)))

    if not device_ids:
        raise HTTPException(status_code=400, detail="No locks configured or discovered for batch update.")

    task_id = ota_service.start_batch_ota_task(device_ids, resolved_path)
    return {
        "status": "batch_started",
        "task_id": task_id,
        "target_count": len(device_ids),
        "device_ids": device_ids,
        "binary_path": resolved_path,
    }


@router.get("/api/ota/batch/status")
@router.get("/api/v1/ota/batch/status")
async def get_batch_ota_status() -> Dict[str, Any]:
    """Retrieves real-time status of the sequential batch OTA flashing sequence."""
    return ota_service.get_batch_status()


@router.post("/api/devices/{device_id}/ota/reset")
@router.post("/api/v1/devices/{device_id}/ota/reset")
async def reset_device_ota_status(device_id: int) -> Dict[str, Any]:
    """Resets the in-memory OTA update status for a device back to IDLE."""
    ota_service.reset_status(device_id)
    return {"status": "reset", "device_id": device_id}

