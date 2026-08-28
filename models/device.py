"""
Database models for representing physical devices in the system.

Contains the SQLAlchemy ORM models that map application objects to the
underlying database tables for persisting device information and state.
"""

from sqlalchemy import Column, Integer, String
from database.database import Base

class Device(Base):
    """
    SQLAlchemy model representing a physical lock or device.
    
    Attributes:
        id (int): Primary key, unique database identifier for the device.
        name (str): Human-readable name of the device.
        device_type (str): Type categorization of the device.
        interface (str): Hardware interface used (e.g., "CAN").
        can_address (int): CAN node ID assigned to the device.
        status (str): Current operating status (e.g., "OFFLINE").
        firmware (str): Firmware version of the device.
        serial_number (str): Unique serial number of the hardware.
        location (str): Physical location descriptor for the device.
    """
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)

    name = Column(String, nullable=False)

    device_type = Column(String, nullable=False)

    interface = Column(String, default="CAN")

    can_address = Column(Integer)

    status = Column(String, default="OFFLINE")

    firmware = Column(String)

    serial_number = Column(String)

    location = Column(String)