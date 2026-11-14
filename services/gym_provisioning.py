"""
Gym-mode auto-provisioning of factory-tested locks.

Every lock leaves the EOL test with Device ID 1 persisted in its flash, so
all new locks look identical on the bus. Node 1 is therefore reserved as the
staging/onboarding ID. The firmware only accepts ASSIGN_ID (0x400|0x7F) while
it is unprovisioned, and offers no command to change the ID of an operational
node, so the only way to move a lock off ID 1 is:

1. FACTORY_RESET (0xFB) addressed to node 1. The lock wipes its config page,
   reboots as 0x7F and announces its UID32 with ID_REQUEST on 0x57F.
2. The announcement is answered with the regular UID assignment
   (ProvisioningService: REQUEST_FULL_UID + ASSIGN_ID) using the lowest free
   node ID in the gym range 2..49.
3. The full hardware UID, the new ID and the default "Locker <id>" label are
   persisted (deduplicated by hardware UID) and the dashboard is notified.

Only one lock should be connected to the bus at ID 1 at a time: two nodes
transmitting the same arbitration ID with different payloads corrupt each
other's frames. The cycle still copes with several locks resetting together
because every unassigned announcement inside the provisioning window is
assigned its own ID.
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from canbus.commands import factory_reset
from canbus.constants import BITRATE_50K
from canbus.listener import normalize_uid, uids_match
from config import settings
from database.database import SessionLocal
from models.device import Device, LOCKER_STATE_VACANT

logger = logging.getLogger(__name__)

STAGING_LOCK_ID = 1
ASSIGN_ATTEMPTS = 2


class NoFreeNodeIdError(RuntimeError):
    """Raised when every node ID in the gym range is already in use."""


def _default_broadcaster(event: str, data: dict) -> None:
    from api.stream import broadcast_sse_event
    broadcast_sse_event(event, data)


class GymProvisioner:
    """Moves locks that appear on the staging ID 1 to the next free gym node ID."""

    def __init__(
        self,
        listener,
        sender: Callable[[Any], Any],
        provisioning=None,
        broadcaster: Optional[Callable[[str, dict], None]] = None,
        session_factory: Callable[[], Any] = SessionLocal,
        window_s: Optional[float] = None,
        quiet_s: Optional[float] = None,
        cooldown_s: Optional[float] = None,
        poll_s: float = 0.2,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.listener = listener
        self.sender = sender
        self._provisioning = provisioning
        self.broadcaster = broadcaster or _default_broadcaster
        self.session_factory = session_factory
        self.window_s = settings.GYM_PROVISION_WINDOW_S if window_s is None else window_s
        self.quiet_s = settings.GYM_PROVISION_QUIET_S if quiet_s is None else quiet_s
        self.cooldown_s = settings.GYM_PROVISION_COOLDOWN_S if cooldown_s is None else cooldown_s
        self.poll_s = poll_s
        self.clock = clock
        self.sleep = sleep

        self._state_lock = threading.Lock()
        self._inflight = False
        self._next_allowed = 0.0
        self._id_lock = threading.Lock()
        self._reserved: set = set()

    # ------------------------------------------------------------------
    # Trigger
    # ------------------------------------------------------------------

    def on_staging_frame(self, device_id: int) -> bool:
        """
        Listener hook: a node reported on the staging ID. Starts a background cycle unless
        one is running or the cooldown has not elapsed. Returns True if a cycle was started.
        """
        if not settings.is_gym_mode() or device_id != settings.STAGING_NODE_ID:
            return False
        now = self.clock()
        with self._state_lock:
            if self._inflight or now < self._next_allowed:
                return False
            self._inflight = True
        # Provisioning waits for CAN replies, so it must not run on the CAN reader thread
        threading.Thread(target=self._run_guarded, name="gym-staging", daemon=True).start()
        return True

    def _run_guarded(self) -> None:
        try:
            self.run_cycle()
        except Exception as exc:
            logger.error(f"Gym staging cycle failed: {exc}")
        finally:
            with self._state_lock:
                self._inflight = False
                self._next_allowed = self.clock() + self.cooldown_s

    # ------------------------------------------------------------------
    # Cycle
    # ------------------------------------------------------------------

    def run_cycle(self) -> List[Dict[str, Any]]:
        """
        Factory-resets the lock(s) on node 1 and assigns every re-announcing node its own ID.

        Returns:
            One result dict per successfully provisioned lock.
        """
        if self.free_node_id() is None:
            self._log("PROVISION_FAILED", None,
                      f"No free node ID in {settings.GYM_NODE_ID_MIN}..{settings.GYM_NODE_ID_MAX}; "
                      "staging lock left untouched")
            return []

        reset_ts = self.clock()
        if not self._send(factory_reset(settings.STAGING_NODE_ID, lock_id=STAGING_LOCK_ID)):
            self._log("PROVISION_FAILED", None, "Could not send FACTORY_RESET to staging node 1")
            return []
        self._log("PROVISION_START", None,
                  f"Lock on staging node {settings.STAGING_NODE_ID} factory-reset for re-assignment")
        self._purge_transient_nodes()

        results: List[Dict[str, Any]] = []
        handled: set = set()
        last_activity = reset_ts
        deadline = reset_ts + self.window_s
        while self.clock() < deadline:
            pending = self.listener.get_unassigned_devices()
            for uid_hex, info in list(pending.items()):
                if uid_hex in handled or (info.get("last_seen") or 0.0) < reset_ts:
                    continue
                handled.add(uid_hex)
                result = self._provision_with_retry(uid_hex)
                last_activity = self.clock()
                if result is not None:
                    results.append(result)
            if handled and self.clock() - last_activity >= self.quiet_s:
                break
            self.sleep(self.poll_s)

        if not handled:
            self._log("PROVISION_FAILED", None,
                      "Staging lock did not re-announce itself after FACTORY_RESET")
        self._purge_transient_nodes()
        return results

    def _provision_with_retry(self, uid_hex: str) -> Optional[Dict[str, Any]]:
        for attempt in range(1, ASSIGN_ATTEMPTS + 1):
            try:
                return self.provision_uid(uid_hex)
            except NoFreeNodeIdError:
                return None
            except Exception as exc:
                logger.warning(f"Provisioning of {uid_hex} failed (attempt {attempt}): {exc}")
                self._log("PROVISION_FAILED", uid_hex, f"Assignment attempt {attempt} failed: {exc}")
        return None

    # ------------------------------------------------------------------
    # Assignment
    # ------------------------------------------------------------------

    def provision_uid(self, uid_hex: str) -> Dict[str, Any]:
        """
        Assigns a node ID to an unassigned lock announcing UID32 ``uid_hex`` and persists it.

        A lock whose hardware UID is already registered keeps its previous ID (and name), so a
        re-onboarded lock never creates a second node.

        Raises:
            NoFreeNodeIdError: when the gym ID range is exhausted.
        """
        node_id, reused = self.select_node_id(uid_hex)
        if node_id is None:
            self._log("PROVISION_FAILED", uid_hex,
                      f"No free node ID in {settings.GYM_NODE_ID_MIN}..{settings.GYM_NODE_ID_MAX}")
            raise NoFreeNodeIdError("No free node ID available")
        try:
            outcome = self._get_provisioning().assign_device_id(uid_hex, node_id, BITRATE_50K)
        finally:
            with self._id_lock:
                self._reserved.discard(node_id)

        full_uid = normalize_uid((outcome or {}).get("full_uid_hex")) or normalize_uid(uid_hex)
        # Deduplicate with listener to prune any old node records associated with this UID
        old_id = self.listener.deduplicate_uid(full_uid, node_id)
        if old_id is not None:
            self._broadcast("device_removed", {"device_id": old_id})
        # Ensure any transient node 127 (0x7F) entry is dropped
        self._purge_transient_nodes(settings.UNPROVISIONED_NODE_ID)
        self._finalize(node_id, full_uid, reused)
        self._log("DEVICE_PROVISIONED", full_uid,
                  f"Staging lock {full_uid} assigned Node {node_id}" + (" (previous ID reused)" if reused else ""),
                  device_id=node_id)
        self._broadcast_device(node_id)
        return {"device_id": node_id, "hardware_uid": full_uid, "reused": reused}

    def select_node_id(self, uid_hex: Optional[str]) -> Tuple[Optional[int], bool]:
        """
        Picks the node ID for a UID and reserves it: the already registered ID of the same
        hardware UID if there is one, otherwise the lowest free ID of the gym range.
        """
        with self._id_lock:
            used = self._used_ids()
            if uid_hex:
                known = self._registered_id_for(uid_hex)
                if known is not None and known not in self._reserved:
                    self._reserved.add(known)
                    return known, True
            for candidate in range(settings.GYM_NODE_ID_MIN, settings.GYM_NODE_ID_MAX + 1):
                if candidate not in used and candidate not in self._reserved:
                    self._reserved.add(candidate)
                    return candidate, False
        return None, False

    def free_node_id(self) -> Optional[int]:
        """Returns the lowest free gym node ID without reserving it."""
        with self._id_lock:
            used = self._used_ids() | self._reserved
            for candidate in range(settings.GYM_NODE_ID_MIN, settings.GYM_NODE_ID_MAX + 1):
                if candidate not in used:
                    return candidate
        return None

    def _used_ids(self) -> set:
        with self.listener.lock:
            used = set(self.listener.devices.keys())
        with self.session_factory() as db:
            used.update(row[0] for row in db.query(Device.device_id).all())
        used.difference_update(settings.TRANSIENT_NODE_IDS)
        return used

    def _registered_id_for(self, uid_hex: str) -> Optional[int]:
        """Finds the gym-range node that already carries this hardware UID (UID32 prefix match)."""
        low, high = settings.GYM_NODE_ID_MIN, settings.GYM_NODE_ID_MAX
        with self.listener.lock:
            for dev_id, dev in self.listener.devices.items():
                if low <= dev_id <= high and (
                    uids_match(dev.hardware_uid, uid_hex) or uids_match(dev.full_uid, uid_hex)
                ):
                    return dev_id
        with self.session_factory() as db:
            for row in db.query(Device).all():
                if low <= row.device_id <= high and row.hardware_uid and uids_match(row.hardware_uid, uid_hex):
                    return row.device_id
        return None

    # ------------------------------------------------------------------
    # Persistence & notifications
    # ------------------------------------------------------------------

    def _finalize(self, node_id: int, full_uid: Optional[str], reused: bool) -> None:
        """Persists hardware UID, default label and a clean vacant state for the node."""
        dev = self.listener.get_device(node_id)  # creates the in-memory node and DB row if missing
        with self.session_factory() as db:
            row = db.get(Device, node_id)
            if row is not None:
                row.hardware_uid = full_uid
                if not row.custom_name:
                    row.custom_name = f"Locker {node_id}"
                if reused:
                    # The factory reset wiped the lock's credentials, so any old claim is void
                    row.locker_state = LOCKER_STATE_VACANT
                    row.assigned_card_uid = None
                    row.occupied_at = None
                db.commit()
        with self.listener.lock:
            dev.hardware_uid = full_uid
            dev.full_uid = full_uid
            dev.is_unprovisioned = False
            if not dev.custom_name:
                dev.custom_name = f"Locker {node_id}"
            if reused:
                dev.locker_state = LOCKER_STATE_VACANT
                dev.assigned_card_uid = None
                dev.occupied_at = None

    def _purge_transient_nodes(self, *node_ids: int) -> None:
        """Removes transient node IDs (default: staging 1 and unprovisioned 127) from memory/DB and notifies dashboards."""
        targets = node_ids or settings.TRANSIENT_NODE_IDS
        for transient_id in targets:
            self.listener.remove_device(transient_id)
            self._broadcast("device_removed", {"device_id": transient_id})

    def _broadcast_device(self, device_id: int) -> None:
        with self.listener.lock:
            dev = self.listener.devices.get(device_id)
            payload = dev.to_stream_payload() if dev is not None else None
        if payload is not None:
            self._broadcast("device_update", payload)

    def _broadcast(self, event: str, data: dict) -> None:
        try:
            self.broadcaster(event, data)
        except Exception as exc:
            logger.error(f"Gym SSE broadcast failed: {exc}")

    def _send(self, frame) -> bool:
        try:
            return self.sender(frame) is not False
        except Exception as exc:
            logger.error(f"Gym provisioning CAN transmit failed: {exc}")
            return False

    def _get_provisioning(self):
        if self._provisioning is None:
            # Imported lazily: the service module binds to the global CAN singletons
            from services.provisioning_service import ProvisioningService
            self._provisioning = ProvisioningService()
        return self._provisioning

    def _log(self, event_type: str, uid: Optional[str], details: str, device_id: Optional[int] = None) -> None:
        try:
            self.listener.add_log(
                settings.STAGING_NODE_ID if device_id is None else device_id,
                event_type, uid, details, direction="INTERNAL",
            )
        except Exception as exc:
            logger.error(f"Gym provisioning log failed: {exc}")

