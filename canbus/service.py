"""
PS Locks OIP CAN Bus Manager.

Provides centralized SocketCAN access, message routing for standard telemetry,
and asynchronous routing of OTA bootloader frames into per-device asyncio queues.
"""

import asyncio
import errno
import logging

import threading
import time
from typing import Any, Callable, Dict, List, Optional

import can

from .constants import (
    FITNET_CAN_ID_OTA_ACK_BASE,
    FITNET_CAN_ID_OTA_BASE_MASK,
    FITNET_CAN_ID_OTA_CMD_BASE,
    FITNET_CAN_ID_OTA_DEVICE_MASK,
    TYPE_MASK,
    CAN_COMMAND,
    CAN_MASTER,
    DEFAULT_DEVICE_ID,
    MASTER_BEACON,
    MASTER_ASSIGN_ID,
    OPEN_LOCK,
    REQUEST_STATUS,
    OPEN_HOLD,
    OPEN_RESET,
    LED_SET,
    LED_RESET,
    BUZZ_PLAY,
    BUZZ_STOP,
    REQUEST_HEALTH,
    REQUEST_VERSION,
    REQUEST_DEVICE_INFO,
    REQUEST_FULL_UID,
    SET_LOCK_MODE,
    REQUEST_LOCK_MODE,
    REQUEST_OCCUPANCY_STATE,
    REQUEST_WL_INFO,
    REQUEST_WL_LIST_V2,
    DELETE_WL_SLOT,
    WL_CLEAR,
    SET_WL_SLOT_POLICY,
    DEVICE_RESET,
    FACTORY_RESET,
    COMMAND_NAMES,
)
from .protocol import CANFrame
logger = logging.getLogger(__name__)



def decode_tx_frame(frame: CANFrame) -> Optional[Dict[str, Any]]:
    """Decodes outgoing CAN frame metadata and human-readable context for TX logging."""
    if not frame or not frame.data:
        return None

    arb_id = frame.arbitration_id
    # Skip periodic heartbeat beacons from flooding logs
    if arb_id == (CAN_MASTER | DEFAULT_DEVICE_ID) and frame.data[0] == MASTER_BEACON:
        return None
    # Skip high-frequency OTA chunk transmissions
    if (arb_id & FITNET_CAN_ID_OTA_BASE_MASK) == FITNET_CAN_ID_OTA_CMD_BASE and frame.data[0] == 0x02:
        return None

    can_id_str = f"0x{arb_id:03X}"
    payload_hex = " ".join(f"{b:02X}" for b in frame.data)
    dev_id = arb_id & 0x7F
    corr_id = None
    command_name = "CAN_TX"
    details = f"TX Frame {can_id_str}"

    if (arb_id & TYPE_MASK) == CAN_COMMAND:
        dev_id = arb_id & 0x7F
        if len(frame.data) >= 2:
            lock_id = frame.data[0]
            cmd_code = frame.data[1]
            corr_id = frame.data[2] if len(frame.data) >= 3 else None
            command_name = COMMAND_NAMES.get(cmd_code, f"CMD_0x{cmd_code:02X}")

            if cmd_code == OPEN_LOCK:
                details = f"Pulse unlock commanded (lock {lock_id})"
            elif cmd_code == OPEN_HOLD:
                details = f"Hold open commanded (lock {lock_id})"
            elif cmd_code == OPEN_RESET:
                details = f"Lock reset commanded (lock {lock_id})"
            elif cmd_code == REQUEST_STATUS:
                details = f"Request lock status (lock {lock_id})"
            elif cmd_code == LED_SET:
                mode_names = {0: "OFF", 1: "GREEN", 2: "RED", 3: "GREEN_BLINK", 4: "RED_BLINK", 5: "GREEN_FAST"}
                m_name = mode_names.get(frame.data[3] if len(frame.data) > 3 else 0, "SET")
                ttl = frame.data[6] if len(frame.data) > 6 else 5
                details = f"Set LED: mode={m_name}, ttl={ttl}s (lock {lock_id})"
            elif cmd_code == LED_RESET:
                details = f"Reset LED indicators (lock {lock_id})"
            elif cmd_code == BUZZ_PLAY:
                sound_names = {1: "OK", 2: "DENY", 3: "ERROR", 4: "ALARM1", 5: "ALARM2"}
                s_name = sound_names.get(frame.data[3] if len(frame.data) > 3 else 1, "PLAY")
                repeat = frame.data[4] if len(frame.data) > 4 else 1
                details = f"Play buzzer: {s_name} x{repeat} (lock {lock_id})"
            elif cmd_code == BUZZ_STOP:
                details = f"Stop buzzer (lock {lock_id})"
            elif cmd_code == REQUEST_HEALTH:
                details = f"Request health telemetry (lock {lock_id})"
            elif cmd_code == REQUEST_VERSION:
                details = f"Request firmware version (lock {lock_id})"
            elif cmd_code == REQUEST_DEVICE_INFO:
                details = f"Request device info (lock {lock_id})"
            elif cmd_code == REQUEST_FULL_UID:
                uid_str = frame.data[3:7].hex().upper() if len(frame.data) >= 7 else ""
                details = f"Request full UID for {uid_str}"
            elif cmd_code == SET_LOCK_MODE:
                details = f"Set lock mode {frame.data[3]} (lock {lock_id})" if len(frame.data) > 3 else "Set lock mode"
            elif cmd_code == REQUEST_LOCK_MODE:
                details = f"Request lock mode (lock {lock_id})"
            elif cmd_code == REQUEST_OCCUPANCY_STATE:
                details = f"Request occupancy state (lock {lock_id})"
            elif cmd_code == REQUEST_WL_INFO:
                details = f"Request whitelist info (lock {lock_id})"
            elif cmd_code == REQUEST_WL_LIST_V2:
                details = f"Request whitelist list (lock {lock_id})"
            elif cmd_code == DELETE_WL_SLOT:
                slot = frame.data[3] if len(frame.data) > 3 else 0
                details = f"Delete whitelist slot {slot} (lock {lock_id})"
            elif cmd_code == WL_CLEAR:
                details = f"Clear whitelist (lock {lock_id})"
            elif cmd_code == SET_WL_SLOT_POLICY:
                slot = frame.data[3] if len(frame.data) > 3 else 0
                details = f"Set whitelist slot {slot} policy (lock {lock_id})"
            elif cmd_code == DEVICE_RESET:
                details = f"Software device reboot commanded (lock {lock_id})"
            elif cmd_code == FACTORY_RESET:
                details = f"Factory reset commanded (lock {lock_id})"
            else:
                details = f"Transmitted {command_name} (corrId: {corr_id})"

    elif (arb_id & FITNET_CAN_ID_OTA_BASE_MASK) == FITNET_CAN_ID_OTA_CMD_BASE:
        dev_id = arb_id & FITNET_CAN_ID_OTA_DEVICE_MASK
        cmd_code = frame.data[0]
        ota_names = {0x01: "CMD_OTA_START", 0x03: "CMD_OTA_VERIFY", 0x04: "CMD_OTA_ACTIVATE"}
        command_name = ota_names.get(cmd_code, f"OTA_CMD_0x{cmd_code:02X}")
        details = f"Transmitted {command_name}"

    elif (arb_id & TYPE_MASK) == CAN_MASTER:
        cmd_code = frame.data[0]
        if cmd_code == MASTER_ASSIGN_ID:
            command_name = "MASTER_ASSIGN_ID"
            details = f"Assign ID: target={frame.data[5]} (UID {frame.data[1:5].hex().upper()})" if len(frame.data) >= 6 else "Assign ID"
        else:
            command_name = f"MASTER_0x{cmd_code:02X}"
            details = f"Transmitted {command_name}"

    return {
        "device_id": dev_id,
        "command_name": command_name,
        "details": details,
        "direction": "TX",
        "can_id": can_id_str,
        "payload": payload_hex,
        "corr_id": corr_id,
    }


class CANManager:
    """
    Centralized CAN Manager dispatching bus messages between standard
    telemetry handlers and asynchronous bootloader OTA queues.
    """

    def __init__(
        self,
        channel: str = "can0",
        bitrate: int = 50000,
        simulation: bool = False,
        pacing_delay: float = 0.006,
    ):
        self.channel = channel
        self.bitrate = bitrate
        self.simulation = simulation
        self.pacing_delay = pacing_delay

        self.bus: Optional[can.interface.Bus] = None
        self.send_lock = threading.Lock()
        self.queue_lock = threading.Lock()

        self.ota_queues: Dict[int, asyncio.Queue] = {}
        self.standard_callbacks: List[Callable[[CANFrame], None]] = []
        self.tx_callbacks: List[Callable[[CANFrame], None]] = []
        self._log_handler: Optional[Callable[..., None]] = None

        self.running = False
        self.reader_thread: Optional[threading.Thread] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        if not self.simulation:
            self.connect()

    # ---------------------------------------------------------

    def connect(self) -> None:
        """Initializes or re-initializes the SocketCAN bus interface."""
        with self.send_lock:
            if self.bus is not None:
                try:
                    self.bus.shutdown()
                except Exception:
                    pass
                self.bus = None

            try:
                self.bus = can.interface.Bus(
                    interface="socketcan",
                    channel=self.channel,
                    bitrate=self.bitrate,
                )
            except Exception as ex:
                self.bus = None
                logger.error(f"CANManager connect error: {ex}")

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Sets the active asyncio event loop for threadsafe queue routing."""
        self.loop = loop

    def register_callback(self, callback: Callable[[CANFrame], None]) -> None:
        """Registers a callback for standard non-OTA CAN frames."""
        if callback not in self.standard_callbacks:
            self.standard_callbacks.append(callback)

    def unregister_callback(self, callback: Callable[[CANFrame], None]) -> None:
        """Unregisters a previously attached callback."""
        if callback in self.standard_callbacks:
            self.standard_callbacks.remove(callback)

    def register_tx_callback(self, callback: Callable[[CANFrame], None]) -> None:
        """Registers a callback for transmitted standard CAN frames."""
        if callback not in self.tx_callbacks:
            self.tx_callbacks.append(callback)

    def unregister_tx_callback(self, callback: Callable[[CANFrame], None]) -> None:
        """Unregisters a previously attached TX callback."""
        if callback in self.tx_callbacks:
            self.tx_callbacks.remove(callback)

    def set_log_handler(self, handler: Callable[..., None]) -> None:
        """Attaches a log handler callback for recording TX events."""
        self._log_handler = handler

    def _emit_tx_log(self, frame: CANFrame) -> None:
        """Emits an explicit TX log entry to the log queue / event streamer."""
        try:
            info = decode_tx_frame(frame)
            if info is None:
                return

            if self._log_handler is not None:
                self._log_handler(
                    device_id=info["device_id"],
                    event_type=info["command_name"],
                    card_uid=None,
                    details=info["details"],
                    direction="TX",
                    can_id=info["can_id"],
                    payload=info["payload"],
                    corr_id=info["corr_id"],
                )
            else:
                try:
                    from config.globals import can_listener
                    if can_listener is not None:
                        can_listener.add_log(
                            device_id=info["device_id"],
                            event_type=info["command_name"],
                            card_uid=None,
                            details=info["details"],
                            direction="TX",
                            can_id=info["can_id"],
                            payload=info["payload"],
                            corr_id=info["corr_id"],
                        )
                except Exception:
                    pass

            for cb in list(self.tx_callbacks):
                try:
                    cb(frame)
                except Exception as ex:
                    logger.error(f"CANManager tx_callback error: {ex}")
        except Exception as ex:
            logger.error(f"Error emitting TX log entry: {ex}")

    def get_ota_queue(self, device_id: int) -> asyncio.Queue:
        """Retrieves or initializes a dedicated asyncio.Queue for a target device ID."""
        with self.queue_lock:
            if device_id not in self.ota_queues:
                self.ota_queues[device_id] = asyncio.Queue()
            return self.ota_queues[device_id]

    def clear_ota_queue(self, device_id: int) -> None:
        """Removes the dedicated OTA queue for a target device ID."""
        with self.queue_lock:
            if device_id in self.ota_queues:
                del self.ota_queues[device_id]

    def start(self) -> None:
        """Starts the centralized background CAN reader thread."""
        if self.running:
            return
        if self.bus is None and not self.simulation:
            self.connect()
        self.running = True
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

    def stop(self) -> None:
        """Stops the reader thread and closes the CAN interface."""
        self.running = False
        if self.reader_thread and self.reader_thread.is_alive():
            self.reader_thread.join(timeout=0.5)
        self.shutdown()

    def _reader_loop(self) -> None:
        """Background listener polling SocketCAN and dispatching frames."""
        while self.running:
            if self.simulation:
                time.sleep(0.05)
                continue
            if self.bus is None:
                self.connect()
                if self.bus is None:
                    time.sleep(0.5)
                    continue

            try:
                msg = self.bus.recv(timeout=0.2)
                if msg is None:
                    continue
                frame = CANFrame(
                    arbitration_id=msg.arbitration_id,
                    data=bytes(msg.data),
                )
                self._dispatch_frame(frame)
            except (can.CanError, OSError) as ex:
                if self.running:
                    time.sleep(0.1)
                    if isinstance(ex, OSError) and ex.errno in (errno.ENETDOWN, errno.EBADF, errno.ENODEV, 100, 9, 19):
                        self.connect()
            except Exception:
                if self.running:
                    time.sleep(0.05)

    def _dispatch_frame(self, frame: CANFrame) -> None:
        """Dispatches frames using mask-based routing for OTA ACKs."""
        arb_id = frame.arbitration_id
        is_ota_ack = ((arb_id & FITNET_CAN_ID_OTA_BASE_MASK) == 0x790) or (
            0x790 <= arb_id <= (FITNET_CAN_ID_OTA_ACK_BASE + FITNET_CAN_ID_OTA_DEVICE_MASK)
        )
        if is_ota_ack:
            self._route_ota_frame(frame)
        else:
            self._route_standard_frame(frame)

    def _route_ota_frame(self, frame: CANFrame) -> None:
        """Routes an OTA ACK frame to the appropriate device queue."""
        arb_id = frame.arbitration_id
        target_queue: Optional[asyncio.Queue] = None

        with self.queue_lock:
            for dev_id, queue in self.ota_queues.items():
                if arb_id in (FITNET_CAN_ID_OTA_ACK_BASE | dev_id, FITNET_CAN_ID_OTA_ACK_BASE + dev_id):
                    target_queue = queue
                    break

            if target_queue is None:
                offset_id = arb_id - FITNET_CAN_ID_OTA_ACK_BASE
                if offset_id in self.ota_queues:
                    target_queue = self.ota_queues[offset_id]
                else:
                    masked_id = arb_id & FITNET_CAN_ID_OTA_DEVICE_MASK
                    if masked_id in self.ota_queues:
                        target_queue = self.ota_queues[masked_id]

        if target_queue is not None:
            if self.loop and self.loop.is_running():
                self.loop.call_soon_threadsafe(target_queue.put_nowait, frame)
            else:
                try:
                    target_queue.put_nowait(frame)
                except Exception:
                    pass

    def _route_standard_frame(self, frame: CANFrame) -> None:
        """Distributes standard non-OTA frames to registered callbacks."""
        for callback in list(self.standard_callbacks):
            try:
                callback(frame)
            except Exception as ex:
                logger.error(f"CANManager callback exception: {ex}")

    def send(
        self,
        frame: CANFrame,
        delay: float = 0.0,
        max_retries: int = 10,
        backoff_base: float = 0.005,
    ) -> bool:
        """
        Thread-safe CAN message transmission with buffer overflow backoff.
        Catches can.CanError and OSError (Errno 105: ENOBUFS) and retries
        with exponential backoff to handle SocketCAN TX queue saturation.
        """
        # Block operational commands targeted to a device undergoing active OTA
        arb_id = frame.arbitration_id
        is_ota_cmd = (arb_id & FITNET_CAN_ID_OTA_BASE_MASK) == FITNET_CAN_ID_OTA_CMD_BASE
        if not is_ota_cmd:
            target_dev = arb_id & FITNET_CAN_ID_OTA_DEVICE_MASK
            if 0 < target_dev < FITNET_CAN_ID_OTA_DEVICE_MASK:
                with self.queue_lock:
                    if target_dev in self.ota_queues:
                        from fastapi import HTTPException
                        raise HTTPException(
                            status_code=503,
                            detail="Device is currently undergoing an OTA firmware update",
                        )

        if self.simulation:
            print(
                f"TX  {frame.arbitration_id:03X}   "
                + " ".join(f"{b:02X}" for b in frame.data)
            )
            self._emit_tx_log(frame)
            if delay > 0:
                time.sleep(delay)
            return True

        msg = can.Message(
            arbitration_id=frame.arbitration_id,
            data=frame.data,
            is_extended_id=False,
        )

        with self.send_lock:
            if self.bus is None:
                self.connect()
                if self.bus is None:
                    return False

            for attempt in range(max_retries):
                try:
                    self.bus.send(msg)
                    self._emit_tx_log(frame)
                    if delay > 0:
                        time.sleep(delay)
                    return True
                except (can.CanError, OSError) as ex:
                    is_buf_err = False
                    if isinstance(ex, OSError) and ex.errno in (errno.ENOBUFS, 105):
                        is_buf_err = True
                    elif "buffer" in str(ex).lower() or "105" in str(ex):
                        is_buf_err = True

                    if is_buf_err and attempt < max_retries - 1:
                        sleep_time = backoff_base * (1.5 ** attempt)
                        time.sleep(sleep_time)
                        continue
                    elif attempt < max_retries - 1:
                        sleep_time = backoff_base * (1.2 ** attempt)
                        time.sleep(sleep_time)
                        continue
                    else:
                        if isinstance(ex, OSError) and ex.errno in (errno.ENETDOWN, errno.EBADF, errno.ENODEV, 100, 9, 19):
                            try:
                                self.bus = can.interface.Bus(
                                    interface="socketcan",
                                    channel=self.channel,
                                    bitrate=self.bitrate,
                                )
                            except Exception:
                                pass
                        raise ex
            return False

    def receive(self, timeout: float = 1.0) -> Optional[CANFrame]:
        """
        Direct synchronous receive fallback.
        Primary reception is handled via the background listener loop.
        """
        if self.simulation or self.bus is None:
            return None

        msg = self.bus.recv(timeout)
        if msg is None:
            return None

        return CANFrame(
            arbitration_id=msg.arbitration_id,
            data=bytes(msg.data),
        )

    def flush(self) -> None:
        """Flushes hardware receive buffer."""
        pass

    def shutdown(self) -> None:
        """Closes the underlying SocketCAN bus connection."""
        if self.bus is not None:
            try:
                self.bus.shutdown()
            except Exception:
                pass
            self.bus = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()


# Compatibility alias ensuring backward compatibility across all modules
CANService = CANManager
