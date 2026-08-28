"""
Diagnostic and state retrieval operations for locks.

Exposes blocking workflows that actively request status or health
diagnostics from a lock, await the specific acknowledgment reply
via the listener, and return the populated data structures.
"""


"""
PS Locks OIP

Lock Status
"""

import time
from config.globals import can_listener
from canbus.parser import StatusBasic, HealthStatus

class LockStatus:

    # ---------------------------------------------------------

    def get_status(
        self,
        device_id,
        timeout=2.0,
    ):
        self.request_status(device_id)
        start = time.time()
        
        while time.time() - start < timeout:
            dev = can_listener.get_device(device_id)
            if dev.last_seen >= start and dev.lock_state is not None:
                return StatusBasic(
                    device_id=dev.device_id,
                    lock_id=0, # assumed 0 if not tracked
                    lock_state=dev.lock_state,
                    lock_error=dev.lock_error
                )
            time.sleep(0.1)

        return None

    # ---------------------------------------------------------

    def get_health(
        self,
        device_id,
        timeout=2.0,
    ):
        self.request_health(device_id)
        start = time.time()

        while time.time() - start < timeout:
            dev = can_listener.get_device(device_id)
            if dev.last_seen >= start and dev.health_data is not None:
                return HealthStatus(
                    device_id=dev.device_id,
                    data=dev.health_data
                )
            time.sleep(0.1)

        return None

    # ---------------------------------------------------------

    def wait_for_ack(
        self,
        command,
        device_id,
        timeout=2.0,
    ):
        start = time.time()

        while time.time() - start < timeout:
            dev = can_listener.get_device(device_id)
            if dev.last_ack_time >= start and dev.last_ack_command == command:
                return True
            time.sleep(0.1)

        return False

    # ---------------------------------------------------------

    def print_status(
        self,
        device_id,
    ):
        status = self.get_status(device_id)

        if status is None:
            print("Kein Status erhalten.")
            return

        print("----------------------------")
        print(f"Device : {status.device_id}")
        print(f"Lock   : {status.lock_id}")
        print(f"State  : {status.lock_state}")
        print(f"Error  : {status.lock_error}")
        print("----------------------------")

    # ---------------------------------------------------------

    def print_health(
        self,
        device_id,
    ):
        health = self.get_health(device_id)

        if health is None:
            print("Keine Health-Daten.")
            return

        print("----------------------------")
        print(f"Device : {health.device_id}")
        print(
            "Health : "
            + " ".join(f"{b:02X}" for b in health.data)
        )
        print("----------------------------")

