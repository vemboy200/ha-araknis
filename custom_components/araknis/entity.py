"""Base entities for the Araknis Networks integration."""

from __future__ import annotations

from homeassistant.const import CONF_HOST
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    ChildDeviceInfo,
    DeviceInfo,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import PoePort, RouterWan
from .const import DOMAIN, MANUFACTURER
from .coordinator import AraknisRouterCoordinator, AraknisSwitchCoordinator


class AraknisEntity[CoordinatorT: AraknisSwitchCoordinator | AraknisRouterCoordinator](
    CoordinatorEntity[CoordinatorT]
):
    """An entity belonging to one Araknis switch or router."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: CoordinatorT, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        assert entry.unique_id is not None
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_device_info = device_info(coordinator)


def device_info(
    coordinator: AraknisSwitchCoordinator | AraknisRouterCoordinator,
) -> DeviceInfo:
    """Return the device info for the switch or router itself."""
    entry = coordinator.config_entry
    assert entry.unique_id is not None
    info = coordinator.info
    return DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id)},
        connections={(CONNECTION_NETWORK_MAC, entry.unique_id)},
        manufacturer=MANUFACTURER,
        model=info.model,
        sw_version=info.firmware,
        serial_number=info.serial_number,
        name=entry.title,
        configuration_url=f"http://{entry.data[CONF_HOST]}",
    )


class AraknisPortEntity(AraknisEntity[AraknisSwitchCoordinator]):
    """An entity for one PoE port, on a child device of the switch."""

    def __init__(
        self, coordinator: AraknisSwitchCoordinator, port: int, key: str
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, f"port{port}_{key}")
        self.port_number = port
        entry = coordinator.config_entry
        assert entry.unique_id is not None
        self._attr_device_info = ChildDeviceInfo(
            identifiers={(DOMAIN, f"{entry.unique_id}_port{port}")},
            name=coordinator.data.ports[port].name,
            parent_device_id=coordinator.device_id,
        )

    @property
    def available(self) -> bool:
        """Return if the port is still reported by the switch."""
        return super().available and self.port_number in self.coordinator.data.ports

    @property
    def port(self) -> PoePort:
        """Return the port's current data."""
        return self.coordinator.data.ports[self.port_number]


class AraknisWanEntity(AraknisEntity[AraknisRouterCoordinator]):
    """An entity for one WAN port, on a child device of the router."""

    def __init__(
        self, coordinator: AraknisRouterCoordinator, interface: str, key: str
    ) -> None:
        """Initialize the entity."""
        wan_key = interface.lower()
        super().__init__(coordinator, f"{wan_key}_{key}")
        self.interface = interface
        entry = coordinator.config_entry
        assert entry.unique_id is not None
        self._attr_device_info = ChildDeviceInfo(
            identifiers={(DOMAIN, f"{entry.unique_id}_{wan_key}")},
            name=coordinator.data.wans[interface].name,
            parent_device_id=coordinator.device_id,
        )

    @property
    def available(self) -> bool:
        """Return if the WAN port is still reported by the router."""
        return super().available and self.interface in self.coordinator.data.wans

    @property
    def wan(self) -> RouterWan:
        """Return the WAN port's current data."""
        return self.coordinator.data.wans[self.interface]
