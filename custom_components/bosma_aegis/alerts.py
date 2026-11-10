"""Opt-in Bosma alarm notifications and freshly confirmed door-open timers."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import OrderedDict
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.exceptions import HomeAssistantError

from .api import BosmaAuthError, BosmaConnectionError, safe_error
from .const import (
    ALERT_COOLDOWN, CONF_ALERT_AJAR_DELAY, CONF_ALERT_BREAK_IN, CONF_ALERT_DOOR_AJAR,
    CONF_ALERT_NOTIFY_SERVICE, CONF_ALERT_PERSISTENT, DEFAULT_AJAR_DELAY, DOMAIN, EVENT_ALERT,
    RECENT_WINDOW,
)

_LOGGER = logging.getLogger(__name__)
# Verified in the Android app's AegisUnlockLogAdapter. Other abnormal codes are NOT break-ins.
BREAK_IN_ACT = 64  # impact / potential door-breaking attempt
DOOR_AJAR_ACT = 68


def _timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(number):
        return None
    return number / 1000 if number > 1e12 else number


class BosmaAlerts:
    """Entry-owned alert state; no historical alarm replay and no stale-state alerts."""

    def __init__(self, hass, entry, coordinator) -> None:
        self.hass, self.entry, self.coordinator = hass, entry, coordinator
        options = entry.options
        self.break_in = options.get(CONF_ALERT_BREAK_IN, False)
        self.door_ajar = options.get(CONF_ALERT_DOOR_AJAR, False)
        self.delay = options.get(CONF_ALERT_AJAR_DELAY, DEFAULT_AJAR_DELAY)
        self.persistent = options.get(CONF_ALERT_PERSISTENT, True)
        self.notify_service = options.get(CONF_ALERT_NOTIFY_SERVICE, "")
        self._closed = False
        self._doors: dict[str, str] = {}
        self._door_observed: dict[str, float] = {}
        self._timers: dict[str, Any] = {}
        self._ajar_sent: set[str] = set()
        self._last_sent: dict[tuple[str, str], float] = {}
        self._baseline: dict[str, float] = {}
        self._seen: OrderedDict[tuple, None] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self._log_tasks: dict[str, asyncio.Task] = {}

    def _task(self, coroutine, name):
        task = self.entry.async_create_background_task(self.hass, coroutine, name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _notification_id(self, pid: str, kind: str) -> str:
        return f"{DOMAIN}_{self.entry.entry_id}_{pid}_{kind}"

    def observe(self, pid: str, raw_door, observed: float) -> None:
        """Observe only valid, ordered door contact reports; repeated open reports don't reset the timer."""
        if self._closed or not isinstance(pid, str) or pid not in self.coordinator._by_pid or str(raw_door) not in ("0", "1"):
            return
        if observed < self._door_observed.get(pid, 0) or observed > time.time() + 5:
            return
        self._door_observed[pid] = observed
        door = "closed" if str(raw_door) == "1" else "open"
        self._doors[pid] = door
        if door == "closed":
            cancel = self._timers.pop(pid, None)
            if cancel:
                cancel()
            self._ajar_sent.discard(pid)
            # A new opening is a new episode, even if the previous one was recent.
            self._last_sent.pop((pid, "door_ajar"), None)
            persistent_notification.async_dismiss(self.hass, self._notification_id(pid, "door_ajar"))
        elif self.door_ajar and pid not in self._timers and pid not in self._ajar_sent:
            self._schedule_timer(pid, self.delay)

    def _schedule_timer(self, pid: str, delay: float) -> None:
        previous = self._timers.pop(pid, None)
        if previous:
            previous()
        @callback
        def elapsed(_now):
            self._timers.pop(pid, None)
            if not self._closed:
                self._task(self._async_confirm_ajar(pid), f"bosma_ajar_{pid}")
        self._timers[pid] = async_call_later(self.hass, delay, elapsed)

    async def _async_confirm_ajar(self, pid: str) -> None:
        if self._closed or self._doors.get(pid) != "open" or pid in self._ajar_sent:
            return
        started = time.time()
        try:
            shadow = await self.coordinator.api.async_get_shadow(self.coordinator._by_pid[pid]["deviceId"])
        except (BosmaAuthError, BosmaConnectionError) as err:
            _LOGGER.debug("Door-ajar confirmation unavailable: %s", safe_error(err))
            if not self._closed and self._doors.get(pid) == "open":
                self._schedule_timer(pid, 30)
            return
        if self._closed or self._door_observed.get(pid, 0) > started:
            return  # a newer contact push won while the request was in flight
        self.observe(pid, shadow.get("doorStatus"), started)
        if str(shadow.get("doorStatus")) == "0" and self._doors.get(pid) == "open":
            # observe() may have started the next timer; one alert per continuous opening is enough.
            cancel = self._timers.pop(pid, None)
            if cancel:
                cancel()
            await self._async_emit(pid, "door_ajar", "The door has been left open for at least %s seconds." % self.delay,
                                   source="door_timer")

    def push(self, pid: str) -> None:
        """Any owned-device notification can prompt an alarm-log scan, even with no lock movement."""
        if self._closed or not isinstance(pid, str) or not (self.break_in or self.door_ajar) or pid not in self.coordinator._by_pid:
            return
        pending = self._log_tasks.get(pid)
        if pending is None or pending.done():
            self._log_tasks[pid] = self._task(self._async_scan(pid), f"bosma_alarms_{pid}")

    async def async_reconcile(self) -> None:
        if not self._closed and (self.break_in or self.door_ajar):
            for pid in self.coordinator._by_pid:
                await self._async_scan(pid)

    async def _async_scan(self, pid: str) -> None:
        async with self._locks.setdefault(pid, asyncio.Lock()):
            if self._closed:
                return
            try:
                rows = await self.coordinator.api.async_get_events(pid, page_size=20)
            except (BosmaAuthError, BosmaConnectionError) as err:
                _LOGGER.debug("Bosma alarm lookup unavailable: %s", safe_error(err))
                return
            self._process_events(pid, rows)

    def _process_events(self, pid: str, rows: list[dict]) -> None:
        valid = []
        for row in rows:
            if not isinstance(row, dict) or row.get("devicePid", pid) != pid:
                continue
            ts = _timestamp(row.get("timestamp"))
            try:
                act = int(row.get("actType"))
            except (ValueError, TypeError):
                continue
            if ts is not None:
                valid.append((ts, act))
        initial = pid not in self._baseline
        if initial:
            self._baseline[pid] = max((ts for ts, _ in valid), default=time.time() - RECENT_WINDOW)
        for ts, act in sorted(valid):
            key = (pid, ts, act)
            if key in self._seen:
                continue
            self._seen[key] = None
            while len(self._seen) > 512:
                self._seen.popitem(last=False)
            if initial or ts < self._baseline[pid] or abs(ts - time.time()) > RECENT_WINDOW:
                continue
            if act == BREAK_IN_ACT and self.break_in:
                self._task(self._async_emit(pid, "break_in", "Bosma reported an impact / potential door-breaking attempt.",
                                           source="bosma_alarm", act_type=act), f"bosma_break_in_{pid}")
            elif act == DOOR_AJAR_ACT and self.door_ajar and self._doors.get(pid) != "closed":
                self._task(self._async_emit(pid, "door_ajar", "Bosma reported that the door was left ajar.",
                                           source="bosma_alarm", act_type=act), f"bosma_ajar_alarm_{pid}")

    async def _async_emit(self, pid: str, kind: str, detail: str, *, source: str, act_type=None) -> None:
        if self._closed or (kind == "door_ajar" and (pid in self._ajar_sent or self._doors.get(pid) == "closed")):
            return
        now = time.monotonic()
        previous = self._last_sent.get((pid, kind))
        if previous is not None and now - previous < ALERT_COOLDOWN:
            return
        self._last_sent[(pid, kind)] = now
        if kind == "door_ajar":
            self._ajar_sent.add(pid)
        name = self.coordinator._by_pid[pid]["name"]
        title = "%s: %s" % (name, "Potential break-in" if kind == "break_in" else "Door left ajar")
        message = "%s: %s" % (name, detail)
        self.hass.bus.async_fire(EVENT_ALERT, {"device_pid": pid, "name": name, "alert_type": kind,
                                              "source": source, "act_type": act_type})
        if self.persistent:
            persistent_notification.async_create(self.hass, message, title=title,
                                                  notification_id=self._notification_id(pid, kind))
        if self.notify_service:
            domain, _, service = self.notify_service.partition(".")
            if domain != "notify" or not service or not self.hass.services.has_service(domain, service):
                _LOGGER.warning("Bosma alert phone notification service is unavailable")
                return
            try:
                async with asyncio.timeout(20):
                    await self.hass.services.async_call(domain, service, {"title": title, "message": message}, blocking=True)
            except (HomeAssistantError, asyncio.TimeoutError) as err:
                _LOGGER.warning("Bosma alert notification failed: %s", safe_error(err))

    async def async_shutdown(self) -> None:
        self._closed = True
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
