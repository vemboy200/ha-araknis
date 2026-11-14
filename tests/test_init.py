"""Tests for setting up the Araknis Networks integration and its entities."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.araknis.api import AraknisAuthError, AraknisSwitchClient
from custom_components.araknis.const import CONF_DEVICE_TYPE, DOMAIN

from .conftest import BASE, HOST, MAC, endpoint


def setting_calls(aioclient_mock: AiohttpClientMocker) -> list[str]:
    """Return the bodies POSTed to poe_port_setting."""
    return [
        data
        for method, url, data, _ in aioclient_mock.mock_calls
        if method == "POST" and str(url).endswith("poe_port_setting")
    ]


async def test_setup_and_unload(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Test the entry loads, then unloads and logs out."""
    assert init_integration.state is ConfigEntryState.LOADED
    await hass.config_entries.async_unload(init_integration.entry_id)
    assert init_integration.state is ConfigEntryState.NOT_LOADED
    assert str(aioclient_mock.mock_calls[-1][1]).endswith("?logout")


@pytest.mark.parametrize("login_result", [1])
async def test_setup_rejected_login_starts_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_switch: AiohttpClientMocker,
) -> None:
    """Test a rejected login fails setup and asks for a new login."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_migrate_entry_from_before_routers(
    hass: HomeAssistant, mock_switch: AiohttpClientMocker
) -> None:
    """Test entries made before router support become switch entries."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC,
        data={CONF_HOST: HOST, CONF_USERNAME: "admin", CONF_PASSWORD: "secret"},
        minor_version=1,
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.minor_version == 2
    assert entry.data[CONF_DEVICE_TYPE] == "switch"


async def test_setup_unreachable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Test an unreachable switch makes setup retry."""
    aioclient_mock.get(f"{BASE}/conf/key", exc=TimeoutError)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.usefixtures("init_integration")
async def test_entity_states(hass: HomeAssistant) -> None:
    """Test entities report the switch's data with the real scaling."""
    expected = {
        "switch.araknis_switch_192_0_2_10_port_6_poe": STATE_ON,
        "sensor.araknis_switch_192_0_2_10_port_6_poe_power": "4.6",
        "sensor.araknis_switch_192_0_2_10_port_6_poe_status": "delivering",
        "sensor.araknis_switch_192_0_2_10_port_6_link_speed": "1000",
        "sensor.araknis_switch_192_0_2_10_port_1_poe_power": "0.0",
        "sensor.araknis_switch_192_0_2_10_port_1_poe_status": "detecting",
        "sensor.araknis_switch_192_0_2_10_port_21_link_speed": "unknown",
        # Port 6 has one device, port 2 one plus a locally administered MAC.
        "sensor.araknis_switch_192_0_2_10_port_6_connected_device_mac": (
            "00:11:22:00:00:60"
        ),
        "sensor.araknis_switch_192_0_2_10_port_2_connected_device_mac": (
            "00:11:22:00:00:20"
        ),
        # Port 1 is an uplink with several devices; port 21's link is down.
        "sensor.araknis_switch_192_0_2_10_port_1_connected_device_mac": "unknown",
        "sensor.araknis_switch_192_0_2_10_port_21_connected_device_mac": "unknown",
        "sensor.araknis_switch_192_0_2_10_poe_power": "21.8",
        "sensor.araknis_switch_192_0_2_10_poe_budget": "190",
    }
    for entity_id, state in expected.items():
        assert (entity := hass.states.get(entity_id)), entity_id
        assert entity.state == state, entity_id
    # Voltage and current are disabled by default.
    assert (
        hass.states.get("sensor.araknis_switch_192_0_2_10_port_6_poe_voltage") is None
    )
    # The SFP port has no PoE row, so it gets no entities.
    assert hass.states.get("switch.araknis_switch_192_0_2_10_sfp_1_poe") is None


@pytest.mark.usefixtures("init_integration")
@pytest.mark.parametrize(
    ("service", "body"),
    [(SERVICE_TURN_OFF, "p3=0,-1,-1,-1&"), (SERVICE_TURN_ON, "p3=1,-1,-1,-1&")],
)
async def test_poe_switch(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    service: str,
    body: str,
) -> None:
    """Test turning PoE off and on addresses the port's row, not its number."""
    await hass.services.async_call(
        SWITCH_DOMAIN,
        service,
        {ATTR_ENTITY_ID: "switch.araknis_switch_192_0_2_10_port_6_poe"},
        blocking=True,
    )
    assert setting_calls(aioclient_mock) == [body]


@pytest.mark.usefixtures("init_integration")
async def test_power_cycle(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Test the power cycle button."""
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: "button.araknis_switch_192_0_2_10_port_21_poe_power_cycle"},
        blocking=True,
    )
    assert setting_calls(aioclient_mock) == ["p4=-1,-1,-1,1&"]


@pytest.mark.usefixtures("init_integration")
@pytest.mark.parametrize("poe_off_ports", [(21,)])
async def test_power_cycle_with_poe_off(
    hass: HomeAssistant, mock_switch: AiohttpClientMocker
) -> None:
    """Test power cycling a port with PoE off is refused without a command."""
    assert (
        hass.states.get("switch.araknis_switch_192_0_2_10_port_21_poe").state
        == STATE_OFF
    )

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {
                ATTR_ENTITY_ID: "button.araknis_switch_192_0_2_10_port_21_poe_power_cycle"
            },
            blocking=True,
        )
    assert setting_calls(mock_switch) == []


async def test_session_expired_logs_in_again(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    public_key_pem: str,
    switch_data: dict[str, dict[str, Any]],
) -> None:
    """Test a "not logged in" reply makes the client log in once more and retry."""
    replies = iter([{"result": 2}, switch_data["poe_port"]])

    async def poe_port(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse(method, url, json=next(replies))

    aioclient_mock.get(re.compile(r"/conf/key$"), text=public_key_pem)
    aioclient_mock.post(endpoint("login"), json={"result": 0})
    aioclient_mock.get(endpoint("poe_port"), side_effect=poe_port)
    for name, reply in switch_data.items():
        if name != "poe_port":
            aioclient_mock.get(endpoint(name), json=reply)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    logins = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("?login")]
    assert len(logins) == 2


@pytest.mark.parametrize("login_result", [1])
async def test_failed_login_is_not_retried_right_away(
    hass: HomeAssistant, mock_switch: AiohttpClientMocker
) -> None:
    """Test the client waits after a rejected login instead of trying again."""
    client = AraknisSwitchClient(async_get_clientsession(hass), HOST, "admin", "bad")
    with pytest.raises(AraknisAuthError):
        await client.get_data()
    calls = mock_switch.call_count
    with pytest.raises(AraknisAuthError, match="suppressed"):
        await client.get_data()
    assert mock_switch.call_count == calls


@pytest.mark.usefixtures("init_integration")
async def test_port_child_devices(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test each PoE port is a child device of the switch."""
    device_registry = dr.async_get(hass)
    switch = device_registry.async_get_device_by_identifier(
        (DOMAIN, MAC), mock_config_entry.entry_id
    )
    assert switch is not None
    children = dr.async_child_entries_for_config_entry(
        device_registry, mock_config_entry.entry_id
    )
    assert {child.name for child in children} == {
        "Port 1",
        "Port 2",
        "Port 6",
        "Port 21",
    }
    assert all(child.parent_device_id == switch.id for child in children)

    port6 = device_registry.async_get_child_device_by_identifier(
        (DOMAIN, f"{MAC}_port6"), mock_config_entry.entry_id
    )
    assert port6 is not None
    # Entity IDs combine the switch, port and entity names.
    assert {
        entity.entity_id
        for entity in er.async_entries_for_device(
            er.async_get(hass), port6.id, include_disabled_entities=True
        )
    } == {
        "switch.araknis_switch_192_0_2_10_port_6_poe",
        "button.araknis_switch_192_0_2_10_port_6_poe_power_cycle",
        "sensor.araknis_switch_192_0_2_10_port_6_poe_power",
        "sensor.araknis_switch_192_0_2_10_port_6_poe_voltage",
        "sensor.araknis_switch_192_0_2_10_port_6_connected_device_mac",
        "sensor.araknis_switch_192_0_2_10_port_6_poe_current",
        "sensor.araknis_switch_192_0_2_10_port_6_poe_status",
        "sensor.araknis_switch_192_0_2_10_port_6_link_speed",
    }


@pytest.mark.usefixtures("init_integration")
async def test_switch_details(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test the switch device shows its model, firmware and serial number."""
    switch = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, MAC), mock_config_entry.entry_id
    )
    assert switch is not None
    assert switch.manufacturer == "Araknis Networks"
    assert switch.model == "AN-210-SW-24-POE"
    assert switch.sw_version == "v1.3.10.54"
    assert switch.serial_number == "ST0000000000000X"


@pytest.mark.usefixtures("init_integration")
@pytest.mark.freeze_time("2026-11-14 12:00:00+00:00")
async def test_temperature_and_last_restart(hass: HomeAssistant) -> None:
    """Test the temperature and last restart sensors."""
    assert (
        hass.states.get("sensor.araknis_switch_192_0_2_10_temperature").state == "31.75"
    )
    # Uptime 6779164 s = 78 d 11:06:04 before the frozen time.
    assert (
        hass.states.get("sensor.araknis_switch_192_0_2_10_last_restart").state
        == "2026-08-28T00:53:56+00:00"
    )


@pytest.mark.usefixtures("init_integration")
@pytest.mark.parametrize("has_temperature_sensor", [False])
async def test_no_temperature_sensor(hass: HomeAssistant) -> None:
    """Test switches without a temperature sensor get no temperature entity."""
    assert hass.states.get("sensor.araknis_switch_192_0_2_10_temperature") is None
