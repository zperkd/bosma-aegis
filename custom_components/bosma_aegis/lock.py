"""Optional lock control for Bosma Aegis via IFTTT.

The integration itself cannot send Bosma's encrypted, gateway-relayed BLE lock/unlock command (see
docs/ARCHITECTURE.md §9). But Bosma exposes lock/unlock as an IFTTT action, so when the user configures their
IFTTT Webhooks key + event names in the options flow, we expose real `lock.*` entities that:

  * show state straight from the read-only integration (instant websocket pushes), and
  * control by firing the user's IFTTT webhook, then WAIT for the integration to confirm the deadbolt actually
    moved before clearing the transient locking/unlocking state (never trust "webhook sent" == "lock moved").

With no key configured, no lock entities are created and the integration stays fully read-only.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import safe_error
from .const import CONF_CONTROL_EVENTS, CONF_IFTTT_KEY, CONTROL_CONFIRM_TIMEOUT, DOMAIN, ifttt_url
from .coordinator import BosmaCoordinator
from .entity import BosmaEntity

_LOGGER = logging.getLogger(__name__)
_WEBHOOK_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    """Create a controllable lock per device that has an IFTTT key + event names configured."""
    key = (entry.options.get(CONF_IFTTT_KEY) or "").strip()
    events = entry.options.get(CONF_CONTROL_EVENTS) or {}
    if not key:
        return  # control not configured -> stay read-only (sensors only)
    coordinator: BosmaCoordinator = hass.data[DOMAIN][entry.entry_id]
    session = async_get_clientsession(hass)
    entities: list[LockEntity] = []
    for device in coordinator.devices:
        ev = events.get(device["devicePid"]) or {}
        if ev.get("lock") and ev.get("unlock"):
            entities.append(BosmaLock(coordinator, device, session, key, ev["lock"], ev["unlock"]))
    async_add_entities(entities)


class BosmaLock(BosmaEntity, LockEntity):
    """A lock whose STATE is the read-only integration and whose CONTROL is the user's IFTTT webhook."""

    _attr_name = None  # the entity's name is just the device name (it's the primary control of the device)

    def __init__(self, coordinator: BosmaCoordinator, device: dict[str, str],
                 session: aiohttp.ClientSession, key: str, lock_event: str, unlock_event: str) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{self._pid}_lock"
        self._session = session
        self._key = key
        self._lock_event = lock_event
        self._unlock_event = unlock_event
        self._pending: str | None = None  # "locked"/"unlocked" while awaiting confirmation
        self._op_seq = 0  # bumped per command so a superseded background task bows out

    @property
    def is_jammed(self) -> bool | None:
        return self._obs.get("status") == "jammed"

    @property
    def is_locked(self) -> bool | None:
        status = self._obs.get("status")
        if status in ("jammed", None):
            return None
        return status == "locked"

    @property
    def is_locking(self) -> bool:
        return self._pending == "locked"

    @property
    def is_unlocking(self) -> bool:
        return self._pending == "unlocked"

    async def async_lock(self, **kwargs) -> None:
        self._operate("locked", self._lock_event)

    async def async_unlock(self, **kwargs) -> None:
        self._operate("unlocked", self._unlock_event)

    def _operate(self, target: str, event: str) -> None:
        """Show the transient state and kick off the fire+confirm in the BACKGROUND.

        This returns immediately so the lock service call (and the Lovelace card / app) don't block on the IFTTT
        round-trip + confirmation wait -- blocking there makes the frontend report an HTTP timeout even though the
        webhook fired and the deadbolt moved. The card shows locking/unlocking until the integration confirms.
        """
        if self._pending == target:
            return  # same direction already in progress
        self._op_seq += 1
        seq = self._op_seq
        self._pending = target
        self.async_write_ha_state()
        self.hass.async_create_background_task(
            self._run(seq, target, event), name=f"bosma_aegis_{self._pid}_{target}")

    async def _run(self, seq: int, target: str, event: str) -> None:
        """Fire the IFTTT webhook, then wait (in the background) for the integration to confirm `target`.

        `seq` identifies this command; if a newer one supersedes it, this task bows out without touching state.
        """
        try:
            if not await self._fire(event):
                if self._op_seq == seq:
                    _LOGGER.warning("%s: IFTTT webhook '%s' failed; lock state unchanged", self._pid, event)
                return
            # Wait for the integration (push or poll) to confirm the deadbolt actually moved.
            deadline = self.hass.loop.time() + CONTROL_CONFIRM_TIMEOUT
            while self.hass.loop.time() < deadline:
                if self._op_seq != seq:
                    return  # a newer command took over; leave its transient state alone
                if self._obs.get("status") == target:
                    return
                await asyncio.sleep(0.5)
            _LOGGER.warning("%s: sent '%s' but lock still reports %r after %ss",
                            self._pid, event, self._obs.get("status"), CONTROL_CONFIRM_TIMEOUT)
        finally:
            if self._op_seq == seq:  # only the latest command clears the transient state
                self._pending = None
                self.async_write_ha_state()

    async def _fire(self, event: str) -> bool:
        """POST the IFTTT webhook. True on a 2xx response."""
        try:
            async with self._session.post(ifttt_url(self._key, event), timeout=_WEBHOOK_TIMEOUT) as resp:
                if resp.status // 100 == 2:
                    return True
                _LOGGER.debug("%s: IFTTT returned HTTP %s for '%s'", self._pid, resp.status, event)
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            # The webhook URL contains the IFTTT key, so log only the error type/status, never its text.
            _LOGGER.debug("%s: IFTTT webhook error for '%s': %s", self._pid, event, safe_error(err))
            return False
