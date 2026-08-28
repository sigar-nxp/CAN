"""
PS Locks OIP

Utility functions for constructing raw CAN frames with standard ID layouts
and byte payloads ready for hardware transmission.
"""

from .protocol import CANFrame


def build_frame(arbitration_id: int, payload: bytes) -> CANFrame:
    """
    Creates an internal CANFrame object with the given ID and byte data.
    
    Args:
        arbitration_id (int): Complete CAN ID (message type + device ID).
        payload (bytes): Data bytes up to 8 bytes long (standard CAN).
        
    Returns:
        CANFrame: The constructed dataclass instance.
    """
    return CANFrame(
        arbitration_id=arbitration_id,
        data=payload
    )


def empty_frame(arbitration_id: int) -> CANFrame:
    """
    Creates an empty CAN frame (zero DLC) for the given arbitration ID.
    
    Args:
        arbitration_id (int): Complete CAN ID.
        
    Returns:
        CANFrame: An empty payload CANFrame.
    """
    return CANFrame(
        arbitration_id=arbitration_id,
        data=b""
    )
