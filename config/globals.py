"""
Global application configurations and service singletons.

This module initializes the primary hardware interface and background
services used throughout the application to maintain a single source
of truth for the CAN bus connection and event polling.
"""

from canbus.service import CANService
from canbus.listener import CANListener
from services.beacon_service import BeaconService

can_service = CANService(channel="can0", bitrate=50000, simulation=False)
can_listener = CANListener(can_service)
beacon_service = BeaconService(can_service)
