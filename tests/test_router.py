"""Tests for Araknis routers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import json
import re
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_fire_time_changed,
    load_json_object_fixture,
)
from pytest_homeassistant.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.araknis.api import (
    AraknisAuthError,
    AraknisConnectionError,
    AraknisRouterClient,
    _data_size,
    _wan_speed,
)
from custom_components.araknis.const import DOMAIN, SCAN_INTERVAL

from .conftest import HOST, ROUTER_MAC, router_cgi


def state_of(hass: HomeAssistant, platform: str, key: str) -> str:
    """Return the state of the router entity with this unique ID suffix."""
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{ROUTER_MAC}_{key}"
    )
    assert entity_id is not None, key
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state.state


def logins(aioclient_mock: AiohttpClientMocker) -> list[Any]:
    """Return the login requests (not logouts) sent to the router."""
    return [
        call
        for call in aioclient_mock.mock_calls
        if call[0] == "POST"
        and str(call[1]).endswith("login.cgi")
        and "logout" not in call[2]
    ]


async def test_setup_and_unload(
    hass: HomeAssistant,
    init_router: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Test the router loads, sends its session cookie, then logs out."""
    assert init_router.state is ConfigEntryState.LOADED
    reads = [c for c in aioclient_mock.mock_calls if c[0] == "GET"]
    assert reads
    assert all(c[3] == {"Cookie": "login_ID=session-1"} for c in reads)
    await hass.config_entries.async_unload(init_router.entry_id)
    assert init_router.state is ConfigEntryState.NOT_LOADED
    method, url, data, _ = aioclient_mock.mock_calls[-1]
    assert (method, str(url).endswith("login.cgi"), data) == (
        "POST",
        True,
        {"logout": "1"},
    )


@pytest.mark.usefixtures("init_router")
async def test_router_entities(hass: HomeAssistant) -> None:
    """Test the router's sensors."""
    assert state_of(hass, "sensor", "cpu_usage") == "0"
    # (499 - 387) / 499
    assert state_of(hass, "sensor", "memory_usage") == "22.4"
    assert state_of(hass, "sensor", "dhcp_clients") == "73"
    assert state_of(hass, "sensor", "static_clients") == "15"
    assert state_of(hass, "sensor", "total_clients") == "88"
    # .100 to .199
    assert state_of(hass, "sensor", "dhcp_pool_size") == "100"
    assert state_of(hass, "binary_sensor", "cloud_connection") == STATE_ON
    assert state_of(hass, "sensor", "last_restart") != STATE_UNKNOWN


async def test_dhcp_pool_size_follows_range_changes(
    hass: HomeAssistant,
    init_router: MockConfigEntry,
    router_data: dict[str, dict[str, Any]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a wider DHCP range shows up on the next poll, without a reload."""
    router_data["dhcp_status"]["DHCP"][0]["ip_range_min"] = "192.168.1.50"
    freezer.tick(SCAN_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert state_of(hass, "sensor", "dhcp_pool_size") == "150"


@pytest.mark.usefixtures("init_router")
async def test_wan_entities(hass: HomeAssistant) -> None:
    """Test each WAN port's entities: WAN1 is connected, WAN2 is not."""
    assert state_of(hass, "binary_sensor", "wan1_connected") == STATE_ON
    # The connection sensor is the WAN device's main entity.
    wan1 = hass.states.get("binary_sensor.an_310_rt_4l2w_wan1")
    assert wan1 is not None
    assert wan1.name == "WAN1"
    assert state_of(hass, "sensor", "wan1_link_speed") == "1000"
    assert state_of(hass, "binary_sensor", "wan2_connected") == STATE_OFF
    assert state_of(hass, "sensor", "wan2_link_speed") == STATE_UNKNOWN
    # The public IP address is disabled by default.
    entry = er.async_get(hass).async_get(
        er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{ROUTER_MAC}_wan1_ip_address"
        )
    )
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_router_devices(
    hass: HomeAssistant, init_router: MockConfigEntry
) -> None:
    """Test the router device and its WAN child devices."""
    registry = dr.async_get(hass)
    router = registry.async_get_device_by_identifier(
        (DOMAIN, ROUTER_MAC), init_router.entry_id
    )
    assert router is not None
    assert router.model == "AN-310-RT-4L2W"
    assert router.sw_version == "1.5.16"
    assert router.serial_number == "ST00000000000TEST"
    for wan in ("wan1", "wan2"):
        child = registry.async_get_child_device_by_identifier(
            (DOMAIN, f"{ROUTER_MAC}_{wan}"), init_router.entry_id
        )
        assert child is not None
        assert child.name == wan.upper()
        assert child.parent_device_id == router.id


async def test_session_expired_logs_in_again(
    hass: HomeAssistant,
    router_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    router_login_reply: dict[str, Any],
    router_data: dict[str, dict[str, Any]],
) -> None:
    """Test a redirect to the login page makes the client log in again and retry."""
    replies = iter([None, router_data["stat"]])

    async def stat(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        reply = next(replies)
        if reply is None:
            return AiohttpClientMockResponse(
                method, url, status=301, headers={"Location": "/login.html"}
            )
        return AiohttpClientMockResponse(method, url, json=reply)

    aioclient_mock.post(router_cgi("login"), json=router_login_reply)
    aioclient_mock.get(router_cgi("stat"), side_effect=stat)
    for name, reply in router_data.items():
        if name != "stat":
            aioclient_mock.get(router_cgi(name), json=reply)
    aioclient_mock.get(
        re.compile(r"/cgi-bin/port_status_table\.cgi\?info=port_status_table$"),
        json=load_json_object_fixture("router_port_status_table.json", DOMAIN),
    )
    router_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(router_config_entry.entry_id)
    assert router_config_entry.state is ConfigEntryState.LOADED
    assert len(logins(aioclient_mock)) == 2


async def test_poll_failure_makes_entities_unavailable(
    hass: HomeAssistant,
    init_router: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test entities go unavailable when the router stops answering."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(router_cgi("stat"), exc=TimeoutError)
    freezer.tick(SCAN_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert state_of(hass, "sensor", "cpu_usage") == STATE_UNAVAILABLE


@pytest.mark.parametrize(
    ("router_login_reply", "error", "message"),
    [
        ({"login_status": 0, "remain": 4}, AraknisAuthError, "4 attempts left"),
        ({"login_status": 2}, AraknisAuthError, "default password"),
        ({"login_status": 3, "pending": 1800}, AraknisAuthError, "locked"),
        ({"login_status": 4, "pending": 60}, AraknisConnectionError, "firmware"),
    ],
)
async def test_login_failures(
    hass: HomeAssistant,
    mock_router: AiohttpClientMocker,
    error: type[Exception],
    message: str,
) -> None:
    """Test each failed login status maps to the right error."""
    client = AraknisRouterClient(async_get_clientsession(hass), HOST, "admin", "bad")
    with pytest.raises(error, match=message):
        await client.get_data()


@pytest.mark.parametrize("router_login_reply", [{"login_status": 0, "remain": 4}])
async def test_failed_login_is_not_retried_right_away(
    hass: HomeAssistant, mock_router: AiohttpClientMocker
) -> None:
    """Test the client waits after a rejected login, so it can't trigger the lockout."""
    client = AraknisRouterClient(async_get_clientsession(hass), HOST, "admin", "bad")
    with pytest.raises(AraknisAuthError):
        await client.get_data()
    with pytest.raises(AraknisAuthError, match="suppressed"):
        await client.get_data()
    assert len(logins(mock_router)) == 1


@pytest.mark.parametrize("router_login_reply", [{"login_status": 0, "remain": 4}])
async def test_setup_rejected_login_starts_reauth(
    hass: HomeAssistant,
    router_config_entry: MockConfigEntry,
    mock_router: AiohttpClientMocker,
) -> None:
    """Test a rejected login fails setup and asks for a new login."""
    router_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(router_config_entry.entry_id)
    assert router_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_wan_speed_parsing() -> None:
    """Test link speeds as the router writes them."""
    assert _wan_speed("1Gbps") == 1000
    assert _wan_speed("100Mbps") == 100
    assert _wan_speed("2Gbps") == 2500
    assert _wan_speed("N/C") is None


@pytest.mark.usefixtures("init_router")
async def test_restart(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Test the restart button sends what the router's web page sends."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{ROUTER_MAC}_restart"
    )
    assert entity_id is not None
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    assert [
        (data, headers)
        for method, url, data, headers in aioclient_mock.mock_calls
        if method == "POST" and str(url).endswith("system_restart.cgi")
    ] == [({"reboot_status": "1"}, {"Cookie": "login_ID=session-1"})]


@pytest.mark.usefixtures("init_router")
async def test_wan_counters(hass: HomeAssistant) -> None:
    """Test the WAN ports' traffic and error counters from the Ports page."""
    assert state_of(hass, "sensor", "wan1_data_sent") == "414.3"
    assert state_of(hass, "sensor", "wan1_data_received") == "705.2"
    assert state_of(hass, "sensor", "wan1_errors") == "0"
    # WAN2 has no link, so the router shows "-".
    assert state_of(hass, "sensor", "wan2_data_sent") == STATE_UNKNOWN
    assert state_of(hass, "sensor", "wan2_errors") == STATE_UNKNOWN


def test_data_size_parsing() -> None:
    """Test counters as the Ports page writes them. Only GB has been seen."""
    assert _data_size("414.3 GB") == 414.3
    assert _data_size("512.0 MB") == 0.512
    assert _data_size("1.2 TB") == 1200.0
    assert _data_size("-") is None


def switch_id(hass: HomeAssistant, key: str) -> str:
    """Return the entity ID of the port forwarding switch with this rule key."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{ROUTER_MAC}_port_forward_{key}"
    )
    assert entity_id is not None, key
    return entity_id


@pytest.mark.usefixtures("init_router")
async def test_port_forward_switches(hass: HomeAssistant) -> None:
    """Test each port forwarding rule gets a switch named after its description."""
    state = hass.states.get(switch_id(hass, "both_wan0_80"))
    assert state is not None
    assert state.state == STATE_ON
    assert state.name == "AN-310-RT-4L2W Port forward DVR80"
    assert state.attributes["internal_ip"] == "192.168.1.250"
    assert state.attributes["external_port"] == "80"
    # A rule without a description is named after its protocol and port.
    state = hass.states.get(switch_id(hass, "tcp_wan0_8000-8010"))
    assert state is not None
    assert state.state == STATE_OFF
    assert state.name == "AN-310-RT-4L2W Port forward TCP 8000-8010"


@pytest.mark.usefixtures("init_router")
async def test_port_forward_toggle(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    router_data: dict[str, dict[str, Any]],
) -> None:
    """Test a toggle posts the whole list back with only that rule changed."""
    original = json.loads(json.dumps(router_data["port_forward_settings"]))
    entity_id = switch_id(hass, "both_wan0_80")
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    posts = [
        (data, headers)
        for method, url, data, headers in aioclient_mock.mock_calls
        if method == "POST" and str(url).endswith("port_forward_settings.cgi")
    ]
    assert len(posts) == 1
    data, headers = posts[0]
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    expected_rules = json.loads(json.dumps(original["forwardRule"]))
    expected_rules[0]["rule_enable"] = "0"
    assert json.loads(data) == {
        "forwardRule": expected_rules,
        "portTriggerEnable": "0",
        "portTrigger": [],
    }
    assert hass.states.get(entity_id).state == STATE_OFF

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    assert hass.states.get(entity_id).state == STATE_ON
    assert (
        router_data["port_forward_settings"]["forwardRule"] == original["forwardRule"]
    )


@pytest.mark.usefixtures("init_router")
async def test_port_forward_not_saved(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    router_data: dict[str, dict[str, Any]],
) -> None:
    """Test an error is raised when the router doesn't keep the change."""
    aioclient_mock.clear_requests()
    aioclient_mock.post(router_cgi("login"), json={"login_status": 1, "login_ID": "s"})
    aioclient_mock.get(
        router_cgi("port_forward_settings"), json=router_data["port_forward_settings"]
    )
    aioclient_mock.post(router_cgi("port_forward_settings"), text="")
    with pytest.raises(HomeAssistantError, match="don't match what was sent"):
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: switch_id(hass, "both_wan0_80")},
            blocking=True,
        )


async def test_new_port_forward_rule_is_added(
    hass: HomeAssistant,
    init_router: MockConfigEntry,
    router_data: dict[str, dict[str, Any]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a rule created on the router shows up at the next poll."""
    rules = router_data["port_forward_settings"]["forwardRule"]
    rules.append({**rules[0], "rule_external_port": "443", "rule_desc": "DVR443"})
    freezer.tick(SCAN_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(switch_id(hass, "both_wan0_443")).state == STATE_ON
    # A rule deleted on the router leaves its switch unavailable.
    del rules[0]
    freezer.tick(SCAN_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(switch_id(hass, "both_wan0_80")).state == STATE_UNAVAILABLE


@pytest.fixture
def fumbling_router(
    aioclient_mock: AiohttpClientMocker,
    router_login_reply: dict[str, Any],
    router_data: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[str, list[tuple[int, str]]], None]:
    """Mock the router with one read CGI that fumbles, then answers properly."""
    monkeypatch.setattr("custom_components.araknis.api._ROUTER_RETRY_DELAY", 0)
    ports = load_json_object_fixture("router_port_status_table.json", DOMAIN)
    cgis = {**router_data, "port_status_table": ports}

    def setup(name: str, bad_replies: list[tuple[int, str]]) -> None:
        answers = iter([*bad_replies, (200, json.dumps(cgis[name]))])

        async def answer(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
            status, text = next(answers)
            return AiohttpClientMockResponse(method, url, status=status, text=text)

        aioclient_mock.post(router_cgi("login"), json=router_login_reply)
        for cgi, reply in cgis.items():
            pattern = (
                re.compile(r"/cgi-bin/port_status_table\.cgi\?info=port_status_table$")
                if cgi == "port_status_table"
                else router_cgi(cgi)
            )
            if cgi == name:
                aioclient_mock.get(pattern, side_effect=answer)
            else:
                aioclient_mock.get(pattern, json=reply)

    return setup


def calls_to(aioclient_mock: AiohttpClientMocker, name: str) -> int:
    """Return how many times a router CGI was read."""
    return sum(
        1
        for call in aioclient_mock.mock_calls
        if call[0] == "GET" and f"/cgi-bin/{name}.cgi" in str(call[1])
    )


@pytest.mark.parametrize(
    ("name", "first_reply"),
    [
        ("port_status_table", (500, "Internal Server Error")),
        ("port_forward_settings", (200, "")),
    ],
)
async def test_fumbled_read_is_tried_again(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    fumbling_router: Callable[[str, list[tuple[int, str]]], None],
    name: str,
    first_reply: tuple[int, str],
) -> None:
    """Test a read the router fumbles once succeeds on the second try."""
    fumbling_router(name, [first_reply])
    client = AraknisRouterClient(async_get_clientsession(hass), HOST, "admin", "pw")
    data = await client.get_data()
    assert data.port_forwards
    assert calls_to(aioclient_mock, name) == 2


async def test_read_fumbled_twice_fails(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    fumbling_router: Callable[[str, list[tuple[int, str]]], None],
) -> None:
    """Test the client gives up after the second fumbled read."""
    fumbling_router("port_status_table", [(500, "Internal Server Error")] * 2)
    client = AraknisRouterClient(async_get_clientsession(hass), HOST, "admin", "pw")
    with pytest.raises(AraknisConnectionError, match="HTTP 500"):
        await client.get_data()
    assert calls_to(aioclient_mock, "port_status_table") == 2
