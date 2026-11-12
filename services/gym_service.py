"""
Offline fitness-gym locker logic ("free locker selection").

In gym mode the gateway itself decides on every card presentation that a lock
reports as a cloud authorization request (STATUS 0x11) and answers with the
existing AUTH_RESP command (0x60). The rules are:

* VACANT locker + card that holds no other locker  -> ALLOW/CLOSE, locker becomes OCCUPIED
* VACANT locker + card that already holds a locker -> DENY (1 card = max 1 locker)
* OCCUPIED locker + owner card                     -> ALLOW/OPEN, locker becomes VACANT
* OCCUPIED locker + any other card                 -> DENY, bolt stays locked

Once a locker is claimed the firmware stores the owner card in its ephemeral
Slot 1, so the owner's next swipe is answered locally by the lock (STATUS 0x10
OPEN_OK) without asking the gateway. Those local actions, and the firmware's
own guest-credential auto-delete, are mirrored here so the database never
keeps a locker marked occupied after the hardware has released it.
"""

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from canbus.commands import auth_resp, delete_wl_slot, open_hold
from canbus.constants import (
    AUTH_ACTION_CLOSE,
    AUTH_ACTION_NONE,
    AUTH_ACTION_OPEN,
    AUTH_RESULT_ALLOW,
    AUTH_RESULT_DENY,
    LOCAL_RESULT_OPEN_OK,
)
from canbus.listener import CardEvent, normalize_uid
from config import settings
from database.database import SessionLocal
from models.device import Device, LOCKER_STATE_OCCUPIED, LOCKER_STATE_VACANT

logger = logging.getLogger(__name__)

# Decision outcomes
OUTCOME_OCCUPIED = "occupied"
OUTCOME_RELEASED = "released"
OUTCOME_DENIED_CARD_IN_USE = "denied_card_in_use"
OUTCOME_DENIED_NOT_OWNER = "denied_not_owner"
OUTCOME_DENIED_INVALID_CARD = "denied_invalid_card"
OUTCOME_IGNORED = "ignored"
OUTCOME_ERROR = "error"

MAX_NAME_LENGTH = 64
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# Slot index of the firmware's ephemeral (guest/owner) credential
EPHEMERAL_SLOT = 1


class GymCommandError(RuntimeError):
    """Raised when a CAN command required by an admin operation could not be transmitted."""


@dataclass(slots=True)
class GymDecision:
    """Result of a gym-mode card decision."""
    outcome: str
    allowed: bool
    device_id: int
    card_uid: Optional[str] = None
    detail: str = ""
    other_device_id: Optional[int] = None


@dataclass(slots=True)
class LockerSnapshot:
    """Immutable view of the persisted gym state of one locker."""
    device_id: int
    state: str
    card_uid: Optional[str]
    occupied_at: Optional[datetime]


def utc_now() -> datetime:
    """Current UTC time as a naive datetime (the format used across the database)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def validate_locker_name(name: Any) -> str:
    """Returns the cleaned locker name or raises ValueError if it is not acceptable."""
    if not isinstance(name, str):
        raise ValueError("Name must be a string")
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("Name must not be empty")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise ValueError(f"Name must not exceed {MAX_NAME_LENGTH} characters")
    if _CONTROL_CHARS.search(cleaned):
        raise ValueError("Name must not contain control characters")
    return cleaned


def _default_broadcaster(event: str, data: dict) -> None:
    """Pushes an SSE event to the dashboard (imported lazily to avoid an import cycle)."""
    from api.stream import broadcast_sse_event
    broadcast_sse_event(event, data)


class GymLockerService:
    """Decision engine and state keeper for gym-mode lockers."""

    DEFAULT_LOCK_ID = 1

    def __init__(
        self,
        listener,
        sender: Callable[[Any], Any],
        broadcaster: Optional[Callable[[str, dict], None]] = None,
        session_factory: Callable[[], Any] = SessionLocal,
        command_gap_s: float = 0.05,
    ):
        self.listener = listener
        self.sender = sender
        self.broadcaster = broadcaster or _default_broadcaster
        self.session_factory = session_factory
        self.command_gap_s = command_gap_s
        # Serializes decisions so "1 card = 1 locker" holds even for near-simultaneous swipes
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Listener hooks
    # ------------------------------------------------------------------

    def handle_card_event(self, event: CardEvent) -> Optional[GymDecision]:
        """Entry point wired to CANListener.card_event_handler."""
        if not settings.is_gym_mode():
            return None
        if event.kind == "auth_request":
            return self.handle_auth_request(
                event.device_id, event.card_uid, requested_action=event.code, lock_id=event.lock_id
            )
        if event.kind == "local_action":
            self.handle_local_action(event.device_id, event.card_uid, event.code)
        return None

    def handle_wl_auto_delete(self, device_id: int) -> None:
        """Firmware auto-deleted the ephemeral credential, i.e. it released the locker on its own."""
        if not settings.is_gym_mode():
            return
        with self._lock:
            snap = self._read_snapshot(device_id)
            if snap is None or snap.state != LOCKER_STATE_OCCUPIED:
                return
            self._set_state(device_id, LOCKER_STATE_VACANT, None, None)
            self._log(device_id, "LOCKER_RELEASED", snap.card_uid,
                      "Locker released by firmware (guest credential auto-deleted)")
            self._broadcast_device(device_id)

    # ------------------------------------------------------------------
    # Card decisions
    # ------------------------------------------------------------------

    def handle_auth_request(
        self,
        device_id: int,
        card_uid: Any,
        requested_action: Optional[int] = None,
        lock_id: Optional[int] = None,
    ) -> GymDecision:
        """
        Decides a card presentation and answers the lock with AUTH_RESP.

        Args:
            device_id: Node that reported the card.
            card_uid: UID of the presented card (hex string, bytes or int).
            requested_action: Action the lock proposed (1=CLOSE, 2=OPEN); informational only,
                the database state is authoritative.
            lock_id: Logical lock index to echo in the response.
        """
        if not settings.is_gym_mode():
            return GymDecision(OUTCOME_IGNORED, False, device_id, detail="Not in gym mode")

        lock_id = self.DEFAULT_LOCK_ID if lock_id is None else lock_id
        card = normalize_uid(card_uid)
        if not card:
            self._send(auth_resp(device_id, AUTH_RESULT_DENY, AUTH_ACTION_NONE, lock_id))
            self._log(device_id, "ACCESS_DENIED", None, "Gym: card UID missing or invalid")
            return GymDecision(OUTCOME_DENIED_INVALID_CARD, False, device_id, detail="Invalid card UID")

        with self._lock:
            self.listener.get_device(device_id)  # guarantees in-memory state and DB row exist
            snap = self._read_snapshot(device_id)
            if snap is None:
                self._send(auth_resp(device_id, AUTH_RESULT_DENY, AUTH_ACTION_NONE, lock_id))
                return GymDecision(OUTCOME_ERROR, False, device_id, card, "Locker record unavailable")

            if snap.state == LOCKER_STATE_OCCUPIED:
                if normalize_uid(snap.card_uid) == card:
                    return self._release_with_card(snap, card, lock_id, requested_action)
                return self._deny(
                    device_id, card, lock_id, OUTCOME_DENIED_NOT_OWNER,
                    f"Gym: locker occupied by another card ({snap.card_uid})",
                )

            holder = self._find_holder(card, exclude_device_id=device_id)
            if holder is not None:
                return self._deny(
                    device_id, card, lock_id, OUTCOME_DENIED_CARD_IN_USE,
                    f"Gym: card already occupies Locker {holder}", other_device_id=holder,
                )
            return self._occupy(snap, card, lock_id, requested_action)

    def handle_local_action(self, device_id: int, card_uid: Any, result: int) -> None:
        """
        Mirrors a decision the lock took on its own (card found in its local whitelist).

        Only a successful local OPEN frees the locker; the CLOSE_OK echo of a claim we just
        granted must not change anything.
        """
        if not settings.is_gym_mode() or result != LOCAL_RESULT_OPEN_OK:
            return
        card = normalize_uid(card_uid)
        with self._lock:
            snap = self._read_snapshot(device_id)
            if snap is None or snap.state != LOCKER_STATE_OCCUPIED:
                return
            self._set_state(device_id, LOCKER_STATE_VACANT, None, None)
            owner = normalize_uid(snap.card_uid) == card
            self._log(
                device_id, "LOCKER_RELEASED", card,
                "Locker released locally by its owner card" if owner
                else f"Locker opened locally by a whitelisted card (owner was {snap.card_uid})",
            )
            self._broadcast_device(device_id)

    # ------------------------------------------------------------------
    # Admin operations
    # ------------------------------------------------------------------

    def force_release(self, device_id: int) -> Dict[str, Any]:
        """
        Admin override: clears the firmware guest credential, holds the bolt open and
        marks the locker vacant. Idempotent for already vacant lockers.

        Raises:
            LookupError: if the locker is unknown.
            GymCommandError: if a CAN command could not be sent (database stays unchanged).
        """
        with self._lock:
            snap = self._read_snapshot(device_id)
            if snap is None:
                raise LookupError(f"Locker {device_id} not found")

            lock_id = self.DEFAULT_LOCK_ID
            # Wipe the owner credential first, otherwise the former owner's card would still
            # be answered locally by the lock; then keep the bolt retracted for the next member.
            if not self._send(delete_wl_slot(device_id, EPHEMERAL_SLOT, lock_id=lock_id)):
                raise GymCommandError("Failed to clear the locker credential on the CAN bus")
            if self.command_gap_s > 0:
                time.sleep(self.command_gap_s)
            if not self._send(open_hold(device_id, lock_id=lock_id)):
                raise GymCommandError("Failed to unlock the locker on the CAN bus")

            self._set_state(device_id, LOCKER_STATE_VACANT, None, None)
            self._log(
                device_id, "LOCKER_FORCE_RELEASE", snap.card_uid,
                f"Locker force-released by admin (previous owner: {snap.card_uid or 'none'})",
            )
            self._broadcast_device(device_id)
            return {
                "device_id": device_id,
                "locker_state": LOCKER_STATE_VACANT,
                "released_card_uid": snap.card_uid,
            }

    def force_release_all(self) -> Dict[str, Any]:
        """
        Bulk admin override: iterates over all currently occupied lockers, sends the
        release/unlock commands to each, clears assigned_card_uid and occupied_at,
        sets locker_state to vacant, persists to SQLite, and broadcasts SSE updates.
        """
        with self._lock:
            with self.session_factory() as db:
                occupied_rows = (
                    db.query(Device)
                    .filter(Device.locker_state == LOCKER_STATE_OCCUPIED)
                    .order_by(Device.device_id)
                    .all()
                )
                target_ids = [
                    r.device_id
                    for r in occupied_rows
                    if r.device_id not in (settings.STAGING_NODE_ID, settings.UNPROVISIONED_NODE_ID)
                ]

            released_ids = []
            released_lockers = []
            failed_ids = []
            for dev_id in target_ids:
                try:
                    res = self.force_release(dev_id)
                    released_ids.append(dev_id)
                    released_lockers.append(res)
                except (GymCommandError, LookupError) as exc:
                    logger.error(f"Bulk release failed for locker {dev_id}: {exc}")
                    failed_ids.append(dev_id)

            return {
                "status": "released_all",
                "released_count": len(released_ids),
                "released_device_ids": released_ids,
                "released_lockers": released_lockers,
                "failed_device_ids": failed_ids,
            }

    def rename(self, device_id: int, name: Any) -> str:
        """
        Renames a locker.

        Raises:
            ValueError: if the name is invalid.
            LookupError: if the locker is unknown.
        """
        cleaned = validate_locker_name(name)
        with self._lock:
            with self.session_factory() as db:
                row = db.get(Device, device_id)
                if row is None:
                    raise LookupError(f"Locker {device_id} not found")
                previous = row.custom_name or row.name
                # Legacy consumers (event log, older clients) still read the plain name column
                row.custom_name = cleaned
                row.name = cleaned
                db.commit()
            with self.listener.lock:
                dev = self.listener.devices.get(device_id)
                if dev is not None:
                    dev.custom_name = cleaned
                    dev.name = cleaned
            self._log(device_id, "LOCKER_RENAMED", None, f"Renamed '{previous}' to '{cleaned}'")
            self._broadcast_device(device_id)
        return cleaned

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _occupy(self, snap: LockerSnapshot, card: str, lock_id: int, requested_action: Optional[int]) -> GymDecision:
        device_id = snap.device_id
        # Persist first so a crash can never leave a closed locker recorded as vacant
        self._set_state(device_id, LOCKER_STATE_OCCUPIED, card, utc_now())
        if not self._send(auth_resp(device_id, AUTH_RESULT_ALLOW, AUTH_ACTION_CLOSE, lock_id)):
            self._set_state(device_id, LOCKER_STATE_VACANT, None, None)
            self._log(device_id, "ERROR", card, "Gym: failed to send AUTH_RESP (close); occupancy rolled back")
            return GymDecision(OUTCOME_ERROR, False, device_id, card, "CAN transmit failed")
        self._log(device_id, "LOCKER_OCCUPIED", card,
                  f"Locker occupied by card {card}{self._requested(requested_action)}")
        self._broadcast_device(device_id)
        return GymDecision(OUTCOME_OCCUPIED, True, device_id, card, "Locker occupied")

    def _release_with_card(
        self, snap: LockerSnapshot, card: str, lock_id: int, requested_action: Optional[int]
    ) -> GymDecision:
        device_id = snap.device_id
        self._set_state(device_id, LOCKER_STATE_VACANT, None, None)
        if not self._send(auth_resp(device_id, AUTH_RESULT_ALLOW, AUTH_ACTION_OPEN, lock_id)):
            self._set_state(device_id, LOCKER_STATE_OCCUPIED, snap.card_uid, snap.occupied_at)
            self._log(device_id, "ERROR", card, "Gym: failed to send AUTH_RESP (open); occupancy restored")
            return GymDecision(OUTCOME_ERROR, False, device_id, card, "CAN transmit failed")
        self._log(device_id, "LOCKER_RELEASED", card,
                  f"Locker released by owner card {card}{self._requested(requested_action)}")
        self._broadcast_device(device_id)
        return GymDecision(OUTCOME_RELEASED, True, device_id, card, "Locker released")

    def _deny(
        self,
        device_id: int,
        card: str,
        lock_id: int,
        outcome: str,
        detail: str,
        other_device_id: Optional[int] = None,
    ) -> GymDecision:
        # The firmware plays the failure buzzer and blinks red on DENY and keeps the bolt as is
        self._send(auth_resp(device_id, AUTH_RESULT_DENY, AUTH_ACTION_NONE, lock_id))
        self._log(device_id, "ACCESS_DENIED", card, detail)
        return GymDecision(outcome, False, device_id, card, detail, other_device_id)

    @staticmethod
    def _requested(requested_action: Optional[int]) -> str:
        names = {1: "CLOSE", 2: "OPEN"}
        return f" (lock requested {names[requested_action]})" if requested_action in names else ""

    def _send(self, frame) -> bool:
        """Transmits a frame; returns False instead of raising so callers can roll back."""
        try:
            return self.sender(frame) is not False
        except Exception as exc:
            logger.error(f"Gym CAN transmit failed: {exc}")
            return False

    def _read_snapshot(self, device_id: int) -> Optional[LockerSnapshot]:
        with self.session_factory() as db:
            row = db.get(Device, device_id)
            if row is None:
                return None
            return LockerSnapshot(
                device_id=row.device_id,
                state=row.locker_state or LOCKER_STATE_VACANT,
                card_uid=row.assigned_card_uid,
                occupied_at=row.occupied_at,
            )

    def _find_holder(self, card: str, exclude_device_id: int) -> Optional[int]:
        """Returns the ID of another locker currently occupied by the card, if any."""
        with self.session_factory() as db:
            rows = (
                db.query(Device)
                .filter(Device.locker_state == LOCKER_STATE_OCCUPIED, Device.device_id != exclude_device_id)
                .all()
            )
            for row in rows:
                if normalize_uid(row.assigned_card_uid) == card:
                    return row.device_id
        return None

    def _set_state(
        self, device_id: int, state: str, card: Optional[str], occupied_at: Optional[datetime]
    ) -> None:
        """Persists a locker state transition and mirrors it into the in-memory device."""
        with self.session_factory() as db:
            row = db.get(Device, device_id)
            if row is None:
                raise LookupError(f"Locker {device_id} not found")
            row.locker_state = state
            row.assigned_card_uid = card
            row.occupied_at = occupied_at
            db.commit()
        with self.listener.lock:
            dev = self.listener.devices.get(device_id)
            if dev is not None:
                dev.locker_state = state
                dev.assigned_card_uid = card
                dev.occupied_at = occupied_at

    def _log(self, device_id: int, event_type: str, card: Optional[str], details: str) -> None:
        try:
            self.listener.add_log(device_id, event_type, card, details, direction="INTERNAL")
        except Exception as exc:
            logger.error(f"Gym log failed: {exc}")

    def _broadcast_device(self, device_id: int) -> None:
        """Pushes the refreshed device card to every connected dashboard."""
        try:
            with self.listener.lock:
                dev = self.listener.devices.get(device_id)
                payload = dev.to_stream_payload() if dev is not None else None
            if payload is not None:
                self.broadcaster("device_update", payload)
        except Exception as exc:
            logger.error(f"Gym SSE broadcast failed: {exc}")

