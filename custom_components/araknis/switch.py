"""Switches: PoE per switch port, router port forwarding rules and OvrC."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import AraknisError, PortForward
from .const import DOMAIN
from .coordinator import (
    AraknisConfigEntry,
    AraknisRouterCoordinator,
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
    """Set up PoE switches, port forwarding rules, or the OvrC switch."""
    coordinator = entry.runtime_data
    if isinstance(coordinator, AraknisWebSmartCoordinator):
        async_add_entities([AraknisCloudSwitch(coordinator)])
        return
    if isinstance(coordinator, AraknisRouterCoordinator):
        known: set[str] = set()

        @callback
        def add_new_rules() -> None:
            """Add switches for rules created on the router since the last poll."""
            new = coordinator.data.port_forwards.keys() - known
            known.update(new)
            async_add_entities(
                AraknisPortForwardSwitch(coordinator, key) for key in new
            )

        add_new_rules()
        entry.async_on_unload(coordinator.async_add_listener(add_new_rules))
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


class AraknisPortForwardSwitch(AraknisEntity[AraknisRouterCoordinator], SwitchEntity):
    """Turns one of the router's port forwarding rules on or off."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "port_forward"

    def __init__(self, coordinator: AraknisRouterCoordinator, key: str) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, f"port_forward_{key}")
        self.key = key
        rule = coordinator.data.port_forwards[key]
        # Named after the rule's description, or its port without one.
        self._attr_translation_placeholders = {
            "name": rule.description or f"{rule.protocol} {rule.external_port}"
        }

    @property
    def available(self) -> bool:
        """Return if the rule still exists on the router."""
        return super().available and self.key in self.coordinator.data.port_forwards

    @property
    def rule(self) -> PortForward:
        """Return the rule's current data."""
        return self.coordinator.data.port_forwards[self.key]

    @property
    def is_on(self) -> bool:
        """Return if the rule is enabled."""
        return self.rule.enabled

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Return where the rule forwards to."""
        rule = self.rule
        return {
            "protocol": rule.protocol,
            "external_port": rule.external_port,
            "internal_ip": rule.internal_ip,
            "internal_port": rule.internal_port,
        }

    async def _set(self, enabled: bool) -> None:
        try:
            rules = await self.coordinator.client.set_port_forward(self.key, enabled)
        except AraknisError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        # The client already read the rules back, so no full poll is needed.
        self.coordinator.async_set_updated_data(
            replace(self.coordinator.data, port_forwards=rules)
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the rule."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the rule."""
        await self._set(False)
