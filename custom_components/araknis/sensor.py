"""Sensors for Araknis switches, routers and their ports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfDataRate,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import POE_STATUS, PoePort, RouterData, RouterWan, SwitchData
from .coordinator import (
    AraknisConfigEntry,
    AraknisRouterCoordinator,
    AraknisSwitchCoordinator,
)
from .entity import AraknisEntity, AraknisPortEntity, AraknisWanEntity

PARALLEL_UPDATES = 0

# Seconds the worked-out boot time may drift before the sensor changes.
BOOT_TIME_TOLERANCE = 60


@dataclass(frozen=True, kw_only=True)
class AraknisPortSensorDescription(SensorEntityDescription):
    """Describes a sensor for one PoE port."""

    value_fn: Callable[[PoePort], float | int | str | None]


@dataclass(frozen=True, kw_only=True)
class AraknisSwitchSensorDescription(SensorEntityDescription):
    """Describes a sensor for the whole switch."""

    value_fn: Callable[[SwitchData], float | int | None]
    exists_fn: Callable[[SwitchData], bool] = lambda _: True


@dataclass(frozen=True, kw_only=True)
class AraknisRouterSensorDescription(SensorEntityDescription):
    """Describes a sensor for the whole router."""

    value_fn: Callable[[RouterData], float | int | None]


@dataclass(frozen=True, kw_only=True)
class AraknisWanSensorDescription(SensorEntityDescription):
    """Describes a sensor for one WAN port."""

    value_fn: Callable[[RouterWan], int | str | None]


PORT_SENSORS: tuple[AraknisPortSensorDescription, ...] = (
    AraknisPortSensorDescription(
        key="power",
        translation_key="port_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda port: port.power,
    ),
    AraknisPortSensorDescription(
        key="voltage",
        translation_key="port_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        entity_registry_enabled_default=False,
        value_fn=lambda port: port.voltage,
    ),
    AraknisPortSensorDescription(
        key="current",
        translation_key="port_current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
        entity_registry_enabled_default=False,
        value_fn=lambda port: port.current,
    ),
    AraknisPortSensorDescription(
        key="poe_status",
        translation_key="poe_status",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        options=[*POE_STATUS.values(), "unknown"],
        value_fn=lambda port: port.poe_status,
    ),
    AraknisPortSensorDescription(
        key="link_speed",
        translation_key="link_speed",
        device_class=SensorDeviceClass.DATA_RATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        value_fn=lambda port: port.link_speed,
    ),
    AraknisPortSensorDescription(
        key="connected_mac",
        translation_key="connected_mac",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda port: port.connected_mac,
    ),
)

SWITCH_SENSORS: tuple[AraknisSwitchSensorDescription, ...] = (
    AraknisSwitchSensorDescription(
        key="poe_power_total",
        translation_key="poe_power_total",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda data: data.power_total,
    ),
    AraknisSwitchSensorDescription(
        key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        exists_fn=lambda data: data.temperature is not None,
        value_fn=lambda data: data.temperature,
    ),
    AraknisSwitchSensorDescription(
        key="poe_budget",
        translation_key="poe_budget",
        device_class=SensorDeviceClass.POWER,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda data: data.power_budget,
    ),
)


ROUTER_SENSORS: tuple[AraknisRouterSensorDescription, ...] = (
    AraknisRouterSensorDescription(
        key="cpu_usage",
        translation_key="cpu_usage",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda data: data.cpu_usage,
    ),
    AraknisRouterSensorDescription(
        key="memory_usage",
        translation_key="memory_usage",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda data: data.memory_usage,
    ),
    AraknisRouterSensorDescription(
        key="dhcp_clients",
        translation_key="dhcp_clients",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.dhcp_clients,
    ),
)

WAN_SENSORS: tuple[AraknisWanSensorDescription, ...] = (
    AraknisWanSensorDescription(
        key="link_speed",
        translation_key="link_speed",
        device_class=SensorDeviceClass.DATA_RATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        value_fn=lambda wan: wan.link_speed,
    ),
    AraknisWanSensorDescription(
        key="ip_address",
        translation_key="ip_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda wan: wan.ip_address,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AraknisConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [AraknisBootTimeSensor(coordinator)]
    if isinstance(coordinator, AraknisRouterCoordinator):
        entities.extend(
            AraknisRouterSensor(coordinator, description)
            for description in ROUTER_SENSORS
        )
        entities.extend(
            AraknisWanSensor(coordinator, interface, description)
            for interface in coordinator.data.wans
            for description in WAN_SENSORS
        )
        async_add_entities(entities)
        return
    entities.extend(
        AraknisSwitchSensor(coordinator, description)
        for description in SWITCH_SENSORS
        if description.exists_fn(coordinator.data)
    )
    entities.extend(
        AraknisPortSensor(coordinator, port, description)
        for port in coordinator.data.ports
        for description in PORT_SENSORS
    )
    async_add_entities(entities)


class AraknisSwitchSensor(AraknisEntity[AraknisSwitchCoordinator], SensorEntity):
    """A sensor for the whole switch."""

    entity_description: AraknisSwitchSensorDescription

    def __init__(
        self,
        coordinator: AraknisSwitchCoordinator,
        description: AraknisSwitchSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | None:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)


class AraknisRouterSensor(AraknisEntity[AraknisRouterCoordinator], SensorEntity):
    """A sensor for the whole router."""

    entity_description: AraknisRouterSensorDescription

    def __init__(
        self,
        coordinator: AraknisRouterCoordinator,
        description: AraknisRouterSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | None:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)


class AraknisBootTimeSensor(
    AraknisEntity[AraknisSwitchCoordinator | AraknisRouterCoordinator], SensorEntity
):
    """When the switch or router last started, worked out from its uptime."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "last_restart"

    def __init__(
        self, coordinator: AraknisSwitchCoordinator | AraknisRouterCoordinator
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "last_restart")
        self._boot_time: datetime | None = None

    @property
    def native_value(self) -> datetime:
        """Return the boot time, ignoring the second or two of jitter between polls."""
        boot_time = dt_util.utcnow().replace(microsecond=0) - timedelta(
            seconds=self.coordinator.data.uptime
        )
        if (
            self._boot_time is None
            or abs((boot_time - self._boot_time).total_seconds()) > BOOT_TIME_TOLERANCE
        ):
            self._boot_time = boot_time
        return self._boot_time


class AraknisPortSensor(AraknisPortEntity, SensorEntity):
    """A sensor for one PoE port."""

    entity_description: AraknisPortSensorDescription

    def __init__(
        self,
        coordinator: AraknisSwitchCoordinator,
        port: int,
        description: AraknisPortSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, port, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | str | None:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.port)


class AraknisWanSensor(AraknisWanEntity, SensorEntity):
    """A sensor for one WAN port."""

    entity_description: AraknisWanSensorDescription

    def __init__(
        self,
        coordinator: AraknisRouterCoordinator,
        interface: str,
        description: AraknisWanSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, interface, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int | str | None:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.wan)
