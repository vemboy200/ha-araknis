"""Polling coordinators for the Araknis Networks integration."""

from __future__ import annotations

from collections.abc import Awaitable
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    AraknisAuthError,
    AraknisError,
    AraknisRouterClient,
    AraknisSwitchClient,
    RouterData,
    RouterInfo,
    SwitchData,
    SwitchInfo,
)
from .const import DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

type AraknisConfigEntry = ConfigEntry[
    AraknisSwitchCoordinator | AraknisRouterCoordinator
]


async def _call[T](request: Awaitable[T]) -> T:
    """Await a client call, turning its errors into Home Assistant ones."""
    try:
        return await request
    except AraknisAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except AraknisError as err:
        raise UpdateFailed(
            translation_domain=DOMAIN,
            translation_key="update_failed",
            translation_placeholders={"error": str(err)},
        ) from err


class AraknisCoordinator[DataT: SwitchData | RouterData, InfoT](
    DataUpdateCoordinator[DataT]
):
    """Polls one Araknis device."""

    config_entry: AraknisConfigEntry

    def __init__(self, hass: HomeAssistant, entry: AraknisConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.device_id = ""
        """Device registry ID of the switch or router, set during setup."""
        self.info: InfoT
        """Model, firmware and so on, read once at setup."""


class AraknisSwitchCoordinator(AraknisCoordinator[SwitchData, SwitchInfo]):
    """Polls one switch."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: AraknisConfigEntry,
        client: AraknisSwitchClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(hass, entry)
        self.client = client

    async def _async_setup(self) -> None:
        self.info = await _call(self.client.get_info())

    async def _async_update_data(self) -> SwitchData:
        return await _call(self.client.get_data())


class AraknisRouterCoordinator(AraknisCoordinator[RouterData, RouterInfo]):
    """Polls one router."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: AraknisConfigEntry,
        client: AraknisRouterClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(hass, entry)
        self.client = client

    async def _async_setup(self) -> None:
        self.info = await _call(self.client.get_info())

    async def _async_update_data(self) -> RouterData:
        return await _call(self.client.get_data())
