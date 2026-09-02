"""
Database models for representing physical devices in the system.

Contains the SQLAlchemy ORM models that map application objects to the
underlying database tables for persisting device information and state.
"""

from sqlalchemy import Column, Integer, String, Float
from database.database import Base
import time

class Device(Base):
    """
    SQLAlchemy model representing a physical lock or device.
    
    Attributes:
        device_id (int): Primary key, unique database identifier (CAN ID) for the device.
        name (str): Human-readable name of the device.
        lock_mode (int): Lock operation mode.
        auto_close_timeout (int): Auto close timeout in seconds.
        behavior_flags (int): Behavior flags.
        last_seen (float): Last time the device was seen.
    """
    __tablename__ = "devices"

    device_id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, default="Lock")
    lock_mode = Column(Integer, default=1)
    auto_close_timeout = Column(Integer, default=3)
    behavior_flags = Column(Integer, default=0)
    last_seen = Column(Float, default=time.time)
