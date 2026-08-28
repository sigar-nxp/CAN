"""
Device assignment and initialization orchestration.

Handles "Variant A" auto-discovery and unassigned lock claiming. Maps an
unassigned physical UID32 hardware address to a logical network node ID
(0-127), and configures the hardware communication bitrate.
"""


"""
PS Locks OIP

Provisioning Service
"""

import time

from config.globals import can_service, can_listener
from canbus.commands import (
    request_full_uid,
    assign_id,
)
from canbus.constants import BITRATE_100K


class ProvisioningService:

    def __init__(self):
        # We now use the global CAN service so that the listener
        # continues to correctly receive responses on the bus.
        self.can = can_service

    # ---------------------------------------------------------

    def assign_device_id(
        self,
        uid32_hex: str,
        target_device_id: int,
        bitrate_code: int = BITRATE_100K,
        timeout: float = 5.0,
    ):
        try:
            uid32_bytes = bytes.fromhex(uid32_hex)
        except ValueError:
            raise ValueError("Invalid uid32 format. Must be hex string.")

        # Step 1: Send request_full_uid
        can_listener.clear_uid_parts()
        self.can.send(request_full_uid(uid32_bytes))

        # Wait for UID parts
        start = time.time()
        full_uid = None
        while time.time() - start < timeout:
            with can_listener.lock:
                p1 = can_listener.latest_uid_part1
                p2 = can_listener.latest_uid_part2
            
            if p1 is not None and p2 is not None:
                full_uid = p1 + p2
                break
            time.sleep(0.1)

        if not full_uid:
            raise TimeoutError("Failed to receive full UID from device within timeout.")

        # Store full UID in unassigned device just in case
        with can_listener.lock:
            if uid32_hex in can_listener.unassigned_devices:
                can_listener.unassigned_devices[uid32_hex].full_uid = full_uid

        # Step 2: Send assign_id
        self.can.send(assign_id(uid32_bytes, target_device_id, bitrate_code))

        # Step 3: Verify the device appears on its new CAN ID
        start = time.time()
        verified = False
        while time.time() - start < timeout:
            dev = can_listener.get_device(target_device_id)
            if dev.last_seen > start:
                verified = True
                break
            time.sleep(0.1)

        if not verified:
            raise TimeoutError(f"Device did not appear on new ID {target_device_id} after assignment.")

        # Step 4: Remove from unassigned_locks
        with can_listener.lock:
            if uid32_hex in can_listener.unassigned_devices:
                del can_listener.unassigned_devices[uid32_hex]

        return {
            "device_id": target_device_id,
            "full_uid_hex": full_uid.hex(),
            "status": "provisioned"
        }

    # ---------------------------------------------------------

    def close(self):
        # Shutdown should be managed globally now.
        pass
