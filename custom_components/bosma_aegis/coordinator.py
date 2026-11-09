"""The Bosma Aegis coordinator: instant websocket pushes + periodic REST reconciliation, with ordered
observations, confident activity attribution, and per-device availability. Read-only."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BosmaApi, BosmaAuthError, BosmaConnectionError
from .const import (
    ACTTYPE, CLOCK_SKEW, EVENT_ACTIVITY, FRESH_AVAILABILITY, LOCK_ACTS, NON_PERSON, RECENT_WINDOW,
    UNLOCK_ACTS, WS_URL,
)

_LOGGER = logging.getLogger(__name__)


def _num(value: Any) -> float | None:
    """Coerce an id/timestamp to a number, or None (defensive against schema drift)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class BosmaCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """One per config entry. `data` maps devicePid -> observation dict used by the entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: BosmaApi,
                 devices: list[dict[str, str]], scan_interval: int) -> None:
        super().__init__(hass, _LOGGER, name="Bosma Aegis",
                         update_interval=timedelta(seconds=scan_interval))
        self.entry = entry
        self.api = api
        self.devices = devices  # [{devicePid, deviceId, name}]
        self._by_pid = {d["devicePid"]: d for d in devices}
        self._revision: dict[str, int] = {}
        self._seen_events: dict[str, set] = {}
        self._last_ev_ts: dict[str, float] = {}  # per-lock last consumed activity timestamp (log has no row id)
        self._ws_task: asyncio.Task | None = None
        self._ws_connected = False
        # seed a blank observation per device so entities exist before the first read
        self.data = {d["devicePid"]: {"status": None, "battery": None, "door": None,
                                      "observed_at": None, "activity": None} for d in devices}

    # ---- lifecycle ----
    def start(self) -> None:
        """Start the background websocket listener (called after the first refresh)."""
        if self._ws_task is None:
            self._ws_task = self.entry.async_create_background_task(
                self.hass, self._ws_loop(), "bosma_aegis_ws")

    async def async_shutdown(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_task
            self._ws_task = None
        await super().async_shutdown()

    # ---- periodic REST reconciliation ----
    async def _async_update_data(self) -> dict[str, dict]:
        try:
            for dev in self.devices:
                started = time.time()
                shadow = await self.api.async_get_shadow(dev["deviceId"])
                self._apply(dev["devicePid"], shadow, source="poll", observed=started)
        except BosmaAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except BosmaConnectionError as err:
            raise UpdateFailed(str(err)) from err
        return self.data

    # ---- availability ----
    def available(self, pid: str) -> bool:
        obs = (self.data.get(pid) or {}).get("observed_at")
        return obs is not None and (time.time() - obs) < FRESH_AVAILABILITY

    @property
    def websocket_connected(self) -> bool:
        return self._ws_connected

    # ---- apply one observation (poll or push) ----
    def _apply(self, pid: str, nd: Any, source: str, observed: float) -> None:
        if pid not in self._by_pid or not isinstance(nd, dict):
            return
        raw = nd.get("lockStatus")
        if str(raw) not in ("0", "1"):
            if raw not in (None, ""):
                _LOGGER.debug("%s: ignoring unknown lockStatus=%r", pid, raw)
            return
        if observed > time.time() + CLOCK_SKEW:
            return
        state = self.data.get(pid) or {}
        prev_obs = state.get("observed_at") or 0
        if observed < prev_obs:
            return  # a newer observation already won (ordering by observation time)
        jammed = str(nd.get("jammed", "0")) in ("1", "true", "True")
        status = "jammed" if jammed else ("locked" if str(raw) == "1" else "unlocked")
        changed = state.get("status") is not None and state.get("status") != status
        # Bump the transition revision ONLY on a real change, so a redundant re-report (battery/door update, poll)
        # right after a transition doesn't supersede that transition's pending attribution lookup.
        if changed:
            self._revision[pid] = self._revision.get(pid, 0) + 1
        revision = self._revision.get(pid, 0)
        door = None
        if nd.get("doorStatus") not in (None, ""):
            door = "closed" if str(nd.get("doorStatus")) == "1" else "open"  # doorStatus 1 = closed
        new = dict(state)
        new.update(status=status, observed_at=observed, source=source,
                   battery=_num(nd.get("battery")) if nd.get("battery") not in (None, "") else state.get("battery"),
                   door=door if door is not None else state.get("door"))
        self.data = dict(self.data, **{pid: new})
        self.async_set_updated_data(self.data)
        if changed and source == "push":
            self.hass.async_create_task(self._attribute(pid, status, revision, observed))

    # ---- attribution (who/how) ----
    async def _attribute(self, pid: str, status: str, revision: int, observed: float) -> None:
        # The activity log row for a transition is usually present within a second or two, BUT its timestamp is on
        # Bosma's server clock, which can run tens of seconds off ours -- so we must NOT compare it to `observed`.
        # Instead pick the newest direction-matching row that is (a) UNCONSUMED (newer than the last one we used for
        # this lock) and (b) RECENT in absolute terms (within RECENT_WINDOW of now, which tolerates the skew both
        # ways and rules out stale history). That row is the action the user just took.
        want = LOCK_ACTS if status == "locked" else UNLOCK_ACTS
        last_ts = self._last_ev_ts.get(pid)
        chosen = None
        for attempt in range(4):
            if self._revision.get(pid) != revision:
                return  # superseded by a newer transition
            try:
                rows = await self.api.async_get_events(pid)
            except (BosmaAuthError, BosmaConnectionError) as err:
                _LOGGER.debug("%s: activity lookup failed (%s); state unaffected", pid, err)
                return
            now = time.time()
            best = None
            for row in rows:
                if not isinstance(row, dict):
                    continue
                at = row.get("actType")
                ts = _num(row.get("timestamp"))
                if ts is not None and ts > 1e12:
                    ts /= 1000.0
                if at is None or ts is None or at not in want or abs(ts - now) > RECENT_WINDOW:
                    continue
                if best is None or ts > best[0]:
                    best = (ts, at, row)
            if best is not None and (last_ts is None or best[0] > last_ts):
                chosen = best
                break
            if attempt < 3:
                await asyncio.sleep(1.5)  # brief wait in case the row hasn't landed yet
        if chosen is None or self._revision.get(pid) != revision:
            return
        sel_ts, at, row = chosen
        seen = self._seen_events.setdefault(pid, set())
        self._last_ev_ts[pid] = sel_ts
        correlated = True  # an unconsumed, recent, direction-matching row = the action just taken
        person = at not in NON_PERSON
        act_name = ACTTYPE.get(at, "unknown")
        state = dict(self.data.get(pid) or {})
        state["activity"] = {"act_name": act_name, "act_type": at, "who": row.get("nickName"),
                             "timestamp": sel_ts, "correlated": correlated, "person": person}
        self.data = dict(self.data, **{pid: state})
        self.async_set_updated_data(self.data)
        event_id = "%s:%d:%s" % (pid, int(sel_ts), at)
        if event_id not in seen and 0 <= time.time() - observed <= 60:  # allow for the attribution retries above
            seen.add(event_id)
            if len(seen) > 64:
                seen.clear()
                seen.add(event_id)
            self.hass.bus.async_fire(EVENT_ACTIVITY, {
                "device_pid": pid, "name": self._by_pid[pid]["name"], "locked": status == "locked",
                "status": status, "act_name": act_name, "act_type": at, "who": row.get("nickName"),
                "person": person, "correlated": correlated, "event_id": event_id})

    # ---- websocket push listener ----
    async def _ws_loop(self) -> None:
        session = async_get_clientsession(self.hass)
        backoff = 2
        while True:
            try:
                if self.api.token is None:
                    await self.api.async_login()
                conn = base64.b64encode(json.dumps(
                    {"lang": "en_US", "token": self.api.token, "client": "APP"}).encode()).decode()
                async with session.ws_connect(WS_URL + conn, heartbeat=None, timeout=20) as ws:
                    self._ws_connected = True
                    backoff = 2
                    await ws.send_str(json.dumps({"action": "notification/subscribe",
                                                  "reqData": {"devicePid": ",".join(self._by_pid)}}))
                    ka = asyncio.create_task(self._ws_keepalive(ws))
                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self._ws_message(ws, msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                    finally:
                        ka.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await ka
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep the listener alive across any transient failure
                _LOGGER.debug("websocket error: %s", err)
            self._ws_connected = False
            await asyncio.sleep(min(backoff, 60))
            backoff = min(backoff * 2, 60)
            with contextlib.suppress(BosmaAuthError, BosmaConnectionError):
                await self.api.async_login()  # refresh the token before reconnecting

    async def _ws_keepalive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while not ws.closed:
            await asyncio.sleep(30)
            with contextlib.suppress(Exception):
                await ws.send_str("ping")

    def _ws_message(self, ws: aiohttp.ClientWebSocketResponse, raw: str) -> None:
        if raw == "ping":
            self.hass.async_create_task(ws.send_str("pong"))
            return
        if raw == "pong":
            return
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return
        if obj.get("action") != "notification/data":
            return
        data = (obj.get("respData") or {}).get("data") or {}
        if data.get("notifyType") == "DEVICE_SHADOW":
            self._apply(data.get("devicePid"), data.get("notifyData"), source="push", observed=time.time())
