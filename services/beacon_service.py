"""
PS Locks OIP Master Beacon Service.

Runs a background thread to broadcast periodic master beacon frames (0xB0).
These frames provide heartbeat timing, network synchronization, and global
control signals (like Master Failover/Takeover) to all locks on the bus.
"""

import threading
import logging

import can

from canbus.commands import build_master_beacon_frame
from canbus.service import CANService
logger = logging.getLogger(__name__)



class BeaconService:
    def __init__(self, can_service: CANService):
        self.can = can_service
        self.interval_sec = 1.0
        self.total_beacons_sent = 0
        self.running = False
        self._thread = None
        self._stop_event = threading.Event()

    def start(self):
        """Start the beacon background thread."""
        if self.running:
            return
        self.running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the beacon background thread gracefully."""
        self.running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _loop(self):
        """Internal loop to periodically broadcast the Master Beacon frame."""
        while not self._stop_event.is_set():
            try:
                frame = build_master_beacon_frame(
                    interval_ms=int(self.interval_sec * 1000)
                )
                self.can.send(frame)
                self.total_beacons_sent += 1
            except can.CanError as e:
                logger.error(f"BeaconService CAN error: {e}")
            except Exception as e:
                logger.error(f"BeaconService unexpected error: {e}")
            
            # Wait for the next interval or until stopped
            self._stop_event.wait(self.interval_sec)
