"""Config flow for Bosma Aegis with native control, including Bosma's new-device email verification (code 514)."""

from __future__ import annotations

import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import slugify

from .api import BosmaApi, BosmaAuthError, BosmaConnectionError, BosmaVerificationRequired
from .const import (
    CONF_ACCOUNT, CONF_CONTROL_EVENTS, CONF_CONTROL_MODE, CONF_IFTTT_KEY, CONF_IMEI, CONF_PASSWORD, CONF_SCAN_INTERVAL,
    CONTROL_NATIVE, CONTROL_IFTTT, CONTROL_READ_ONLY,
    DEFAULT_SCAN_INTERVAL, DOMAIN,
    CONF_ALERT_BREAK_IN, CONF_ALERT_DOOR_AJAR, CONF_ALERT_AJAR_DELAY, CONF_ALERT_PERSISTENT,
    CONF_ALERT_NOTIFY_SERVICE, DEFAULT_AJAR_DELAY,
)

USER_SCHEMA = vol.Schema({vol.Required(CONF_ACCOUNT): str, vol.Required(CONF_PASSWORD): str})
CODE_SCHEMA = vol.Schema({vol.Required("verification_code"): str})


class BosmaConfigFlow(ConfigFlow, domain=DOMAIN):
    """UI setup with email verification for a new device."""

    VERSION = 1

    def __init__(self) -> None:
        self._api: BosmaApi | None = None
        self._account = ""
        self._password = ""
        self._imei = ""
        self._reauth_entry: ConfigEntry | None = None

    async def _start_verify(self) -> ConfigFlowResult:
        """Bosma returned 514 (new device): ask it to email a code, then show the code-entry step."""
        assert self._api is not None
        try:
            await self._api.async_send_verify_code()
        except BosmaConnectionError:
            pass  # show the step anyway; the user can retry / enter a code already in their inbox
        return await self.async_step_verify()

    async def _finish(self) -> ConfigFlowResult:
        """The api is logged in: discover locks and create/update the entry."""
        assert self._api is not None
        devices = await self._api.async_discover_locks()
        if not devices:
            return self.async_abort(reason="no_devices")
        data = {CONF_ACCOUNT: self._account, CONF_PASSWORD: self._password,
                CONF_IMEI: self._imei, "devices": devices}
        if self._reauth_entry is not None:
            self.hass.config_entries.async_update_entry(
                self._reauth_entry, data={**self._reauth_entry.data, **data})
            await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            return self.async_abort(reason="reauth_successful")
        return self.async_create_entry(title=self._account, data=data)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._account = user_input[CONF_ACCOUNT].strip()
            self._password = user_input[CONF_PASSWORD]
            self._imei = "ha-bosma-" + uuid.uuid4().hex[:12]
            await self.async_set_unique_id(self._account.lower())
            self._abort_if_unique_id_configured()
            self._api = BosmaApi(async_get_clientsession(self.hass), self._account, self._password, self._imei)
            try:
                await self._api.async_login()
            except BosmaVerificationRequired:
                return await self._start_verify()  # Bosma will email a code
            except BosmaAuthError:
                errors["base"] = "invalid_auth"
            except BosmaConnectionError:
                errors["base"] = "cannot_connect"
            else:
                return await self._finish()
        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors=errors)

    async def async_step_verify(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Enter the verification code Bosma emailed for this new device."""
        assert self._api is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._api.async_login(verify_code=user_input["verification_code"].strip())
            except BosmaVerificationRequired:
                errors["base"] = "code_resent"   # a fresh code was emailed; enter the new one
            except BosmaAuthError:
                errors["base"] = "invalid_code"
            except BosmaConnectionError:
                errors["base"] = "cannot_connect"
            else:
                return await self._finish()
        return self.async_show_form(step_id="verify", data_schema=CODE_SCHEMA, errors=errors,
                                    description_placeholders={"account": self._account})

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._reauth_entry is not None
        errors: dict[str, str] = {}
        self._account = self._reauth_entry.data[CONF_ACCOUNT]
        self._imei = self._reauth_entry.data.get(CONF_IMEI, "ha-bosma")
        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            self._api = BosmaApi(async_get_clientsession(self.hass), self._account, self._password, self._imei)
            try:
                await self._api.async_login()
            except BosmaVerificationRequired:
                return await self._start_verify()
            except BosmaAuthError:
                errors["base"] = "invalid_auth"
            except BosmaConnectionError:
                errors["base"] = "cannot_connect"
            else:
                return await self._finish()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return BosmaOptionsFlow(entry)


class BosmaOptionsFlow(OptionsFlow):
    """Options: polling interval, native/read-only control and legacy IFTTT."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    def _save(self, updates: dict[str, Any]) -> ConfigFlowResult:
        """Merge into existing options (each sub-step edits only its own keys) and finish."""
        return self.async_create_entry(title="", data={**self._entry.options, **updates})

    def _slug_map(self) -> dict[str, dict[str, str]]:
        """Unique, readable slug -> {pid, name} for each lock (slug used as the option field prefix)."""
        out: dict[str, dict[str, str]] = {}
        for dev in self._entry.data.get("devices", []):
            base = slugify(dev.get("name") or "lock") or "lock"
            slug, n = base, 2
            while slug in out:
                slug, n = f"{base}_{n}", n + 1
            out[slug] = {"pid": dev["devicePid"], "name": dev.get("name") or "Lock"}
        return out

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["settings", "control", "alerts"])

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save({CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL]})
        current = self._entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(step_id="settings", data_schema=vol.Schema({
            vol.Optional(CONF_SCAN_INTERVAL, default=current): vol.All(int, vol.Range(min=30, max=900))}))

    async def async_step_control(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose native, read-only, or the explicit legacy webhook path."""
        if user_input is not None:
            if user_input[CONF_CONTROL_MODE] == CONTROL_IFTTT:
                return await self.async_step_ifttt()
            return self._save({CONF_CONTROL_MODE: user_input[CONF_CONTROL_MODE]})
        return self.async_show_form(step_id="control", data_schema=vol.Schema({
            vol.Required(CONF_CONTROL_MODE, default=self._entry.options.get(CONF_CONTROL_MODE, CONTROL_NATIVE)):
                vol.In({CONTROL_NATIVE: "Bosma cloud (native)", CONTROL_READ_ONLY: "Read only",
                        CONTROL_IFTTT: "IFTTT (legacy)"}),
        }))

    async def async_step_ifttt(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Optional lock/unlock control via IFTTT webhooks (see docs/CONTROL_VIA_IFTTT.md)."""
        slugs = self._slug_map()
        existing = self._entry.options.get(CONF_CONTROL_EVENTS) or {}

        if user_input is not None:
            key = (user_input.get(CONF_IFTTT_KEY) or "").strip()
            events: dict[str, dict[str, str]] = {}
            for slug, info in slugs.items():
                lock_ev = (user_input.get(f"{slug}_lock_event") or "").strip()
                unlock_ev = (user_input.get(f"{slug}_unlock_event") or "").strip()
                if lock_ev and unlock_ev:
                    events[info["pid"]] = {"lock": lock_ev, "unlock": unlock_ev}
            return self._save({CONF_CONTROL_MODE: CONTROL_IFTTT,
                               CONF_IFTTT_KEY: key, CONF_CONTROL_EVENTS: events})

        schema: dict[Any, Any] = {
            vol.Optional(CONF_IFTTT_KEY,
                         default=self._entry.options.get(CONF_IFTTT_KEY, "")): str,
        }
        for slug, info in slugs.items():
            cur = existing.get(info["pid"], {})
            schema[vol.Optional(f"{slug}_lock_event",
                                default=cur.get("lock", f"{slug}_lock"))] = str
            schema[vol.Optional(f"{slug}_unlock_event",
                                default=cur.get("unlock", f"{slug}_unlock"))] = str
        return self.async_show_form(step_id="ifttt", data_schema=vol.Schema(schema))


    async def async_step_alerts(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Opt-in HA and/or phone alerts; the device's own alarm settings are untouched."""
        errors = {}
        choices = {"": "None (Home Assistant only)"}
        for service in self.hass.services.async_services().get("notify", {}):
            if service not in ("send_message", "persistent_notification"):
                choices["notify." + service] = "notify." + service
        existing = self._entry.options.get(CONF_ALERT_NOTIFY_SERVICE, "")
        if existing and existing not in choices:
            choices[existing] = existing + " (unavailable)"
        values = {**self._entry.options, **(user_input or {})}
        if user_input is not None:
            enabled = user_input[CONF_ALERT_BREAK_IN] or user_input[CONF_ALERT_DOOR_AJAR]
            selected = user_input.get(CONF_ALERT_NOTIFY_SERVICE, "")
            if enabled and not user_input[CONF_ALERT_PERSISTENT] and not selected:
                errors["base"] = "no_alert_destination"
            elif selected and not self.hass.services.has_service("notify", selected.partition(".")[2]):
                errors["base"] = "notify_unavailable"
            else:
                return self._save(user_input)
        return self.async_show_form(step_id="alerts", errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_ALERT_BREAK_IN, default=values.get(CONF_ALERT_BREAK_IN, False)): bool,
            vol.Required(CONF_ALERT_DOOR_AJAR, default=values.get(CONF_ALERT_DOOR_AJAR, False)): bool,
            vol.Required(CONF_ALERT_AJAR_DELAY, default=values.get(CONF_ALERT_AJAR_DELAY, DEFAULT_AJAR_DELAY)):
                vol.All(int, vol.Range(min=10, max=3600)),
            vol.Required(CONF_ALERT_PERSISTENT, default=values.get(CONF_ALERT_PERSISTENT, True)): bool,
            vol.Optional(CONF_ALERT_NOTIFY_SERVICE, default=values.get(CONF_ALERT_NOTIFY_SERVICE, "")): vol.In(choices),
        }))
