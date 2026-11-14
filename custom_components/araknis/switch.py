"""PoE on/off switches for Araknis switch ports, and the web-smart OvrC setting."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import AraknisError
from .const import DOMAIN
from .coordinator import (
    AraknisConfigEntry,
    AraknisSwitchCoordinator,
    AraknisWebSmartCoordinator,
)
from .entity import AraknisEntity, AraknisPortEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AraknisConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up PoE switches, or the OvrC switch of a web-smart switch."""
    coordinator = entry.runtime_data
    if isinstance(coordinator, AraknisWebSmartCoordinator):
        async_add_entities([AraknisCloudSwitch(coordinator)])
        return
    assert isinstance(coordinator, AraknisSwitchCoordinator)
    async_add_entities(
        AraknisPoeSwitch(coordinator, port) for port in coordinator.data.ports
    )


class AraknisPoeSwitch(AraknisPortEntity, SwitchEntity):
    """Turns PoE on or off for a port."""

    _attr_translation_key = "poe"

    def __init__(self, coordinator: AraknisSwitchCoordinator, port: int) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, port, "poe")

    @property
    def is_on(self) -> bool:
        """Return if PoE is enabled on the port."""
        return self.port.poe_enabled

    async def _set(self, enabled: bool) -> None:
        try:
            await self.coordinator.client.set_poe(self.port_number, enabled)
        except AraknisError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn PoE on."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn PoE off."""
        await self._set(False)


class AraknisCloudSwitch(AraknisEntity[AraknisWebSmartCoordinator], SwitchEntity):
    """Turns the OvrC cloud service on or off."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "cloud_service"

    def __init__(self, coordinator: AraknisWebSmartCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, "cloud_service")

    @property
    def is_on(self) -> bool:
        """Return if the OvrC setting is on."""
        return self.coordinator.data.cloud_enabled

    async def _set(self, enabled: bool) -> None:
        try:
            await self.coordinator.client.set_cloud(enabled)
        except AraknisError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn OvrC on."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn OvrC off."""
        await self._set(False)
