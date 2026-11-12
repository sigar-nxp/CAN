"""
Runtime settings for the PS Locks OIP gateway.

Holds the locker operation mode (offline fitness-gym "free locker selection"
versus the legacy standard behaviour) and the node-ID plan used for automatic
provisioning. The mode can be preset through the ``LOCKER_OPERATION_MODE``
environment variable and switched at runtime through the system API.
"""

import logging
import os
import threading

from database.database import get_system_setting, set_system_setting

logger = logging.getLogger(__name__)

# ----------------------------------------------------------
# Locker operation mode
# ----------------------------------------------------------

LOCKER_MODE_GYM = "gym"
LOCKER_MODE_STANDARD = "standard"
VALID_LOCKER_MODES = (LOCKER_MODE_GYM, LOCKER_MODE_STANDARD)
LOCKER_MODE_SETTING_KEY = "locker_operation_mode"

_mode_lock = threading.Lock()


def normalize_locker_mode(mode) -> str:
    """Returns the canonical lowercase mode string or raises ValueError if unknown."""
    value = str(mode).strip().lower() if mode is not None else ""
    if value not in VALID_LOCKER_MODES:
        raise ValueError(
            f"Invalid locker operation mode {mode!r}; expected one of {VALID_LOCKER_MODES}"
        )
    return value


def _mode_from_environment() -> str:
    raw = os.environ.get("LOCKER_OPERATION_MODE", LOCKER_MODE_GYM)
    try:
        return normalize_locker_mode(raw)
    except ValueError:
        logger.warning("Unknown LOCKER_OPERATION_MODE %r; falling back to %r", raw, LOCKER_MODE_GYM)
        return LOCKER_MODE_GYM


# Active mode. Always read it through get_locker_operation_mode() so runtime changes apply.
LOCKER_OPERATION_MODE = _mode_from_environment()


def load_locker_operation_mode(bind=None) -> str:
    """
    Loads the persisted locker operation mode from SQLite into runtime state.

    If no valid mode is stored in the database yet, falls back to the environment default
    ("gym" unless LOCKER_OPERATION_MODE is set) and persists it in SQLite.
    """
    global LOCKER_OPERATION_MODE
    stored = get_system_setting(LOCKER_MODE_SETTING_KEY, default=None, bind=bind)
    value = None
    if stored is not None:
        try:
            value = normalize_locker_mode(stored)
        except ValueError:
            logger.warning("Invalid persisted locker_operation_mode %r; resetting to default", stored)
    if value is None:
        value = _mode_from_environment()
        set_system_setting(LOCKER_MODE_SETTING_KEY, value, bind=bind)
    with _mode_lock:
        LOCKER_OPERATION_MODE = value
    return value


def get_locker_operation_mode() -> str:
    """Returns the currently active locker operation mode."""
    return LOCKER_OPERATION_MODE


def set_locker_operation_mode(mode, persist: bool = True, bind=None) -> str:
    """Switches the locker operation mode at runtime and persists it to SQLite."""
    global LOCKER_OPERATION_MODE
    value = normalize_locker_mode(mode)
    with _mode_lock:
        LOCKER_OPERATION_MODE = value
    if persist:
        set_system_setting(LOCKER_MODE_SETTING_KEY, value, bind=bind)
    return value


def is_gym_mode() -> bool:
    """True when the gateway performs free-locker selection decisions itself."""
    return LOCKER_OPERATION_MODE == LOCKER_MODE_GYM


# Initialize from the persistent SQLite store at module load time.
load_locker_operation_mode()


# ----------------------------------------------------------
# Node-ID plan (gym mode)
# ----------------------------------------------------------

# Factory-tested locks ship with Device ID 1 from the EOL test. Node 1 is therefore
# reserved as the staging/onboarding ID and never kept as a registered locker.
STAGING_NODE_ID = 1

# Node 127 (0x7F) is the CAN unprovisioned default address used during discovery,
# unassigned state, and factory reset. It must never be registered as an operational locker.
UNPROVISIONED_NODE_ID = 0x7F  # 127
# Lockers are assigned the lowest free ID in this inclusive range (48 lockers per Pi).
GYM_NODE_ID_MIN = 2
GYM_NODE_ID_MAX = 49

# Seconds to wait for a factory-reset lock to re-announce itself as unassigned.
GYM_PROVISION_WINDOW_S = 20.0
# Seconds without new unassigned announcements after which a provisioning cycle ends early.
GYM_PROVISION_QUIET_S = 5.0
# Minimum seconds between two staging cycles (prevents repeated factory resets).
GYM_PROVISION_COOLDOWN_S = 10.0
