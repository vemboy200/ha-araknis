"""The Araknis Networks integration."""

from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    DEVICE_TYPE_ROUTER,
    DEVICE_TYPE_SWITCH,
    DEVICE_TYPE_WEB_SMART,
    AraknisRouterClient,
    AraknisSwitchClient,
    AraknisWebSmartClient,
)
from .const import CONF_DEVICE_TYPE
from .coordinator import (
    AnyAraknisCoordinator,
    AraknisConfigEntry,
    AraknisRouterCoordinator,
    AraknisSwitchCoordinator,
    AraknisWebSmartCoordinator,
)
from .entity import device_info

PLATFORMS: dict[str, list[Platform]] = {
    DEVICE_TYPE_SWITCH: [Platform.BUTTON, Platform.SENSOR, Platform.SWITCH],
    DEVICE_TYPE_ROUTER: [
        Platform.BINARY_SENSOR,
        Platform.BUTTON,
        Platform.SENSOR,
        Platform.SWITCH,
    ],
    DEVICE_TYPE_WEB_SMART: [
        Platform.BINARY_SENSOR,
        Platform.BUTTON,
        Platform.SENSOR,
        Platform.SWITCH,
    ],
}


async def async_setup_entry(hass: HomeAssistant, entry: AraknisConfigEntry) -> bool:
    """Set up an Araknis switch or router from a config entry."""
    session = async_get_clientsession(hass)
    login = (
        entry.data[CONF_HOST],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )
    coordinator: AnyAraknisCoordinator
    if entry.data[CONF_DEVICE_TYPE] == DEVICE_TYPE_ROUTER:
        coordinator = AraknisRouterCoordinator(
            hass, entry, AraknisRouterClient(session, *login)
        )
    elif entry.data[CONF_DEVICE_TYPE] == DEVICE_TYPE_WEB_SMART:
        coordinator = AraknisWebSmartCoordinator(
            hass, entry, AraknisWebSmartClient(session, *login)
        )
    else:
        coordinator = AraknisSwitchCoordinator(
            hass, entry, AraknisSwitchClient(session, *login)
        )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    # Register the device first: each port is a child device of it.
    coordinator.device_id = (
        dr.async_get(hass)
        .async_get_or_create(config_entry_id=entry.entry_id, **device_info(coordinator))
        .id
    )
    await hass.config_entries.async_forward_entry_setups(
        entry, PLATFORMS[entry.data[CONF_DEVICE_TYPE]]
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AraknisConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS[entry.data[CONF_DEVICE_TYPE]]
    )
    if unloaded:
        await entry.runtime_data.client.logout()
    return unloaded


async def async_migrate_entry(hass: HomeAssistant, entry: AraknisConfigEntry) -> bool:
    """Migrate an old config entry."""
    if entry.version > 1:
        # Downgraded from a future version.
        return False
    if entry.minor_version < 2:
        # Entries from before router support are all switches.
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_DEVICE_TYPE: DEVICE_TYPE_SWITCH},
            minor_version=2,
        )
    return True
