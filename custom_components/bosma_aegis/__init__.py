"""The Bosma Aegis integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BosmaApi, BosmaAuthError, BosmaConnectionError
from .const import (
    CONF_ACCOUNT, CONF_IMEI, CONF_PASSWORD, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN, PLATFORMS,
)
from .coordinator import BosmaCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Bosma Aegis from a config entry."""
    session = async_get_clientsession(hass)
    api = BosmaApi(session, entry.data[CONF_ACCOUNT], entry.data[CONF_PASSWORD],
                   entry.data.get(CONF_IMEI, "ha-bosma-aegis"))
    # Refresh device metadata for pre-1.0.3 entries (remote gateway metadata is needed for native control).
    try:
        devices = await api.async_discover_locks()
    except BosmaAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from None
    except BosmaConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from None
    if not devices:
        devices = entry.data["devices"]
    scan = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    coordinator = BosmaCoordinator(hass, entry, api, devices, scan)

    from .alerts import BosmaAlerts

    coordinator.alerts = BosmaAlerts(hass, entry, coordinator)
    await coordinator.async_config_entry_first_refresh()
    coordinator.start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: BosmaCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unload_ok


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
