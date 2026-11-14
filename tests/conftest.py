"""Shared fixtures for the Araknis Networks tests.

The tests talk to the integration's real client, with the switch's HTTP API
mocked by `aioclient_mock`. The fixture data is a trimmed copy of a real
AN-210-SW-24-POE reply (ports 1, 2, 6 and 21, plus one SFP port) with the MAC
address replaced.
"""

from __future__ import annotations

from collections.abc import Generator
import re
from typing import Any
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant.common import (
    MockConfigEntry,
    load_fixture,
    load_json_object_fixture,
)
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.araknis.const import CONF_DEVICE_TYPE, DOMAIN

HOST = "192.0.2.10"
MAC = "aa:bb:cc:dd:ee:ff"
USER_INPUT = {CONF_HOST: HOST, CONF_USERNAME: "admin", CONF_PASSWORD: "secret"}
SWITCH_ENTRY_DATA = {**USER_INPUT, CONF_DEVICE_TYPE: "switch"}
BASE = f"http://{HOST}"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load the custom integration."""


@pytest.fixture(scope="session")
def public_key_pem() -> str:
    """A throwaway RSA key standing in for the switch's login key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    return (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


def endpoint(name: str) -> re.Pattern[str]:
    """Match one datajson.cgi endpoint (its name is the whole query string)."""
    return re.compile(rf"/cgi-bin/datajson\.cgi\?{name}$")


@pytest.fixture
def login_result() -> int:
    """The result code the switch answers a login with."""
    return 0


@pytest.fixture
def restart_result() -> int:
    """The result code the switch answers a restart with."""
    return 0


@pytest.fixture
def poe_off_ports() -> tuple[int, ...]:
    """Ports the mocked switch reports with PoE turned off."""
    return ()


@pytest.fixture
def has_temperature_sensor() -> bool:
    """Whether the mocked switch has a temperature sensor (the 8-port has none)."""
    return True


@pytest.fixture
def switch_data(
    poe_off_ports: tuple[int, ...], has_temperature_sensor: bool
) -> dict[str, dict[str, Any]]:
    """Replies for the read endpoints."""
    data = {
        name: load_json_object_fixture(f"{name}.json", DOMAIN)
        for name in (
            "poe_port",
            "port_cfg",
            "poe_global",
            "const_info",
            "sys_summary",
            "sys_info",
        )
    }
    # The MAC table comes in pages, requested as dyn_mac=<start>-<count>.
    data["dyn_mac=0-50"] = load_json_object_fixture("dyn_mac_0.json", DOMAIN)
    data["dyn_mac=50-50"] = load_json_object_fixture("dyn_mac_50.json", DOMAIN)
    if not has_temperature_sensor:
        # As the 8-port reports it: no lm63, tmp or loctmp at all.
        for key in ("lm63", "tmp", "loctmp"):
            del data["sys_summary"]["sys_summary"][key]
    for row in data["poe_port"]["poe_port"]["port"]:
        if row["po"] in poe_off_ports:
            row.update(en=0, st=0)
    return data


@pytest.fixture
def mock_switch(
    aioclient_mock: AiohttpClientMocker,
    public_key_pem: str,
    login_result: int,
    restart_result: int,
    switch_data: dict[str, dict[str, Any]],
) -> AiohttpClientMocker:
    """Mock a switch's web API."""
    aioclient_mock.get(re.compile(r"/conf/key$"), text=public_key_pem)
    aioclient_mock.post(endpoint("login"), json={"result": login_result})
    for name, reply in switch_data.items():
        aioclient_mock.get(endpoint(name), json=reply)
    aioclient_mock.post(endpoint("poe_port_setting"), json={"result": 0})
    aioclient_mock.post(endpoint("restart"), json={"result": restart_result})
    aioclient_mock.get(endpoint("logout"), json={"result": 0})
    return aioclient_mock


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A config entry for one switch."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Araknis switch ({HOST})",
        unique_id=MAC,
        data=SWITCH_ENTRY_DATA,
        minor_version=2,
    )


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_switch: AiohttpClientMocker,
) -> MockConfigEntry:
    """Set up the integration with the mocked switch."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


@pytest.fixture
def mock_setup_entry() -> Generator[None]:
    """Skip setting up the entry after the config flow creates it."""
    with patch("custom_components.araknis.async_setup_entry", return_value=True):
        yield


ROUTER_MAC = "aa:bb:cc:dd:ee:01"
ROUTER_ENTRY_DATA = {**USER_INPUT, CONF_DEVICE_TYPE: "router"}
ROUTER_LOGIN_PAGE = '<script>$.ajax({url: "/cgi-bin/login.cgi"})</script>'


def router_cgi(name: str) -> re.Pattern[str]:
    """Match one of the router's CGIs."""
    return re.compile(rf"/cgi-bin/{name}\.cgi$")


@pytest.fixture
def router_login_reply() -> dict[str, Any]:
    """What the router answers a login with."""
    return {"login_status": 1, "login_ID": "session-1"}


@pytest.fixture
def router_data() -> dict[str, dict[str, Any]]:
    """Replies for the router's read CGIs, from a real AN-310-RT-4L2W (sanitized)."""
    return {
        name: load_json_object_fixture(f"router_{name}.json", DOMAIN)
        for name in ("stat", "system_status", "wan_status_cards", "client_count")
    }


@pytest.fixture
def mock_router(
    aioclient_mock: AiohttpClientMocker,
    router_login_reply: dict[str, Any],
    router_data: dict[str, dict[str, Any]],
) -> AiohttpClientMocker:
    """Mock a router's web API."""
    # Routers redirect everything they don't serve without a login.
    aioclient_mock.get(
        re.compile(r"/conf/key$"), status=301, headers={"Location": "/login.html"}
    )
    aioclient_mock.get(re.compile(r"/login\.html$"), text=ROUTER_LOGIN_PAGE)
    aioclient_mock.post(router_cgi("login"), json=router_login_reply)
    for name, reply in router_data.items():
        aioclient_mock.get(router_cgi(name), json=reply)
    # The router's web page ignores what this answers, so its reply is unknown.
    aioclient_mock.post(router_cgi("system_restart"), text="")
    return aioclient_mock


@pytest.fixture
def router_config_entry() -> MockConfigEntry:
    """A config entry for one router."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="AN-310-RT-4L2W",
        unique_id=ROUTER_MAC,
        data=ROUTER_ENTRY_DATA,
        minor_version=2,
    )


@pytest.fixture
async def init_router(
    hass: HomeAssistant,
    router_config_entry: MockConfigEntry,
    mock_router: AiohttpClientMocker,
) -> MockConfigEntry:
    """Set up the integration with the mocked router."""
    router_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(router_config_entry.entry_id)
    await hass.async_block_till_done()
    return router_config_entry


WEB_SMART_MAC = "aa:bb:cc:dd:ee:02"
WEB_SMART_ENTRY_DATA = {**USER_INPUT, CONF_DEVICE_TYPE: "web_smart"}


def web_smart_page(name: str) -> re.Pattern[str]:
    """Match one of the web-smart switch's pages."""
    return re.compile(rf"/{name}\.cgi$")


@pytest.fixture
def web_smart_login_ok() -> bool:
    """Whether the mocked web-smart switch accepts the login."""
    return True


@pytest.fixture
def mock_web_smart(
    aioclient_mock: AiohttpClientMocker, web_smart_login_ok: bool
) -> AiohttpClientMocker:
    """Mock an AN-110 web-smart switch, from a real AN-110-SW-16 (sanitized)."""
    challenge = {"WWW-Authenticate": 'Basic realm="Switch"'}
    # Every page asks for a login; the mock can't check one, so /conf/key
    # stands in for a request without it.
    aioclient_mock.get(re.compile(r"/conf/key$"), status=401, headers=challenge)
    for name, fixture in (
        ("info", "web_smart_info.html"),
        ("webovrc", "web_smart_webovrc.html"),
    ):
        if web_smart_login_ok:
            aioclient_mock.get(web_smart_page(name), text=load_fixture(fixture, DOMAIN))
        else:
            aioclient_mock.get(web_smart_page(name), status=401, headers=challenge)
    aioclient_mock.post(
        web_smart_page("webovrc"), text=load_fixture("web_smart_webovrc.html", DOMAIN)
    )
    return aioclient_mock


@pytest.fixture
def web_smart_config_entry() -> MockConfigEntry:
    """A config entry for one web-smart switch."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="AN-110-SW-16",
        unique_id=WEB_SMART_MAC,
        data=WEB_SMART_ENTRY_DATA,
        minor_version=2,
    )


@pytest.fixture
async def init_web_smart(
    hass: HomeAssistant,
    web_smart_config_entry: MockConfigEntry,
    mock_web_smart: AiohttpClientMocker,
) -> MockConfigEntry:
    """Set up the integration with the mocked web-smart switch."""
    web_smart_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(web_smart_config_entry.entry_id)
    await hass.async_block_till_done()
    return web_smart_config_entry
