"""PoE on/off switches for Araknis switch ports."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import AraknisError
from .const import DOMAIN
from .coordinator import AraknisConfigEntry, AraknisSwitchCoordinator
from .entity import AraknisPortEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AraknisConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up PoE switches."""
    coordinator = entry.runtime_data
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
