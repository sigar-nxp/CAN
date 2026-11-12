"""
Global application configurations and service singletons.

This module initializes the primary hardware interface and background
services used throughout the application to maintain a single source
of truth for the CAN bus connection and event polling.
"""

import os

from canbus.service import CANService
from canbus.listener import CANListener
from services.beacon_service import BeaconService
from services.gym_service import GymLockerService
from services.gym_provisioning import GymProvisioner

sim = os.environ.get("CAN_SIMULATION", "0") in ("1", "true", "True")
can_service = CANService(channel="can0", bitrate=50000, simulation=sim)
can_listener = CANListener(can_service)
beacon_service = BeaconService(can_service)

# Gym mode (free locker selection + auto-provisioning). The hooks are always wired but
# the handlers only act while settings.LOCKER_OPERATION_MODE is "gym".
gym_service = GymLockerService(can_listener, sender=can_service.send)
gym_provisioner = GymProvisioner(can_listener, sender=can_service.send)
can_listener.card_event_handler = gym_service.handle_card_event
can_listener.wl_auto_delete_handler = gym_service.handle_wl_auto_delete
can_listener.staging_frame_handler = gym_provisioner.on_staging_frame
