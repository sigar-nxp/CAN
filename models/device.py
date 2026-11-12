"""
Database models for representing physical devices in the system.

Contains the SQLAlchemy ORM models that map application objects to the
underlying database tables for persisting device information and state.
"""

from sqlalchemy import Column, Integer, String, Float, DateTime
from database.database import Base
import time

LOCKER_STATE_VACANT = "vacant"
LOCKER_STATE_OCCUPIED = "occupied"


def _default_custom_name(context) -> str:
    """Builds the default display name ("Locker <id>") from the row being inserted."""
    return f"Locker {context.get_current_parameters()['device_id']}"


class Device(Base):
    """
    SQLAlchemy model representing a physical lock or device.
    
    Attributes:
        device_id (int): Primary key, unique database identifier (CAN ID) for the device.
        name (str): Human-readable name of the device.
        custom_name (str): Operator-editable locker label (default "Locker <id>").
        lock_mode (int): Lock operation mode.
        auto_close_timeout (int): Auto close timeout in seconds.
        behavior_flags (int): Behavior flags.
        last_seen (float): Last time the device was seen.
        locker_state (str): Gym occupancy state, "vacant" or "occupied".
        assigned_card_uid (str): Normalized hex UID of the member card occupying the locker, or None.
        occupied_at (datetime): UTC timestamp at which the locker was occupied, or None.
    """
    __tablename__ = "devices"

    device_id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, default="Lock")
    custom_name = Column(String, nullable=True, default=_default_custom_name)
    hardware_uid = Column(String, nullable=True, index=True)
    lock_mode = Column(Integer, default=1)
    auto_close_timeout = Column(Integer, default=3)
    behavior_flags = Column(Integer, default=0)
    active_slot = Column(Integer, default=0)
    last_seen = Column(Float, default=time.time)
    locker_state = Column(String, nullable=False, default=LOCKER_STATE_VACANT, server_default=LOCKER_STATE_VACANT)
    assigned_card_uid = Column(String, nullable=True)
    occupied_at = Column(DateTime, nullable=True)
