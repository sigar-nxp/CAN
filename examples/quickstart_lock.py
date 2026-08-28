# Quickstart example demonstrating LockService in simulation mode
from services.lock_service import LockService

lock = LockService(simulation=True)

# Query device status
lock.request_status(1)

# Control locking mechanism
lock.open(1)
lock.open_hold(1)
lock.open_reset(1)

# Actuator feedback
lock.led_green(1)
lock.led_red(1)
lock.led_off(1)

lock.buzzer_on(1)
lock.buzzer_off(1)

lock.close()