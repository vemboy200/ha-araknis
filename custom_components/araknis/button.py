"""PoE power cycle buttons for Araknis switch ports."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
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
    """Set up power cycle buttons."""
    coordinator = entry.runtime_data
    assert isinstance(coordinator, AraknisSwitchCoordinator)
    async_add_entities(
        AraknisPowerCycleButton(coordinator, port) for port in coordinator.data.ports
    )


class AraknisPowerCycleButton(AraknisPortEntity, ButtonEntity):
    """Briefly cuts PoE power to a port, restarting the device on it."""

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_translation_key = "power_cycle"

    def __init__(self, coordinator: AraknisSwitchCoordinator, port: int) -> None:
        """Initialize the button."""
        super().__init__(coordinator, port, "power_cycle")

    async def async_press(self) -> None:
        """Power cycle the port."""
        if not self.port.poe_enabled:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="poe_off",
                translation_placeholders={"port": self.port.name},
            )
        try:
            await self.coordinator.client.power_cycle(self.port_number)
        except AraknisError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await self.coordinator.async_request_refresh()
