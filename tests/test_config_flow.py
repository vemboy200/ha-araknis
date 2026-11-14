"""Tests for the Araknis Networks config flow."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import SOURCE_SSDP, SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.ssdp import (
    ATTR_UPNP_DEVICE_TYPE,
    ATTR_UPNP_SERIAL,
    ATTR_UPNP_UDN,
    SsdpServiceInfo,
)
import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.araknis.const import DOMAIN

from .conftest import (
    BASE,
    HOST,
    MAC,
    ROUTER_ENTRY_DATA,
    ROUTER_MAC,
    SWITCH_ENTRY_DATA,
    USER_INPUT,
    endpoint,
)

pytestmark = pytest.mark.usefixtures("mock_setup_entry")


async def test_user_flow(hass: HomeAssistant, mock_switch: AiohttpClientMocker) -> None:
    """Test setting up a switch."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Rack switch"
    assert result["data"] == SWITCH_ENTRY_DATA
    assert result["result"].unique_id == MAC


async def test_user_flow_router(
    hass: HomeAssistant, mock_router: AiohttpClientMocker
) -> None:
    """Test setting up a router: the flow tells it apart from a switch."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "AN-310-RT-4L2W"
    assert result["data"] == ROUTER_ENTRY_DATA
    assert result["result"].unique_id == ROUTER_MAC


@pytest.mark.parametrize("router_login_reply", [{"login_status": 0, "remain": 4}])
async def test_user_flow_router_rejected_login(
    hass: HomeAssistant, mock_router: AiohttpClientMocker
) -> None:
    """Test a router rejecting the login shows an error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_not_supported(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Test a device that is neither a supported switch nor router.

    Such as Araknis's web-smart switches, which answer 401 everywhere.
    """
    aioclient_mock.get(f"{BASE}/conf/key", status=401)
    aioclient_mock.get(f"{BASE}/login.html", status=401)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "not_supported"}


@pytest.mark.parametrize(
    ("login_result", "error"), [(1, "invalid_auth"), (9, "invalid_auth")]
)
async def test_user_flow_rejected_login(
    hass: HomeAssistant, mock_switch: AiohttpClientMocker, error: str
) -> None:
    """Test a rejected login shows an error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_user_flow_cannot_connect_then_recovers(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    public_key_pem: str,
    switch_data: dict[str, dict[str, Any]],
) -> None:
    """Test an unreachable switch shows an error and the flow can still finish."""
    aioclient_mock.get(f"{BASE}/conf/key", exc=TimeoutError)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/conf/key", text=public_key_pem)
    aioclient_mock.post(endpoint("login"), json={"result": 0})
    for name, reply in switch_data.items():
        aioclient_mock.get(endpoint(name), json=reply)
    aioclient_mock.get(endpoint("logout"), json={"result": 0})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_unknown_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Test an unexpected error maps to unknown."""
    aioclient_mock.get(f"{BASE}/conf/key", exc=RuntimeError)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
    )
    assert result["errors"] == {"base": "unknown"}


async def test_user_flow_already_configured(
    hass: HomeAssistant,
    mock_switch: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the same switch can't be added twice, even under a new address."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={**USER_INPUT, CONF_HOST: "192.0.2.11"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert mock_config_entry.data[CONF_HOST] == "192.0.2.11"


async def test_reauth(
    hass: HomeAssistant,
    mock_switch: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test reauthenticating with a new password."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "new"


@pytest.mark.parametrize("login_result", [1])
async def test_reauth_rejected(
    hass: HomeAssistant,
    mock_switch: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test a rejected login during reauth shows an error."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


def ssdp_info(host: str = HOST) -> SsdpServiceInfo:
    """Return SSDP discovery info as a 210 switch announces itself."""
    return SsdpServiceInfo(
        ssdp_usn="uuid:7545c99a-0232-4f55-888e-AABBCCDDEEFF::upnp:rootdevice",
        ssdp_st="upnp:rootdevice",
        ssdp_location=f"http://{host}:27173/rootDesc.xml",
        upnp={
            ATTR_UPNP_DEVICE_TYPE: "urn:AN-210-SW-24-POE:device:Switch:1",
            ATTR_UPNP_SERIAL: "ST0000000000000X",
            ATTR_UPNP_UDN: "uuid:7545c99a-0232-4f55-888e-AABBCCDDEEFF",
        },
    )


async def test_ssdp_discovery(
    hass: HomeAssistant, mock_switch: AiohttpClientMocker
) -> None:
    """Test adding a switch found over SSDP only asks for the login."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_SSDP}, data=ssdp_info()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    assert result["description_placeholders"] == {"host": HOST}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "secret"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Rack switch"
    assert result["data"] == SWITCH_ENTRY_DATA
    assert result["result"].unique_id == MAC


@pytest.mark.parametrize("login_result", [1])
async def test_ssdp_discovery_rejected_login(
    hass: HomeAssistant, mock_switch: AiohttpClientMocker
) -> None:
    """Test a rejected login on a discovered switch shows an error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_SSDP}, data=ssdp_info()
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_ssdp_discovery_updates_host(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test rediscovering a configured switch at a new address updates it."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_SSDP}, data=ssdp_info("192.0.2.20")
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert mock_config_entry.data[CONF_HOST] == "192.0.2.20"
