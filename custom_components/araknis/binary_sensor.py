"""Binary sensors for Araknis routers, web-smart switches and WAN ports."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import (
    AraknisConfigEntry,
    AraknisRouterCoordinator,
    AraknisWebSmartCoordinator,
)
from .entity import AraknisEntity, AraknisWanEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AraknisConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    coordinator = entry.runtime_data
    assert isinstance(
        coordinator, AraknisRouterCoordinator | AraknisWebSmartCoordinator
    )
    entities: list[BinarySensorEntity] = [AraknisCloudSensor(coordinator)]
    if isinstance(coordinator, AraknisRouterCoordinator):
        entities.extend(
            AraknisWanConnectedSensor(coordinator, interface)
            for interface in coordinator.data.wans
        )
    async_add_entities(entities)


class AraknisCloudSensor(
    AraknisEntity[AraknisRouterCoordinator | AraknisWebSmartCoordinator],
    BinarySensorEntity,
):
    """Whether the device is connected to the OvrC cloud service."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "cloud_connection"

    def __init__(
        self, coordinator: AraknisRouterCoordinator | AraknisWebSmartCoordinator
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, "cloud_connection")

    @property
    def is_on(self) -> bool:
        """Return if the device is connected to OvrC."""
        return self.coordinator.data.cloud_connected


class AraknisWanConnectedSensor(AraknisWanEntity, BinarySensorEntity):
    """Whether a WAN port has a link and an IP address."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    # The WAN port's main entity, named after its device. The translation key
    # only gives it an icon.
    _attr_name = None
    _attr_translation_key = "wan_connection"

    def __init__(self, coordinator: AraknisRouterCoordinator, interface: str) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, interface, "connected")

    @property
    def is_on(self) -> bool:
        """Return if the WAN port is connected."""
        return self.wan.connected
