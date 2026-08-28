"""
PS Locks OIP Lock Commands Facade.

Provides a unified object-oriented wrapper mapping high-level action calls
(e.g., open_hold, buzz_play) into corresponding low-level CAN bus messages,
and sending them directly via the injected CANService instance.
"""

from canbus.commands import (
    open_lock,
    open_hold,
    open_reset,
    request_status,
    request_health,
    request_version,
    request_device_info,
    request_full_uid,
    device_reset,
    factory_reset,
    assign_id,
    led_set,
    led_reset,
    buzz_play,
    buzz_stop,
)

from canbus.constants import (
    LED_GREEN,
    LED_RED,
    BUZZ_OK,
)


class LockCommands:

    # ---------------------------------------------------------
    # Low-Level
    # ---------------------------------------------------------

    def send(self, frame):

        self.can.send(frame)

    def receive(self, timeout=1.0):

        return self.can.receive(timeout)

    # ---------------------------------------------------------
    # Lock
    # ---------------------------------------------------------

    def request_status(self, device_id, lock_id=1):
        self.send(request_status(device_id, lock_id=lock_id))

    def open(self, device_id, lock_id=1):
        self.send(open_lock(device_id, lock_id=lock_id))

    def open_hold(self, device_id, lock_id=1):
        self.send(open_hold(device_id, lock_id=lock_id))

    def open_reset(self, device_id, lock_id=1):
        self.send(open_reset(device_id, lock_id=lock_id))

    # ---------------------------------------------------------
    # LED
    # ---------------------------------------------------------

    def led_green(self, device_id, lock_id=1):
        self.send(led_set(device_id, LED_GREEN, lock_id=lock_id))

    def led_red(self, device_id, lock_id=1):
        self.send(led_set(device_id, LED_RED, lock_id=lock_id))

    def led_off(self, device_id, lock_id=1):
        self.send(led_reset(device_id, lock_id=lock_id))

    # ---------------------------------------------------------
    # Buzzer
    # ---------------------------------------------------------

    def buzzer_on(self, device_id, lock_id=1):
        self.send(buzz_play(device_id, BUZZ_OK, lock_id=lock_id))

    def buzzer_off(self, device_id, lock_id=1):
        self.send(buzz_stop(device_id, lock_id=lock_id))

    # ---------------------------------------------------------
    # System
    # ---------------------------------------------------------

    def request_health(self, device_id, lock_id=1):
        self.send(request_health(device_id, lock_id=lock_id))

    def request_version(self, device_id, lock_id=1):
        self.send(request_version(device_id, lock_id=lock_id))

    def request_device_info(self, device_id, lock_id=1):
        self.send(request_device_info(device_id, lock_id=lock_id))

    def device_reset(self, device_id, lock_id=1):
        self.send(device_reset(device_id, lock_id=lock_id))

    def factory_reset(self, device_id, lock_id=1):
        self.send(factory_reset(device_id, lock_id=lock_id))

    # ---------------------------------------------------------
    # Provisioning
    # ---------------------------------------------------------

    def request_full_uid(self, uid32):

        self.send(
            request_full_uid(uid32)
        )

    def assign_id(
        self,
        uid32,
        device_id,
    ):

        self.send(
            assign_id(
                uid32,
                device_id,
            )
        )

    # ---------------------------------------------------------
    # WHITELIST & CONFIG
    # ---------------------------------------------------------

    def request_wl_info(self, device_id, lock_id=1):
        from canbus.commands import request_wl_info
        self.send(request_wl_info(device_id, lock_id=lock_id))

    def request_wl_list_v2(self, device_id, start_index=1, page_size=32, lock_id=1):
        from canbus.commands import request_wl_list_v2
        self.send(request_wl_list_v2(device_id, start_index, page_size, lock_id=lock_id))

    def delete_wl_slot(self, device_id, slot_index, lock_id=1):
        from canbus.commands import delete_wl_slot
        self.send(delete_wl_slot(device_id, slot_index, lock_id=lock_id))

    def wl_clear(self, device_id, lock_id=1):
        from canbus.commands import wl_clear
        self.send(wl_clear(device_id, lock_id=lock_id))

    def set_wl_slot_policy(self, device_id, slot_index, policy, open_action, flags=0, lock_id=1):
        from canbus.commands import set_wl_slot_policy
        self.send(set_wl_slot_policy(device_id, slot_index, policy, open_action, flags, lock_id=lock_id))

    def set_lock_mode(self, device_id, lock_mode, auto_close_timeout_s=3, behavior_flags=0, door_warning_delay_s=2, door_release_delay_s=5, lock_id=1):
        from canbus.commands import set_lock_mode
        self.send(set_lock_mode(device_id, lock_mode, auto_close_timeout_s, behavior_flags, door_warning_delay_s, door_release_delay_s, lock_id=lock_id))

    def request_lock_mode(self, device_id, lock_id=1):
        from canbus.commands import request_lock_mode
        self.send(request_lock_mode(device_id, lock_id=lock_id))

    def request_occupancy_state(self, device_id, lock_id=1):
        from canbus.commands import request_occupancy_state
        self.send(request_occupancy_state(device_id, lock_id=lock_id))

    def auth_resp(self, device_id, result, action, lock_id=1):
        from canbus.commands import auth_resp
        self.send(auth_resp(device_id, result, action, lock_id=lock_id))
