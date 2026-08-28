"""
PS Locks OIP

CAN Protocol Data Classes. Defines the structural representations for messages
flowing through the CAN bus interface.
"""

from dataclasses import dataclass

from .constants import DEVICE_MASK, TYPE_MASK


@dataclass(slots=True)
class CANFrame:
    """
    Internal representation of a CAN bus message (frame).
    
    Attributes:
        arbitration_id (int): Standard 11-bit CAN Identifier.
        data (bytes): Byte payload of the message.
    """

    arbitration_id: int
    data: bytes

    @property
    def device_id(self) -> int:
        """Extracts the 8-bit target device ID from the arbitration ID."""
        return self.arbitration_id & DEVICE_MASK

    @property
    def message_type(self) -> int:
        """Extracts the upper bits dictating message category/type."""
        return self.arbitration_id & TYPE_MASK

    @property
    def dlc(self) -> int:
        """Gets the Data Length Code (DLC) i.e. payload length."""
        return len(self.data)
