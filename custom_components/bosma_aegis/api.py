"""Async client for Bosma cloud discovery, live state and native lock control."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import aiohttp

from .const import APP_HEADERS, FRONTEND_BASE, GLOBAL_BASE
from .crypto import decode_offline_key, encrypt_challenge

_LOGGER = logging.getLogger(__name__)

# Aegis smart-lock model codes begin with this family prefix (e.g. 010601). Used to pick locks out of the device list.
LOCK_MODEL_PREFIX = "0106"
_TIMEOUT = aiohttp.ClientTimeout(total=20)


class BosmaAuthError(Exception):
    """Login failed / credentials rejected."""


class BosmaVerificationRequired(BosmaAuthError):
    """Bosma requires an emailed verification code for this (new) device (login code 514).

    Subclasses BosmaAuthError so any caller that already handles auth failures (e.g. the coordinator -> reauth)
    keeps working; the config flow catches this specifically to show the "enter the code" step.
    """


class BosmaConnectionError(Exception):
    """A transport or unexpected-response error talking to the Bosma cloud."""


def safe_error(err: BaseException) -> str:
    """Describe a request error for logs/exceptions WITHOUT its message text.

    aiohttp puts the full request URL in many exception messages (e.g. WSServerHandshakeError, InvalidURL), and our
    URLs carry secrets: the Bosma token (websocket URL + REST query string) and the IFTTT key (webhook path). So only
    the exception type and, when present, the HTTP status are ever reported.
    """
    status = getattr(err, "status", None)
    name = type(err).__name__
    return "%s (HTTP %s)" % (name, status) if isinstance(status, int) and status else name


def _find_locks(payload: Any) -> list[dict[str, str]]:
    """Walk an arbitrary device-list response and pull out lock devices.

    The device-list response shape isn't contractually documented, so this is deliberately tolerant: it recurses
    through dicts/lists and collects any object that carries both a device PID and a device id. When a modelCode is
    present it keeps only the lock family; objects without a modelCode are kept (the caller can still use them).
    """
    found: dict[str, dict[str, str]] = {}

    def pid_of(obj: dict) -> str | None:
        for k in ("devicePid", "pid", "lockPid", "deviceId"):
            v = obj.get(k)
            if isinstance(v, str) and len(v) >= 8:
                return v
        return None

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            pid = node.get("devicePid") or node.get("pid") or node.get("lockPid")
            did = node.get("deviceId") or node.get("did")
            model = str(node.get("modelCode") or node.get("model") or "")
            if isinstance(pid, str) and isinstance(did, str) and len(pid) >= 8:
                if not model or model.startswith(LOCK_MODEL_PREFIX):
                    name = (node.get("deviceName") or node.get("name") or node.get("nickName")
                            or node.get("deviceNickName") or "Bosma Lock")
                    found[pid] = {"devicePid": pid, "deviceId": did, "name": str(name), "modelCode": model}
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)
    return list(found.values())


class BosmaApi:
    """Async Bosma cloud client. One instance per config entry."""

    def __init__(self, session: aiohttp.ClientSession, account: str, password: str, imei: str) -> None:
        self._session = session
        self._account = account
        self._password = password
        self._imei = imei
        self._token: str | None = None
        self._generation = 0
        self._login_lock = asyncio.Lock()
        self._mac_key: str | None = None
        self._control_locks: dict[str, asyncio.Lock] = {}

    @property
    def token(self) -> str | None:
        return self._token

    @property
    def generation(self) -> int:
        return self._generation

    async def async_login(self, verify_code: str = "", expected_generation: int | None = None) -> str:
        """Log in and store the token. Single-flight: if another coroutine already refreshed past
        `expected_generation`, reuse that token instead of logging in again.

        A new device triggers Bosma's code-514 security check (an emailed verification code); pass that code as
        `verify_code` to complete it. Raises BosmaVerificationRequired on 514 (a code was just emailed)."""
        async with self._login_lock:
            if expected_generation is not None and self._generation != expected_generation:
                return self._token  # someone else already refreshed
            digest = hashlib.md5(self._password.encode()).hexdigest()
            data = {"account": self._account, "password": digest, "lang": "en_US", "appId": "smart",
                    "identity": "1", "imei": self._imei, "verifyCode": verify_code}
            try:
                async with self._session.post(GLOBAL_BASE + "/api/global/user/login", data=data,
                                              headers=dict(APP_HEADERS), timeout=_TIMEOUT) as resp:
                    body = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise BosmaConnectionError(safe_error(err)) from None  # drop the cause: its text has the URL
            code = body.get("code") if isinstance(body, dict) else None
            if code == 514:
                # New device: Bosma has emailed a verification code; the caller must retry with it.
                raise BosmaVerificationRequired()
            if code != 0:
                msg = str((body or {}).get("msg", ""))[:80]
                raise BosmaAuthError("login rejected (code %s) %s" % (code, msg))
            token = (body.get("data") or {}).get("token") or (body.get("data") or {}).get("accessToken")
            if not token:
                raise BosmaAuthError("no token in login response")
            self._token = token
            self._mac_key = (body.get("data") or {}).get("macKey")
            self._generation += 1
            _LOGGER.debug("Bosma login ok (generation %s)", self._generation)
            return token

    async def async_send_verify_code(self) -> None:
        """Ask Bosma to email a new-device verification code to this account (the code-514 flow)."""
        try:
            async with self._session.post(GLOBAL_BASE + "/api/global/user/resendVerifyCode",
                                          data={"account": self._account}, headers=dict(APP_HEADERS),
                                          timeout=_TIMEOUT) as resp:
                body = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise BosmaConnectionError(safe_error(err)) from None  # drop the cause: its text has the URL
        code = body.get("code") if isinstance(body, dict) else None
        # 0 = sent; 500 "You have just sent it" = a code was already sent moments ago (still in the inbox).
        if code not in (0, 500):
            raise BosmaConnectionError("could not send verification code (code %s)" % code)

    async def _get(self, base: str, path: str, params: dict[str, str]) -> dict:
        """Authenticated GET with one generation-aware re-login on an auth failure (HTTP 401 OR JSON code 401/507)."""
        if self._token is None:
            await self.async_login()
        for attempt in range(2):
            generation = self._generation
            query = dict(params, token=self._token or "")
            headers = dict(APP_HEADERS, token=self._token or "")
            headers["Domain-Name"] = "user_domain"  # required by the family/device-list + log endpoints
            try:
                async with self._session.get(base + path, params=query, headers=headers, timeout=_TIMEOUT) as resp:
                    if resp.status == 401:
                        if attempt == 0:
                            await self.async_login(expected_generation=generation)
                            continue
                        raise BosmaAuthError("HTTP 401 after re-login")
                    resp.raise_for_status()
                    body = await resp.json(content_type=None)
            except aiohttp.ClientResponseError as err:
                raise BosmaConnectionError("HTTP %s" % err.status) from None
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise BosmaConnectionError(safe_error(err)) from None  # drop the cause: its text has the URL
            if isinstance(body, dict) and (body.get("code") in (401, 507)
                                           or "expired" in str(body.get("msg", "")).lower()):
                if attempt == 0:
                    await self.async_login(expected_generation=generation)
                    continue
                raise BosmaAuthError("token still expired after re-login")
            return body if isinstance(body, dict) else {}

    async def _post(self, path: str, params: dict[str, str], *, retry_auth: bool = True) -> dict:
        """Authenticated form POST. Never retry a physical command after transport failure."""
        if self._token is None:
            await self.async_login()
        for attempt in range(2 if retry_auth else 1):
            generation = self._generation
            headers = dict(APP_HEADERS, token=self._token or "")
            headers["Domain-Name"] = "user_domain"
            try:
                async with self._session.post(
                    FRONTEND_BASE + path, data=dict(params, token=self._token or ""),
                    headers=headers, timeout=_TIMEOUT,
                ) as resp:
                    if resp.status == 401:
                        if retry_auth and attempt == 0:
                            await self.async_login(expected_generation=generation)
                            continue
                        raise BosmaAuthError("Control authentication expired")
                    resp.raise_for_status()
                    body = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                raise BosmaConnectionError(safe_error(err)) from None
            if not isinstance(body, dict):
                raise BosmaConnectionError("Invalid control response")
            code = body.get("code")
            if code in (401, 507):
                if retry_auth and attempt == 0:
                    await self.async_login(expected_generation=generation)
                    continue
                raise BosmaAuthError("Control authentication expired")
            if code != 0:
                # Never include server messages: they may echo a token or encrypted key.
                numeric_code = code if type(code) is int else "unknown"
                raise BosmaConnectionError("Bosma control rejected (code %s)" % numeric_code)
            return body
        raise BosmaAuthError("Control authentication expired")

    async def async_control(self, device: dict[str, str], *, unlock: bool) -> None:
        """Submit an explicit lock/unlock command; caller must verify observed state.

        Provision the key using this account's authorization, then obtain a fresh challenge.
        All account and lock key material stays in memory. A rejected/uncertain command is
        never silently retried or routed to IFTTT.
        """
        pid = device["devicePid"]
        async with self._control_locks.setdefault(pid, asyncio.Lock()):
            key_body = await self._post("/api/lock/getOfflineKey", {
                "devicePid": pid, "deviceId": device["deviceId"],
            })
            data = key_body.get("data")
            if not isinstance(data, dict) or not self._mac_key:
                raise BosmaConnectionError("Bosma did not provide control credentials")
            try:
                key = decode_offline_key(data.get("offlineKey") or data.get("key"), self._mac_key)
            except (ValueError, TypeError):
                raise BosmaConnectionError("Could not decode Bosma control credentials") from None
            generation = self._generation
            challenge_body = await self._post("/api/lock/remoteUnlockBefore", {"devicePid": pid}, retry_auth=False)
            challenge = challenge_body.get("data")
            if not isinstance(challenge, dict):
                raise BosmaConnectionError("Missing Bosma control challenge")
            try:
                response = encrypt_challenge(challenge.get("rnd"), key)
            except (ValueError, TypeError):
                raise BosmaConnectionError("Invalid Bosma control challenge") from None
            if generation != self._generation:
                raise BosmaConnectionError("Bosma session changed during control; try again")
            await self._post("/api/lock/remoteUnlock", {
                "devicePid": pid,
                "dbPid": device.get("remotePid") or device.get("hubMac") or device.get("lockPid") or "",
                "rndEn": response, "isUnlock": "1" if unlock else "0",
            }, retry_auth=False)

    async def async_discover_locks(self) -> list[dict[str, str]]:
        """Return the account's lock devices: [{deviceId, devicePid, name, modelCode}].

        Two steps: list the account's families, then list each family's devices and keep the lock models
        (modelCode 0106xx -- excludes keypads 0104xx and gateways 0107xx).
        """
        families = (await self._get(FRONTEND_BASE, "/api/family/getList", {})).get("data") or []
        locks: dict[str, dict[str, str]] = {}
        for fam in families:
            if not isinstance(fam, dict) or not fam.get("familyId"):
                continue
            body = await self._get(FRONTEND_BASE, "/api/family/getFamilyDeviceList",
                                   {"familyId": str(fam["familyId"])})
            devices = body.get("data") or []
            for dev in devices if isinstance(devices, list) else []:
                if not isinstance(dev, dict):
                    continue
                pid = dev.get("pid") or dev.get("devicePid")
                did = dev.get("deviceId")
                model = str(dev.get("modelCode") or "")
                if isinstance(pid, str) and isinstance(did, str) and model.startswith(LOCK_MODEL_PREFIX):
                    name = dev.get("deviceName") or dev.get("name") or dev.get("nickName") or "Bosma Lock"
                    locks[pid] = {"devicePid": pid, "deviceId": did, "name": str(name).strip(), "modelCode": model,
                                  "remotePid": str(dev.get("hubMac") or dev.get("lockPid") or "")}
            if not locks:  # defensive: tolerate an unexpected shape by walking the response
                for found in _find_locks(devices):
                    locks.setdefault(found["devicePid"], found)
        return list(locks.values())

    async def async_get_shadow(self, device_id: str) -> dict:
        """Lock state shadow: {lockStatus, doorStatus, battery} (lockStatus 1=locked, 0=unlocked)."""
        body = await self._get(FRONTEND_BASE, "/api/deviceShadow/getDeviceInfo",
                               {"deviceId": device_id, "fields": "lockStatus,doorStatus,battery"})
        return body.get("data") or {}

    async def async_get_events(self, device_pid: str, page_size: int = 5) -> list[dict]:
        """Newest-first activity rows: [{actType, nickName, timestamp(sec), id, ...}]."""
        body = await self._get(FRONTEND_BASE, "/api/log/lock/list",
                               {"devicePids": device_pid, "pageSize": str(page_size), "pageCurrent": "1"})
        try:
            rows = body["data"]["page"]["data"]
        except (KeyError, TypeError):
            return []
        return rows if isinstance(rows, list) else []
