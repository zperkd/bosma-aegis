"""Bosma native cloud lock control with observed-state confirmation and legacy IFTTT."""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import BosmaAuthError, BosmaConnectionError, safe_error
from .const import (CONF_CONTROL_EVENTS, CONF_CONTROL_MODE, CONF_IFTTT_KEY, CONTROL_CONFIRM_TIMEOUT,
    CONTROL_NATIVE, CONTROL_IFTTT, CONTROL_READ_ONLY, DOMAIN, ifttt_url)
from .coordinator import BosmaCoordinator
from .entity import BosmaEntity

_LOGGER = logging.getLogger(__name__)
_WEBHOOK_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    """Create native lock entities, or the explicitly selected legacy IFTTT entities."""
    key = (entry.options.get(CONF_IFTTT_KEY) or "").strip()
    events = entry.options.get(CONF_CONTROL_EVENTS) or {}
    mode = entry.options.get(CONF_CONTROL_MODE, CONTROL_NATIVE)
    if mode == CONTROL_READ_ONLY:
        return
    coordinator: BosmaCoordinator = hass.data[DOMAIN][entry.entry_id]
    session = async_get_clientsession(hass)
    entities: list[LockEntity] = []
    for device in coordinator.devices:
        ev = events.get(device["devicePid"]) or {}
        if mode == CONTROL_NATIVE or (mode == CONTROL_IFTTT and key and ev.get("lock") and ev.get("unlock")):
            entities.append(BosmaLock(coordinator, device, session, key, ev.get("lock", ""), ev.get("unlock", ""), mode))
    async_add_entities(entities)


class BosmaLock(BosmaEntity, LockEntity):
    """Native cloud control with existing state/entity identities preserved."""

    _attr_name = None  # the entity's name is just the device name (it's the primary control of the device)

    def __init__(self, coordinator: BosmaCoordinator, device: dict[str, str],
                 session: aiohttp.ClientSession, key: str, lock_event: str, unlock_event: str,
                 mode: str = CONTROL_NATIVE) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{self._pid}_lock"
        self._device = device
        self._mode = mode
        self._dispatch_lock = asyncio.Lock()
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
        self.coordinator.entry.async_create_background_task(
            self.hass, self._run(seq, target, event), name=f"bosma_aegis_{self._pid}_{target}")

    async def _run(self, seq: int, target: str, event: str) -> None:
        """Serialize dispatch, then confirm a fresh observation of the actual deadbolt."""
        try:
            async with self._dispatch_lock:
                if self._op_seq != seq:
                    return
                started = time.time()
                if self._mode == CONTROL_NATIVE:
                    await self.coordinator.api.async_control(self._device, unlock=target == "unlocked")
                elif not await self._fire(event):
                    _LOGGER.warning("%s: IFTTT control failed", self._pid)
                    return
                deadline = self.hass.loop.time() + CONTROL_CONFIRM_TIMEOUT
                next_refresh = 0.0
                # Finish confirming a dispatched command before releasing the serial lock,
                # even if the user has queued the opposite direction. Otherwise a gateway
                # can receive a relock while the earlier unlock is still in flight.
                while self.hass.loop.time() < deadline:
                    if (self._obs.get("status") == target
                            and (self._obs.get("observed_at") or 0) >= started):
                        return
                    # Reconcile promptly if a push is missed; never equate accepted POST with success.
                    if self.hass.loop.time() >= next_refresh:
                        await self.coordinator.async_request_refresh()
                        next_refresh = self.hass.loop.time() + 3
                    await asyncio.sleep(0.5)
                _LOGGER.warning("%s: control not confirmed after %ss", self._pid, CONTROL_CONFIRM_TIMEOUT)
        except BosmaAuthError:
            self.coordinator.entry.async_start_reauth(self.hass)
            _LOGGER.warning("Bosma control needs re-authentication")
        except (BosmaConnectionError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Bosma control failed: %s", safe_error(err))
        finally:
            if self._op_seq == seq:
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
