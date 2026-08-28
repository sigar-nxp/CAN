"""
Utility routines for awaiting asynchronous device states.

Provides non-blocking polling loops that block caller execution until
a target lock transitions to a desired hardware state (e.g. LOCKED)
as verified by the CANListener, with configurable timeouts.
"""


"""
PS Locks OIP

Lock Wait Functions
"""

import time


class LockWait:

    def wait_for_lock_state(
        self,
        device_id,
        state,
        timeout=5.0,
    ):

        start = time.time()

        while time.time() - start < timeout:

            status = self.get_status(device_id)

            if status is None:
                continue

            if status.lock_state == state:
                return True

        return False

    def wait(
        self,
        seconds,
    ):

        time.sleep(seconds)
