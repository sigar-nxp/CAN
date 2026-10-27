"""
PS Locks OIP CAN Listener.

Implements a background polling loop to continuously listen for CAN messages.
Dispatches messages to the parser and updates an in-memory dictionary of
device states tracking properties like lock status, telemetry, and health.
"""


import os
import threading
import time
import queue
from typing import Dict, Any, Optional
from datetime import datetime
import logging


from database.database import SessionLocal
from models.log import EventLog
from models.device import Device


from .protocol import CANFrame
from .service import CANService
from .constants import COMMAND_NAMES
from .parser import CANParser, StatusBasic, HealthStatus, StatusError, CommandAck, IdRequest, UIDPart1, UIDPart2, StatusRFID, StatusRFIDPart2, EventWlAutoDelete, HealthVersionInfo, HealthDeviceInfo, HealthShort, HealthExtended, HealthDiagInfo, HealthSecurityStatus, WlInfoReport, LockModeReport, WlListV2Item, WlListV2UidPart1, WlListV2UidPart2, OccupancyStateReport, OccupancyOwnerShort, PolicyActionReport, RuntimeStateSnapshot, DiagExtended
logger = logging.getLogger(__name__)



ERROR_CODES = {
    0x00: "NONE",
    0x02: "INVALID_PARAM",
    0x03: "UNSUPPORTED_CMD",
    0x04: "DOOR_GUARD_ACTIVE",
    0x06: "SLOT_OUT_OF_RANGE",
    0x0D: "DOOR_GUARD_TIMEOUT"
}

def normalize_uid(uid: Any) -> Optional[str]:
    """Normalizes a hardware UID (int, bytes, or hex string) to a lowercase hex string."""
    if uid is None:
        return None
    if isinstance(uid, bytes):
        return uid.hex().lower()
    if isinstance(uid, int):
        return f"{uid:08x}".lower()
    s = str(uid).strip().replace(" ", "").replace(":", "").lower()
    if s.startswith("0x"):
        s = s[2:]
    return s if s else None


def uids_match(uid1: Any, uid2: Any) -> bool:
    """Returns True if two UIDs match either exactly or as prefix (e.g. 32-bit vs full UID)."""
    u1 = normalize_uid(uid1)
    u2 = normalize_uid(uid2)
    if not u1 or not u2:
        return False
    return u1 == u2 or u1.startswith(u2) or u2.startswith(u1)


class UnassignedDevice:
    def __init__(self, uid32: bytes):
        self.uid32 = uid32
        self.uid32_hex = uid32.hex()
        self.last_seen = time.time()
        self.full_uid = b""

    def to_dict(self):
        return {
            "uid32_hex": self.uid32_hex,
            "last_seen": self.last_seen,
            "full_uid_hex": self.full_uid.hex() if self.full_uid else None
        }

class DeviceState:
    def __init__(self, device_id: int, offline_timeout: float = 30.0):
        self.device_id = device_id
        self.name = f"Lock {device_id}"
        self.lock_state = None
        self.lock_error = 0
        self.last_seen = 0.0
        self.offline_timeout = offline_timeout
        self._is_offline_override: bool = False
        self.is_unprovisioned: bool = False
        self.hardware_uid: Optional[str] = None
        self.health_data = None
        self.last_ack_command = None
        self.last_ack_time = 0.0

        # Decoded helper fields
        self.is_locked: Optional[bool] = None
        self.is_door_closed: Optional[bool] = None
        self.status_text: str = "UNKNOWN"
        self.error_text: str = "NONE"
        self.firmware_version: Optional[str] = None
        self.hardware_version: Optional[str] = None
        self.full_uid: Optional[str] = None
        self.last_scanned_card: Optional[str] = None

        # Whitelist & Telemetry state
        self.whitelist_info: Dict[str, Any] = {}
        self.whitelist_items: Dict[int, Dict[str, Any]] = {}
        self.lock_mode: int = 1
        self.auto_close_timeout: int = 3
        self.occupancy_state: Dict[str, Any] = {}

        self.health_short: Dict[str, Any] = {}
        self.health_extended: Dict[str, Any] = {}
        self.health_diag_info: Dict[str, Any] = {}
        self.health_security_status: Dict[str, Any] = {}
        self._partial_rfid = None

        self.door_close_guard_supported: bool = False
        self.lock_mode_behavior_flags: int = 0
        self.door_warning_delay_s: int = 2
        self.door_release_delay_s: int = 5
        self.active_slot: int = 0
        self.runtime_state: Dict[str, Any] = {}
        self.diag_extended: Dict[str, Any] = {}

    @property
    def online(self) -> bool:
        if self._is_offline_override:
            return False
        if self.last_seen <= 0.0:
            return False
        return (time.time() - self.last_seen) < self.offline_timeout

    @property
    def is_online(self) -> bool:
        return self.online

    @is_online.setter
    def is_online(self, val: bool):
        if not val:
            self._is_offline_override = True
        else:
            self._is_offline_override = False
            if self.last_seen <= 0.0:
                self.last_seen = time.time()

    def mark_seen(self, timestamp: Optional[float] = None):
        """Records a successful heartbeat/telemetry reception and marks device online."""
        self.last_seen = timestamp or time.time()
        self._is_offline_override = False
        self.is_unprovisioned = False
        if self.status_text in ("OFFLINE", "UNPROVISIONED"):
            if self.lock_state is not None:
                self.update_lock_state(self.lock_state)
            else:
                self.status_text = "UNKNOWN"

    def mark_offline(self, unprovisioned: bool = False):
        """Marks device as offline or unprovisioned."""
        self._is_offline_override = True
        self.last_seen = 0.0
        if unprovisioned:
            self.is_unprovisioned = True
            self.status_text = "UNPROVISIONED"
        else:
            self.status_text = "OFFLINE"

    def update_lock_state(self, state: int):
        self.lock_state = state
        if state == 1:
            self.is_locked = True
            self.is_door_closed = True
            self.status_text = "LOCKED"
        elif state == 2:
            self.is_locked = False
            self.is_door_closed = True
            self.status_text = "UNLOCKED"
        elif state == 3:
            self.is_locked = True
            self.is_door_closed = False
            self.status_text = "LOCKED_DOOR_OPEN"
        elif state == 4:
            self.is_locked = False
            self.is_door_closed = False
            self.status_text = "UNLOCKED_DOOR_OPEN"
        elif state == 5:
            self.is_locked = False
            self.is_door_closed = None
            self.status_text = "OPEN_PULSE"
        elif state == 6:
            self.is_locked = False
            self.is_door_closed = False
            self.status_text = "OPEN_HOLD_DO"
        elif state == 7:
            self.is_locked = False
            self.is_door_closed = True
            self.status_text = "OPEN_HOLD_DC"
        else:
            self.is_locked = None
            self.is_door_closed = None
            self.status_text = "UNKNOWN"

    def update_error(self, error_code: int):
        self.lock_error = error_code
        self.error_text = ERROR_CODES.get(error_code, f"ERROR_{error_code}")

    def to_dict(self):
        return {
            "device_id": self.device_id,
            "name": self.name,
            "lock_state": self.lock_state,
            "lock_error": self.lock_error,
            "last_seen": self.last_seen,
            "health_data": self.health_data.hex() if self.health_data else None,
            "last_ack_command": self.last_ack_command,
            "last_ack_time": self.last_ack_time,
            "online": self.online,
            "is_online": self.online,
            "status": "OFFLINE" if not self.online else self.status_text,
            "is_locked": self.is_locked,
            "is_door_closed": self.is_door_closed,
            "status_text": self.status_text,
            "error_text": self.error_text,
            "firmware_version": self.firmware_version,
            "hardware_version": self.hardware_version,
            "hardware_uid": self.hardware_uid,
            "full_uid": self.full_uid,
            "last_scanned_card": self.last_scanned_card,
            "whitelist_info": self.whitelist_info,
            "whitelist_items": self.whitelist_items,
            "lock_mode": self.lock_mode,
            "auto_close_timeout": self.auto_close_timeout,
            "occupancy_state": self.occupancy_state,
            "active_slot": self.active_slot,
            "is_unprovisioned": self.is_unprovisioned,
        }


class CANListener:
    def __init__(self, can_service: CANService, offline_timeout: float = 30.0):
        self.can = can_service
        self.parser = CANParser()
        self.running = False
        self.thread = None
        self.devices: Dict[int, DeviceState] = {}
        self.unassigned_devices: Dict[str, UnassignedDevice] = {}
        self.latest_uid_part1: Optional[bytes] = None
        self.latest_uid_part2: Optional[bytes] = None
        self.lock = threading.RLock()
        
        self.offline_timeout: float = float(os.environ.get("LOCK_OFFLINE_TIMEOUT", offline_timeout))
        self.janitor_thread = None

        self.log_queue = queue.Queue()
        self.recent_logs = []
        self.log_thread = None

        if hasattr(self.can, "set_log_handler"):
            self.can.set_log_handler(self.add_log)

    def _persist_log_entry(self, db, item: Dict[str, Any]):
        log_entry = EventLog(**item)
        db.add(log_entry)
        db.commit()
        db.refresh(log_entry)
        
        log_dict = {
            "id": log_entry.id,
            "timestamp": log_entry.timestamp.isoformat() + "Z",
            "device_id": log_entry.device_id,
            "device_name": log_entry.device_name,
            "event_type": log_entry.event_type,
            "card_uid": log_entry.card_uid,
            "details": log_entry.details,
            "direction": getattr(log_entry, "direction", "RX") or item.get("direction", "RX"),
            "can_id": getattr(log_entry, "can_id", None) or item.get("can_id"),
            "payload": getattr(log_entry, "payload", None) or item.get("payload"),
            "corr_id": getattr(log_entry, "corr_id", None) or item.get("corr_id"),
            "corrId": getattr(log_entry, "corr_id", None) or item.get("corr_id"),
        }
        with self.lock:
            self.recent_logs.append(log_dict)
            if len(self.recent_logs) > 200:
                self.recent_logs = self.recent_logs[-100:]
        return log_entry

    def _log_worker(self):
        with SessionLocal() as db:
            while self.running:
                try:
                    item = self.log_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is None:
                    break
                try:
                    self._persist_log_entry(db, item)
                except Exception as e:
                    logger.error(f"Log worker error: {e}")
                    db.rollback()
            # Drain remaining items if any upon shutdown
            while not self.log_queue.empty():
                try:
                    item = self.log_queue.get_nowait()
                    if item is not None:
                        self._persist_log_entry(db, item)
                except Exception:
                    break

    def add_log(
        self,
        device_id: int,
        event_type: str,
        card_uid: Optional[str] = None,
        details: Optional[str] = None,
        direction: str = "RX",
        can_id: Optional[str] = None,
        payload: Optional[str] = None,
        corr_id: Optional[int] = None,
    ):
        device_name = f"Lock {device_id}"
        if device_id in self.devices:
            device_name = self.devices[device_id].name
        item = {
            "device_id": device_id,
            "device_name": device_name,
            "event_type": event_type,
            "card_uid": card_uid,
            "details": details,
            "direction": direction,
            "can_id": can_id,
            "payload": payload,
            "corr_id": corr_id,
        }
        if self.running and self.log_thread and self.log_thread.is_alive():
            self.log_queue.put(item)
        else:
            try:
                with SessionLocal() as db:
                    self._persist_log_entry(db, item)
            except Exception as e:
                logger.error(f"Direct log persist error: {e}")

    def set_offline_timeout(self, seconds: float):
        """Updates the offline timeout across listener and active device states."""
        with self.lock:
            self.offline_timeout = seconds
            for dev in self.devices.values():
                dev.offline_timeout = seconds

    def check_offline_devices(self) -> None:
        """Janitor task: marks devices that have exceeded heartbeat/telemetry timeout as OFFLINE."""
        with self.lock:
            now = time.time()
            for dev in self.devices.values():
                if dev.last_seen > 0.0 and (now - dev.last_seen) >= dev.offline_timeout:
                    if not dev._is_offline_override:
                        dev.mark_offline(unprovisioned=False)

    def _janitor_worker(self):
        """Background thread monitoring device heartbeats and transitions to OFFLINE."""
        while self.running:
            try:
                self.check_offline_devices()
            except Exception as e:
                logger.error(f"Janitor worker error: {e}")
            time.sleep(1.0)

    def remove_device(self, device_id: int) -> bool:
        """Removes a device from memory and database to eliminate ghost nodes. Returns True if deleted."""
        found = False
        with self.lock:
            if device_id in self.devices:
                del self.devices[device_id]
                found = True

        try:
            with SessionLocal() as db:
                db_dev = db.query(Device).filter(Device.device_id == device_id).first()
                if db_dev:
                    db.delete(db_dev)
                    db.commit()
                    found = True
        except Exception as e:
            logger.error(f"Error removing device {device_id} from DB: {e}")

        return found

    def prune_offline_devices(self) -> list:
        """
        Removes all devices that are currently offline from memory and SQLite database.
        Returns the list of pruned device IDs.
        """
        self.check_offline_devices()

        offline_ids = []
        with self.lock:
            for dev_id, dev in list(self.devices.items()):
                if not dev.online:
                    offline_ids.append(dev_id)

        try:
            with SessionLocal() as db:
                db_devs = db.query(Device).all()
                for db_d in db_devs:
                    if db_d.device_id not in self.devices:
                        if db_d.device_id not in offline_ids:
                            offline_ids.append(db_d.device_id)
                    else:
                        dev = self.devices[db_d.device_id]
                        if not dev.online and db_d.device_id not in offline_ids:
                            offline_ids.append(db_d.device_id)
        except Exception as e:
            logger.error(f"Error querying DB for offline prune: {e}")

        for dev_id in offline_ids:
            self.remove_device(dev_id)
            self.add_log(
                dev_id,
                "DEVICE_PRUNED",
                None,
                f"Offline Lock {dev_id} pruned during bulk cleanup",
                direction="INTERNAL"
            )

        return offline_ids

    def deduplicate_uid(self, hardware_uid: str, target_device_id: int) -> Optional[int]:
        """
        Checks if hardware_uid is already associated with an existing registered device ID
        other than target_device_id. If found, removes the old device entry to prevent ghost nodes.
        Returns the old device_id if one was removed, or None.
        """
        norm_uid = normalize_uid(hardware_uid)
        if not norm_uid:
            return None

        old_device_id = None
        with self.lock:
            for dev_id, dev in list(self.devices.items()):
                if dev_id != target_device_id:
                    if uids_match(dev.hardware_uid, norm_uid) or uids_match(dev.full_uid, norm_uid):
                        old_device_id = dev_id
                        break

        # Check DB if not found in memory
        if old_device_id is None:
            try:
                with SessionLocal() as db:
                    db_devs = db.query(Device).filter(Device.device_id != target_device_id).all()
                    for db_d in db_devs:
                        if db_d.hardware_uid and uids_match(db_d.hardware_uid, norm_uid):
                            old_device_id = db_d.device_id
                            break
            except Exception as e:
                logger.error(f"Error querying DB for UID deduplication: {e}")

        if old_device_id is not None:
            logger.info(f"Deduplicating UID {norm_uid}: removing ghost device node {old_device_id} (re-assigned to {target_device_id})")
            self.remove_device(old_device_id)
            self.add_log(
                old_device_id,
                "DEVICE_REASSIGNED",
                norm_uid,
                f"Node {old_device_id} removed; UID re-assigned to Node {target_device_id}",
                direction="INTERNAL"
            )

        # Associate UID with the target device
        target_dev = self.get_device(target_device_id)
        target_dev.hardware_uid = norm_uid
        if len(norm_uid) > 8:
            target_dev.full_uid = norm_uid
        try:
            with SessionLocal() as db:
                db_target = db.query(Device).filter(Device.device_id == target_device_id).first()
                if db_target:
                    db_target.hardware_uid = norm_uid
                    db.commit()
        except Exception as e:
            logger.error(f"Error persisting hardware_uid for device {target_device_id}: {e}")

        return old_device_id

    def clear_uid_parts(self):
        with self.lock:
            self.latest_uid_part1 = None
            self.latest_uid_part2 = None

    def get_unassigned_devices(self) -> Dict[str, Any]:
        with self.lock:
            current_time = time.time()
            # Clean up devices that haven't been seen in 30 seconds
            expired_keys = [k for k, v in self.unassigned_devices.items() if current_time - v.last_seen > 30.0]
            for k in expired_keys:
                del self.unassigned_devices[k]

            return {uid_hex: dev.to_dict() for uid_hex, dev in self.unassigned_devices.items()}

    def start(self):
        if self.running:
            return
        self.preload_devices_from_db()
        self.running = True
        self.log_queue = queue.Queue()
        self.can.register_callback(self.handle_frame)
        self.log_thread = threading.Thread(target=self._log_worker, daemon=True)
        self.log_thread.start()
        self.janitor_thread = threading.Thread(target=self._janitor_worker, daemon=True)
        self.janitor_thread.start()

    def preload_devices_from_db(self):
        from database.database import SessionLocal
        from models.device import Device
        
        with SessionLocal() as db:
            devices = db.query(Device).all()
            with self.lock:
                for db_dev in devices:
                    dev = DeviceState(db_dev.device_id, offline_timeout=self.offline_timeout)
                    dev.name = db_dev.name
                    dev.hardware_uid = getattr(db_dev, "hardware_uid", None)
                    dev.full_uid = getattr(db_dev, "hardware_uid", None)
                    dev.lock_mode = db_dev.lock_mode
                    dev.auto_close_timeout = db_dev.auto_close_timeout
                    dev.lock_mode_behavior_flags = db_dev.behavior_flags
                    dev.active_slot = getattr(db_dev, "active_slot", 0) or 0
                    dev.last_seen = db_dev.last_seen or 0.0
                    self.devices[db_dev.device_id] = dev
        logger.info(f"Preloaded {len(self.devices)} devices from database.")

    def get_device(self, device_id: int) -> DeviceState:
        with self.lock:
            if device_id not in self.devices:
                self.devices[device_id] = DeviceState(device_id, offline_timeout=self.offline_timeout)
                
                # Check DB for existing device or create a new entry
                from database.database import SessionLocal
                from models.device import Device
                
                try:
                    with SessionLocal() as db:
                        db_dev = db.query(Device).filter(Device.device_id == device_id).first()
                        if db_dev:
                            self.devices[device_id].name = db_dev.name
                            self.devices[device_id].hardware_uid = getattr(db_dev, "hardware_uid", None)
                            self.devices[device_id].full_uid = getattr(db_dev, "hardware_uid", None)
                            self.devices[device_id].lock_mode = db_dev.lock_mode
                            self.devices[device_id].auto_close_timeout = db_dev.auto_close_timeout
                            self.devices[device_id].lock_mode_behavior_flags = db_dev.behavior_flags
                            self.devices[device_id].active_slot = getattr(db_dev, "active_slot", 0) or 0
                            self.devices[device_id].last_seen = db_dev.last_seen or 0.0
                        else:
                            new_dev = Device(
                                device_id=device_id,
                                name=f"Lock {device_id}",
                                hardware_uid=None,
                                lock_mode=1,
                                auto_close_timeout=3,
                                behavior_flags=0,
                                active_slot=0,
                                last_seen=0.0
                            )
                            db.add(new_dev)
                            db.commit()
                except Exception as e:
                    logger.error(f"Failed to query/save device in DB: {e}")

            return self.devices[device_id]

    def get_all_devices(self) -> Dict[int, Any]:
        with self.lock:
            return {dev_id: dev.to_dict() for dev_id, dev in self.devices.items()}

    def stop(self):
        was_running = self.running
        self.running = False
        if was_running:
            try:
                self.can.unregister_callback(self.handle_frame)
            except Exception as e:
                logger.error(f"Error unregistering CANListener callback: {e}")
        if hasattr(self, 'thread') and self.thread and self.thread.is_alive():
            self.thread.join(timeout=0.5)
        try:
            self.log_queue.put_nowait(None)
        except Exception:
            pass
        if self.log_thread and self.log_thread.is_alive():
            self.log_thread.join(timeout=0.5)
        if self.janitor_thread and self.janitor_thread.is_alive():
            self.janitor_thread.join(timeout=0.5)

    def _loop(self):
        """Legacy placeholder; frame reception is driven by CANManager."""
        pass

    def handle_frame(self, frame: CANFrame) -> None:
        """Processes an incoming CAN frame dispatched from CANManager."""
        if not self.running or frame is None:
            return
        try:
            can_id_str = f"0x{frame.arbitration_id:03X}"
            payload_hex = " ".join(f"{b:02X}" for b in frame.data)
            msg = self.parser.parse(frame)
            if msg is not None:
                dev_id = frame.device_id

                if isinstance(msg, StatusBasic):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        prev_state = dev.lock_state
                        prev_error = dev.lock_error
                        
                        dev.update_lock_state(msg.lock_state)
                        dev.update_error(msg.lock_error)
                        
                        if prev_state is not None and prev_state != msg.lock_state:
                            self.add_log(dev_id, "LOCK_STATUS", None, f"State changed to {dev.status_text}", direction="RX", can_id=can_id_str, payload=payload_hex)
                        if prev_error is not None and prev_error != msg.lock_error:
                            if msg.lock_error != 0:
                                self.add_log(dev_id, "ERROR", None, f"Error changed to {dev.error_text}", direction="RX", can_id=can_id_str, payload=payload_hex)
                            else:
                                self.add_log(dev_id, "LOCK_STATUS", None, "Error cleared", direction="RX", can_id=can_id_str, payload=payload_hex)

                        if msg.active_slot is not None and msg.active_slot in (0, 1):
                            dev.active_slot = msg.active_slot
                            try:
                                with SessionLocal() as db:
                                    db_dev = db.query(Device).filter(Device.device_id == dev_id).first()
                                    if db_dev and db_dev.active_slot != msg.active_slot:
                                        db_dev.active_slot = msg.active_slot
                                        db.commit()
                            except Exception as ex:
                                logger.error(f"Failed to persist active_slot from StatusBasic: {ex}")
                                
                        dev.mark_seen()
                elif isinstance(msg, HealthStatus):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.health_data = msg.data
                        dev.mark_seen()
                elif isinstance(msg, HealthShort):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.health_short = {
                            "vcc_mv": msg.vcc_mv,
                            "temp_c": msg.temp_c
                        }
                        dev.mark_seen()
                elif isinstance(msg, HealthExtended):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.health_extended = {
                            "free_ram": msg.free_ram,
                            "uptime_s": msg.uptime_s
                        }
                        dev.mark_seen()
                elif isinstance(msg, HealthDiagInfo):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.health_diag_info = {
                            "reset_reason": msg.reset_reason,
                            "brownout_cnt": msg.brownout_cnt,
                            "watchdog_cnt": msg.watchdog_cnt,
                            "can_tx_err": msg.can_tx_err,
                            "can_rx_err": msg.can_rx_err
                        }
                        dev.mark_seen()
                elif isinstance(msg, HealthSecurityStatus):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.health_security_status = {
                            "state": msg.state,
                            "supported": msg.supported,
                            "pending": msg.pending,
                            "last_counter": msg.last_counter
                        }
                        dev.mark_seen()
                elif isinstance(msg, HealthVersionInfo):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.firmware_version = msg.version_str
                        if msg.active_slot is not None and msg.active_slot in (0, 1):
                            if dev.active_slot != msg.active_slot:
                                dev.active_slot = msg.active_slot
                                try:
                                    with SessionLocal() as db:
                                        db_dev = db.query(Device).filter(Device.device_id == dev_id).first()
                                        if db_dev:
                                            db_dev.active_slot = msg.active_slot
                                            db.commit()
                                except Exception as ex:
                                    logger.error(f"Failed to persist active_slot from version info: {ex}")
                        dev.mark_seen()
                elif isinstance(msg, HealthDeviceInfo):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.hardware_version = msg.hw_info_str
                        dev.door_close_guard_supported = msg.door_close_guard_supported
                        dev.mark_seen()
                elif isinstance(msg, StatusRFID):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        if msg.uid_len <= len(msg.uid):
                            uid_hex = msg.uid[:msg.uid_len].hex()
                            dev.last_scanned_card = uid_hex
                            dev._partial_rfid = None
                            self.add_log(dev_id, "RFID_SCAN", uid_hex, "Card scanned (4-byte)", direction="RX", can_id=can_id_str, payload=payload_hex)
                        else:
                            dev._partial_rfid = msg
                        dev.mark_seen()
                elif isinstance(msg, StatusRFIDPart2):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        if hasattr(dev, '_partial_rfid') and dev._partial_rfid:
                            full_uid_bytes = dev._partial_rfid.uid + msg.uid_part2
                            expected_len = dev._partial_rfid.uid_len
                            if expected_len <= len(full_uid_bytes):
                                uid_hex = full_uid_bytes[:expected_len].hex()
                                dev.last_scanned_card = uid_hex
                                dev._partial_rfid = None
                                self.add_log(dev_id, "RFID_SCAN", uid_hex, "Card scanned (7-byte)", direction="RX", can_id=can_id_str, payload=payload_hex)
                        dev.mark_seen()
                elif isinstance(msg, EventWlAutoDelete):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        if 1 in dev.whitelist_items:
                            del dev.whitelist_items[1]
                        dev.occupancy_state["occupied"] = False
                        dev.mark_seen()
                elif isinstance(msg, CommandAck):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.last_ack_command = msg.command
                        dev.last_ack_time = time.time()
                        if msg.command == 0x01: # OPEN_LOCK
                            self.add_log(dev_id, "MANUAL_OPEN", None, "Pulse open commanded", direction="RX", can_id=can_id_str, payload=payload_hex, corr_id=msg.corr_id)
                        elif msg.command == 0x04: # OPEN_HOLD
                            self.add_log(dev_id, "MANUAL_OPEN", None, "Hold open commanded", direction="RX", can_id=can_id_str, payload=payload_hex, corr_id=msg.corr_id)
                        elif msg.command == 0x05: # OPEN_RESET
                            self.add_log(dev_id, "MANUAL_OPEN", None, "Lock reset commanded", direction="RX", can_id=can_id_str, payload=payload_hex, corr_id=msg.corr_id)
                        elif msg.command in (0x30, 0x31, 0x32, 0x33, 0x3D, 0x6A): # Whitelist commands
                            self.add_log(dev_id, "WHITELIST_MUTATION", None, f"Whitelist command 0x{msg.command:02X} acknowledged", direction="RX", can_id=can_id_str, payload=payload_hex, corr_id=msg.corr_id)
                        elif msg.command in (0x40, 0x41): # LED_SET, LED_RESET
                            cmd_name = COMMAND_NAMES.get(msg.command, f"0x{msg.command:02X}")
                            self.add_log(dev_id, "COMMAND_ACK", None, f"LED command ({cmd_name}) acknowledged", direction="RX", can_id=can_id_str, payload=payload_hex, corr_id=msg.corr_id)
                        elif msg.command in (0x50, 0x51): # BUZZ_PLAY, BUZZ_STOP
                            cmd_name = COMMAND_NAMES.get(msg.command, f"0x{msg.command:02X}")
                            self.add_log(dev_id, "COMMAND_ACK", None, f"Buzzer command ({cmd_name}) acknowledged", direction="RX", can_id=can_id_str, payload=payload_hex, corr_id=msg.corr_id)
                        else:
                            cmd_name = COMMAND_NAMES.get(msg.command, f"0x{msg.command:02X}")
                            self.add_log(dev_id, "COMMAND_ACK", None, f"Command {cmd_name} acknowledged", direction="RX", can_id=can_id_str, payload=payload_hex, corr_id=msg.corr_id)
                        dev.mark_seen()
                elif isinstance(msg, StatusError):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.update_error(msg.error_code)
                        dev.mark_seen()
                        err_name = ERROR_CODES.get(msg.error_code, f"ERROR_{msg.error_code}")
                        self.add_log(dev_id, "ERROR", None, f"Command 0x{msg.cmd:02X} rejected: {err_name}", direction="RX", can_id=can_id_str, payload=payload_hex, corr_id=msg.corr_id)
                elif isinstance(msg, IdRequest):
                    uid_hex = msg.uid32.hex().lower()
                    with self.lock:
                        if uid_hex not in self.unassigned_devices:
                            self.unassigned_devices[uid_hex] = UnassignedDevice(msg.uid32)
                        self.unassigned_devices[uid_hex].last_seen = time.time()

                        # Requirement 3: Factory Reset / Unassigned Announcement Handling
                        # When an unassigned announcement arrives for a known registered UID, mark it offline/unprovisioned
                        for registered_id, registered_dev in list(self.devices.items()):
                            if uids_match(registered_dev.hardware_uid, uid_hex) or uids_match(registered_dev.full_uid, uid_hex):
                                registered_dev.mark_offline(unprovisioned=True)
                                logger.warning(
                                    f"Registered device {registered_id} announced as unassigned (factory reset); marked OFFLINE/UNPROVISIONED."
                                )
                                self.add_log(
                                    registered_id,
                                    "UNASSIGNED_ANNOUNCEMENT",
                                    uid_hex,
                                    f"Node {registered_id} announced as unassigned (factory reset); marked OFFLINE/UNPROVISIONED",
                                    direction="RX",
                                    can_id=can_id_str,
                                    payload=payload_hex
                                )
                elif isinstance(msg, UIDPart1):
                    with self.lock:
                        self.latest_uid_part1 = msg.uid
                elif isinstance(msg, UIDPart2):
                    with self.lock:
                        self.latest_uid_part2 = msg.uid

                elif isinstance(msg, WlInfoReport):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.whitelist_info = {
                            "used_persistent_slots": msg.used_persistent_slots,
                            "max_persistent_capacity": msg.max_persistent_capacity,
                            "ephemeral_active": msg.ephemeral_active
                        }
                        dev.mark_seen()
                elif isinstance(msg, LockModeReport):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.lock_mode = msg.lock_mode
                        dev.auto_close_timeout = msg.auto_close_timeout_s
                        dev.lock_mode_behavior_flags = msg.behavior_flags
                        dev.door_warning_delay_s = msg.door_warning_delay_s
                        dev.door_release_delay_s = msg.door_release_delay_s
                        dev.mark_seen()
                elif isinstance(msg, WlListV2Item):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        if msg.slot_index not in dev.whitelist_items:
                            dev.whitelist_items[msg.slot_index] = {"uid_parts": {}}
                        dev.whitelist_items[msg.slot_index].update({
                            "entry_type": msg.entry_type,
                            "uid_len": msg.uid_len,
                            "policy": msg.policy,
                            "open_action": msg.open_action,
                            "ttl_days": msg.ttl_days
                        })
                        dev.mark_seen()
                elif isinstance(msg, WlListV2UidPart1):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        if msg.slot_index not in dev.whitelist_items:
                            dev.whitelist_items[msg.slot_index] = {"uid_parts": {}}
                        dev.whitelist_items[msg.slot_index]["uid_parts"][1] = msg.uid_bytes
                        dev.mark_seen()
                elif isinstance(msg, WlListV2UidPart2):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        if msg.slot_index not in dev.whitelist_items:
                            dev.whitelist_items[msg.slot_index] = {"uid_parts": {}}
                        dev.whitelist_items[msg.slot_index]["uid_parts"][2] = msg.uid_bytes
                        dev.mark_seen()
                elif isinstance(msg, OccupancyStateReport):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.occupancy_state.update({
                            "occupied": msg.occupied,
                            "owner_present": msg.owner_present,
                            "source": msg.source,
                            "state_counter_24": msg.state_counter_24
                        })
                        dev.mark_seen()
                elif isinstance(msg, OccupancyOwnerShort):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.occupancy_state["owner_uid_short"] = msg.owner_uid_short.hex()
                        dev.mark_seen()
                elif isinstance(msg, PolicyActionReport):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        action_map = {0: "DENIED", 1: "GRANTED_PULSE", 2: "GRANTED_HOLD", 3: "GRANTED_TOGGLE"}
                        action_str = action_map.get(msg.open_action, f"ACTION_{msg.open_action}")
                        if msg.local_result == 0:
                            result_str = "DENIED"
                            event_type = "ACCESS_DENIED"
                        else:
                            result_str = "GRANTED"
                            event_type = "ACCESS_GRANTED"
                        
                        self.add_log(dev_id, event_type, dev.last_scanned_card, f"Policy: {msg.policy}, Action: {action_str}, Result: {result_str}", direction="RX", can_id=can_id_str, payload=payload_hex)
                        dev.mark_seen()
                elif isinstance(msg, RuntimeStateSnapshot):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        prev_state = dev.lock_state
                        prev_error = dev.lock_error
                        
                        dev.runtime_state.update({
                            "lock_id": msg.lock_id,
                            "physical_state": msg.physical_state,
                            "lock_error": msg.lock_error,
                            "occupancy_flags": msg.occupancy_flags,
                            "led_status": msg.led_status,
                            "led_remaining_s": msg.led_remaining_s,
                            "state_revision": msg.state_revision
                        })
                        # refresh lock state
                        dev.update_lock_state(msg.physical_state)
                        dev.update_error(msg.lock_error)
                        
                        if prev_state is not None and prev_state != msg.physical_state:
                            self.add_log(dev_id, "LOCK_STATUS", None, f"State changed to {dev.status_text}", direction="RX", can_id=can_id_str, payload=payload_hex)
                        if prev_error is not None and prev_error != msg.lock_error:
                            if msg.lock_error != 0:
                                self.add_log(dev_id, "ERROR", None, f"Error changed to {dev.error_text}", direction="RX", can_id=can_id_str, payload=payload_hex)
                            else:
                                self.add_log(dev_id, "LOCK_STATUS", None, "Error cleared", direction="RX", can_id=can_id_str, payload=payload_hex)
                                
                        dev.mark_seen()
                elif isinstance(msg, DiagExtended):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.diag_extended.update({
                            "lock_id": msg.lock_id,
                            "bus_off_counter": msg.bus_off_counter,
                            "last_recovery_stage": msg.last_recovery_stage,
                            "diag_flags": msg.diag_flags
                        })
                        dev.mark_seen()


        except Exception as e:
            logger.error(f"CANListener handle_frame error: {e}")
