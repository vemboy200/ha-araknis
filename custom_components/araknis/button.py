"""Restart buttons for Araknis switches, routers and switch ports."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import AraknisError
from .const import DOMAIN
from .coordinator import (
    AnyAraknisCoordinator,
    AraknisConfigEntry,
    AraknisSwitchCoordinator,
)
from .entity import AraknisEntity, AraknisPortEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AraknisConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up restart and power cycle buttons."""
    coordinator = entry.runtime_data
    buttons: list[ButtonEntity] = [AraknisRestartButton(coordinator)]
    if isinstance(coordinator, AraknisSwitchCoordinator):
        buttons.extend(
            AraknisPowerCycleButton(coordinator, port)
            for port in coordinator.data.ports
        )
    async_add_entities(buttons)


class AraknisRestartButton(AraknisEntity[AnyAraknisCoordinator], ButtonEntity):
    """Restarts the switch or router."""

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: AnyAraknisCoordinator) -> None:
        """Initialize the button."""
        super().__init__(coordinator, "restart")

    async def async_press(self) -> None:
        """Restart the device."""
        try:
            await self.coordinator.client.restart()
        except AraknisError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err


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
