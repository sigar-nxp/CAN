"""
Core lock service providing asynchronous action dispatch.

Acts as the primary entrypoint for business logic requiring interaction
with a lock device. Inherits command construction from LockCommands
and leverages CANListener data for real-time status fetching.
"""


"""
PS Locks OIP

Lock Service
"""

from canbus.parser import CANParser
from config.globals import can_service

from .lock_commands import LockCommands
from .lock_status import LockStatus
from .lock_wait import LockWait


class LockService(
    LockCommands,
    LockStatus,
    LockWait,
):

    def __init__(
        self,
        channel: str = "can0",
        simulation: bool = False,
    ):
        # We now use the global CAN service
        # so that the listener can process incoming frames correctly.
        self.can = can_service
        self.parser = CANParser()

    def close(self):
        # Shutdown should be managed globally,
        # not locally per LockService instance.
        pass

