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
from typing import Callable, Dict, List, Optional

import can

from .constants import (
    FITNET_CAN_ID_OTA_ACK_BASE,
    FITNET_CAN_ID_OTA_BASE_MASK,
    FITNET_CAN_ID_OTA_CMD_BASE,
    FITNET_CAN_ID_OTA_DEVICE_MASK,
)
from .protocol import CANFrame
logger = logging.getLogger(__name__)




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
            self.reader_thread.join(timeout=2.0)
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
        for callback in self.standard_callbacks:
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
