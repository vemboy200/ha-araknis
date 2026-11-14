"""Tests for Araknis AN-110 web-smart switches."""

from __future__ import annotations

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.switch import (
    DOMAIN as SWITCH_DOMAIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.araknis.api import (
    AraknisAuthError,
    AraknisWebSmartClient,
    _parse_uptime,
)
from custom_components.araknis.const import DOMAIN

from .conftest import HOST, USER_INPUT, WEB_SMART_ENTRY_DATA, WEB_SMART_MAC


def entity_id_of(hass: HomeAssistant, platform: str, key: str) -> str:
    """Return the entity ID of the web-smart switch entity with this key."""
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{WEB_SMART_MAC}_{key}"
    )
    assert entity_id is not None, key
    return entity_id


def ovrc_posts(aioclient_mock: AiohttpClientMocker) -> list[dict[str, str]]:
    """Return the forms POSTed to the OvrC setting page."""
    return [
        data
        for method, url, data, _ in aioclient_mock.mock_calls
        if method == "POST" and str(url).endswith("webovrc.cgi")
    ]


@pytest.mark.usefixtures("mock_setup_entry", "mock_web_smart")
async def test_user_flow(hass: HomeAssistant) -> None:
    """Test the flow recognizes a web-smart switch and reads its MAC."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "AN-110-SW-16"
    assert result["data"] == WEB_SMART_ENTRY_DATA
    assert result["result"].unique_id == WEB_SMART_MAC


@pytest.mark.usefixtures("mock_setup_entry", "mock_web_smart")
@pytest.mark.parametrize("web_smart_login_ok", [False])
async def test_user_flow_rejected_login(hass: HomeAssistant) -> None:
    """Test a rejected login shows an error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.freeze_time("2026-11-14 12:00:00+00:00")
async def test_entities(hass: HomeAssistant, init_web_smart: MockConfigEntry) -> None:
    """Test the device and its entities."""
    assert init_web_smart.state is ConfigEntryState.LOADED
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, WEB_SMART_MAC), init_web_smart.entry_id
    )
    assert device is not None
    assert device.model == "AN-110-SW-16"
    assert device.sw_version == "1.1.21"
    assert device.serial_number == "ST0000000000000Y"
    # Uptime 85 days 15:51:39 before the frozen time.
    assert (
        hass.states.get(entity_id_of(hass, "sensor", "last_restart")).state
        == "2026-08-20T20:08:21+00:00"
    )
    assert (
        hass.states.get(entity_id_of(hass, "binary_sensor", "cloud_connection")).state
        == STATE_ON
    )
    assert (
        hass.states.get(entity_id_of(hass, "switch", "cloud_service")).state == STATE_ON
    )


@pytest.mark.usefixtures("init_web_smart")
@pytest.mark.parametrize(
    ("service", "state"), [(SERVICE_TURN_OFF, "0"), (SERVICE_TURN_ON, "1")]
)
async def test_cloud_switch(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    service: str,
    state: str,
) -> None:
    """Test the OvrC switch posts what the switch's OvrC Setting page posts."""
    await hass.services.async_call(
        SWITCH_DOMAIN,
        service,
        {ATTR_ENTITY_ID: entity_id_of(hass, "switch", "cloud_service")},
        blocking=True,
    )
    assert ovrc_posts(aioclient_mock) == [{"ovrc_state": state, "cmd": "OvrC Setting"}]


@pytest.mark.usefixtures("mock_web_smart")
@pytest.mark.parametrize("web_smart_login_ok", [False])
async def test_rejected_login_is_not_retried_right_away(hass: HomeAssistant) -> None:
    """Test the client waits after a rejected login."""
    client = AraknisWebSmartClient(async_get_clientsession(hass), HOST, "a", "bad")
    with pytest.raises(AraknisAuthError):
        await client.get_data()
    with pytest.raises(AraknisAuthError, match="suppressed"):
        await client.get_data()


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("85 days 15:51:39", 85 * 86400 + 15 * 3600 + 51 * 60 + 39),
        ("1 day 00:00:05", 86405),
        ("0 days 00:01:00", 60),
        ("12:00:00", 43200),
    ],
)
def test_parse_uptime(text: str, seconds: int) -> None:
    """Test uptime parsing. Only the "N days HH:MM:SS" form has been seen."""
    assert _parse_uptime(text) == seconds


@pytest.mark.usefixtures("init_web_smart")
async def test_restart(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Test the restart button posts what the switch's Reboot button posts."""
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: entity_id_of(hass, "button", "restart")},
        blocking=True,
    )
    assert [
        data
        for method, url, data, _ in aioclient_mock.mock_calls
        if method == "POST" and str(url).endswith("reboot.cgi")
    ] == [{"cmd": "reboot"}]
