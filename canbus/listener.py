"""
PS Locks OIP CAN Listener.

Implements a background polling loop to continuously listen for CAN messages.
Dispatches messages to the parser and updates an in-memory dictionary of
device states tracking properties like lock status, telemetry, and health.
"""


import threading
import time
import queue
from typing import Dict, Any, Optional
from datetime import datetime

from database.database import SessionLocal
from models.log import EventLog

from .service import CANService
from .parser import CANParser, StatusBasic, HealthStatus, StatusError, CommandAck, IdRequest, UIDPart1, UIDPart2, StatusRFID, StatusRFIDPart2, EventWlAutoDelete, HealthVersionInfo, HealthDeviceInfo, HealthShort, HealthExtended, HealthDiagInfo, HealthSecurityStatus, WlInfoReport, LockModeReport, WlListV2Item, WlListV2UidPart1, WlListV2UidPart2, OccupancyStateReport, OccupancyOwnerShort, PolicyActionReport, RuntimeStateSnapshot, DiagExtended

ERROR_CODES = {
    0x00: "NONE",
    0x02: "INVALID_PARAM",
    0x03: "UNSUPPORTED_CMD",
    0x04: "DOOR_GUARD_ACTIVE",
    0x06: "SLOT_OUT_OF_RANGE",
    0x0D: "DOOR_GUARD_TIMEOUT"
}

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
    def __init__(self, device_id: int):
        self.device_id = device_id
        self.name = f"Lock {device_id}"
        self.lock_state = None
        self.lock_error = 0
        self.last_seen = 0.0
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
        self.runtime_state: Dict[str, Any] = {}
        self.diag_extended: Dict[str, Any] = {}


    @property
    def online(self) -> bool:
        return (time.time() - self.last_seen) < 60.0

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
            "lock_state": self.lock_state,
            "lock_error": self.lock_error,
            "last_seen": self.last_seen,
            "health_data": self.health_data.hex() if self.health_data else None,
            "last_ack_command": self.last_ack_command,
            "last_ack_time": self.last_ack_time,
            "online": self.online,
            "is_locked": self.is_locked,
            "is_door_closed": self.is_door_closed,
            "status_text": self.status_text,
            "error_text": self.error_text,
            "firmware_version": self.firmware_version,
            "hardware_version": self.hardware_version,
            "full_uid": self.full_uid,
            
            "full_uid": self.full_uid,
            "last_scanned_card": self.last_scanned_card,
            "whitelist_info": self.whitelist_info,
            "whitelist_items": self.whitelist_items,
            "lock_mode": self.lock_mode,
            "auto_close_timeout": self.auto_close_timeout,
            "occupancy_state": self.occupancy_state,
        }


class CANListener:
    def __init__(self, can_service: CANService):
        self.can = can_service
        self.parser = CANParser()
        self.running = False
        self.thread = None
        self.devices: Dict[int, DeviceState] = {}
        self.unassigned_devices: Dict[str, UnassignedDevice] = {}
        self.latest_uid_part1: Optional[bytes] = None
        self.latest_uid_part2: Optional[bytes] = None
        self.lock = threading.Lock()
        
        self.log_queue = queue.Queue()
        self.recent_logs = []
        self.log_thread = None

    def _log_worker(self):
        with SessionLocal() as db:
            while self.running:
                item = self.log_queue.get()
                if item is None:
                    break
                try:
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
                        "details": log_entry.details
                    }
                    with self.lock:
                        self.recent_logs.append(log_dict)
                        if len(self.recent_logs) > 200:
                            self.recent_logs = self.recent_logs[-100:]
                except Exception as e:
                    print(f"Log worker error: {e}")
                    db.rollback()

    def add_log(self, device_id: int, event_type: str, card_uid: Optional[str] = None, details: Optional[str] = None):
        device_name = f"Lock {device_id}"
        if device_id in self.devices:
            device_name = self.devices[device_id].name
        self.log_queue.put({
            "device_id": device_id,
            "device_name": device_name,
            "event_type": event_type,
            "card_uid": card_uid,
            "details": details
        })

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
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.log_thread = threading.Thread(target=self._log_worker, daemon=True)
        self.thread.start()
        self.log_thread.start()

    def preload_devices_from_db(self):
        from database.database import SessionLocal
        from models.device import Device
        
        with SessionLocal() as db:
            devices = db.query(Device).all()
            with self.lock:
                for db_dev in devices:
                    dev = DeviceState(db_dev.device_id)
                    dev.name = db_dev.name
                    dev.lock_mode = db_dev.lock_mode
                    dev.auto_close_timeout = db_dev.auto_close_timeout
                    dev.lock_mode_behavior_flags = db_dev.behavior_flags
                    dev.last_seen = db_dev.last_seen
                    self.devices[db_dev.device_id] = dev
        print(f"Preloaded {len(self.devices)} devices from database.")

    def get_device(self, device_id: int) -> DeviceState:
        with self.lock:
            if device_id not in self.devices:
                self.devices[device_id] = DeviceState(device_id)
                
                # Check DB for existing device or create a new entry
                from database.database import SessionLocal
                from models.device import Device
                
                try:
                    with SessionLocal() as db:
                        db_dev = db.query(Device).filter(Device.device_id == device_id).first()
                        if db_dev:
                            self.devices[device_id].name = db_dev.name
                            self.devices[device_id].lock_mode = db_dev.lock_mode
                            self.devices[device_id].auto_close_timeout = db_dev.auto_close_timeout
                            self.devices[device_id].lock_mode_behavior_flags = db_dev.behavior_flags
                            self.devices[device_id].last_seen = db_dev.last_seen
                        else:
                            new_dev = Device(
                                device_id=device_id,
                                name=f"Lock {device_id}",
                                lock_mode=1,
                                auto_close_timeout=3,
                                behavior_flags=0,
                                last_seen=time.time()
                            )
                            db.add(new_dev)
                            db.commit()
                except Exception as e:
                    print(f"Failed to query/save device in DB: {e}")

            return self.devices[device_id]

    def get_all_devices(self) -> Dict[int, Any]:
        with self.lock:
            return {dev_id: dev.to_dict() for dev_id, dev in self.devices.items()}

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)
        self.log_queue.put(None)
        if self.log_thread:
            self.log_thread.join(timeout=2.0)

    def _loop(self):
        while self.running:
            try:
                frame = self.can.receive(timeout=0.5)
                if frame is None:
                    continue

                msg = self.parser.parse(frame)
                if msg is None:
                    continue

                dev_id = frame.device_id

                if isinstance(msg, StatusBasic):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        prev_state = dev.lock_state
                        prev_error = dev.lock_error
                        
                        dev.update_lock_state(msg.lock_state)
                        dev.update_error(msg.lock_error)
                        
                        if prev_state is not None and prev_state != msg.lock_state:
                            self.add_log(dev_id, "LOCK_STATUS", None, f"State changed to {dev.status_text}")
                        if prev_error is not None and prev_error != msg.lock_error:
                            if msg.lock_error != 0:
                                self.add_log(dev_id, "ERROR", None, f"Error changed to {dev.error_text}")
                            else:
                                self.add_log(dev_id, "LOCK_STATUS", None, "Error cleared")
                                
                        dev.last_seen = time.time()
                elif isinstance(msg, HealthStatus):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.health_data = msg.data
                        dev.last_seen = time.time()
                elif isinstance(msg, HealthShort):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.health_short = {
                            "vcc_mv": msg.vcc_mv,
                            "temp_c": msg.temp_c
                        }
                        dev.last_seen = time.time()
                elif isinstance(msg, HealthExtended):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.health_extended = {
                            "free_ram": msg.free_ram,
                            "uptime_s": msg.uptime_s
                        }
                        dev.last_seen = time.time()
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
                        dev.last_seen = time.time()
                elif isinstance(msg, HealthSecurityStatus):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.health_security_status = {
                            "state": msg.state,
                            "supported": msg.supported,
                            "pending": msg.pending,
                            "last_counter": msg.last_counter
                        }
                        dev.last_seen = time.time()
                elif isinstance(msg, HealthVersionInfo):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.firmware_version = msg.version_str
                        dev.last_seen = time.time()
                elif isinstance(msg, HealthDeviceInfo):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.hardware_version = msg.hw_info_str
                        dev.door_close_guard_supported = msg.door_close_guard_supported
                        dev.last_seen = time.time()
                elif isinstance(msg, StatusRFID):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        if msg.uid_len <= len(msg.uid):
                            uid_hex = msg.uid[:msg.uid_len].hex()
                            dev.last_scanned_card = uid_hex
                            dev._partial_rfid = None
                            self.add_log(dev_id, "RFID_SCAN", uid_hex, "Card scanned (4-byte)")
                        else:
                            dev._partial_rfid = msg
                        dev.last_seen = time.time()
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
                                self.add_log(dev_id, "RFID_SCAN", uid_hex, "Card scanned (7-byte)")
                        dev.last_seen = time.time()
                elif isinstance(msg, EventWlAutoDelete):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        if 1 in dev.whitelist_items:
                            del dev.whitelist_items[1]
                        dev.occupancy_state["occupied"] = False
                        dev.last_seen = time.time()
                elif isinstance(msg, CommandAck):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.last_ack_command = msg.command
                        dev.last_ack_time = time.time()
                        if msg.command == 0x01: # OPEN_LOCK
                            self.add_log(dev_id, "MANUAL_OPEN", None, "Pulse open commanded")
                        elif msg.command == 0x04: # OPEN_HOLD
                            self.add_log(dev_id, "MANUAL_OPEN", None, "Hold open commanded")
                        elif msg.command == 0x05: # OPEN_RESET
                            self.add_log(dev_id, "MANUAL_OPEN", None, "Lock reset commanded")
                elif isinstance(msg, StatusError):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.update_error(msg.error_code)
                        dev.last_seen = time.time()
                elif isinstance(msg, IdRequest):
                    uid_hex = msg.uid32.hex()
                    with self.lock:
                        if uid_hex not in self.unassigned_devices:
                            self.unassigned_devices[uid_hex] = UnassignedDevice(msg.uid32)
                        self.unassigned_devices[uid_hex].last_seen = time.time()
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
                        dev.last_seen = time.time()
                elif isinstance(msg, LockModeReport):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.lock_mode = msg.lock_mode
                        dev.auto_close_timeout = msg.auto_close_timeout_s
                        dev.lock_mode_behavior_flags = msg.behavior_flags
                        dev.door_warning_delay_s = msg.door_warning_delay_s
                        dev.door_release_delay_s = msg.door_release_delay_s
                        dev.last_seen = time.time()
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
                        dev.last_seen = time.time()
                elif isinstance(msg, WlListV2UidPart1):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        if msg.slot_index not in dev.whitelist_items:
                            dev.whitelist_items[msg.slot_index] = {"uid_parts": {}}
                        dev.whitelist_items[msg.slot_index]["uid_parts"][1] = msg.uid_bytes
                        dev.last_seen = time.time()
                elif isinstance(msg, WlListV2UidPart2):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        if msg.slot_index not in dev.whitelist_items:
                            dev.whitelist_items[msg.slot_index] = {"uid_parts": {}}
                        dev.whitelist_items[msg.slot_index]["uid_parts"][2] = msg.uid_bytes
                        dev.last_seen = time.time()
                elif isinstance(msg, OccupancyStateReport):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.occupancy_state.update({
                            "occupied": msg.occupied,
                            "owner_present": msg.owner_present,
                            "source": msg.source,
                            "state_counter_24": msg.state_counter_24
                        })
                        dev.last_seen = time.time()
                elif isinstance(msg, OccupancyOwnerShort):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.occupancy_state["owner_uid_short"] = msg.owner_uid_short.hex()
                        dev.last_seen = time.time()
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
                        
                        self.add_log(dev_id, event_type, dev.last_scanned_card, f"Policy: {msg.policy}, Action: {action_str}, Result: {result_str}")
                        dev.last_seen = time.time()
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
                            self.add_log(dev_id, "LOCK_STATUS", None, f"State changed to {dev.status_text}")
                        if prev_error is not None and prev_error != msg.lock_error:
                            if msg.lock_error != 0:
                                self.add_log(dev_id, "ERROR", None, f"Error changed to {dev.error_text}")
                            else:
                                self.add_log(dev_id, "LOCK_STATUS", None, "Error cleared")
                                
                        dev.last_seen = time.time()
                elif isinstance(msg, DiagExtended):
                    dev = self.get_device(dev_id)
                    with self.lock:
                        dev.diag_extended.update({
                            "lock_id": msg.lock_id,
                            "bus_off_counter": msg.bus_off_counter,
                            "last_recovery_stage": msg.last_recovery_stage,
                            "diag_flags": msg.diag_flags
                        })
                        dev.last_seen = time.time()


            except Exception as e:
                print(f"CANListener error: {e}")
                time.sleep(1)
