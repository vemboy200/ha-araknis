"""Clients for the web APIs of Araknis switches and routers.

This module has no Home Assistant imports so it can move into its own PyPI
library later.

Araknis publishes no API for this gear. These clients talk to the JSON CGIs
that the devices' own web pages use.

Switches (AN-210/310-SW), as worked out in
https://github.com/lovintexas/Araknis_Switch (MIT, tested on an
AN-310-SW-24-POE) and checked on AN-210-SW-8/16/24-POE: GET /conf/key returns
an RSA public key. The username and password are each encrypted with it
(PKCS#1 v1.5), base64 encoded and POSTed as `etvapqp` and `rctater` to
`datajson.cgi?login`. The session is tied to the client IP, there is no
cookie. Every reply carries `result`: 0 is success, 2 means not logged in (or
the session expired), 9 is an account lockout.

Routers (checked on an AN-310-RT-4L2W, firmware 1.5.16): the login page
encrypts the password with AES-128-CTR, using a key and IV written into the
page itself, and POSTs it with the username to /cgi-bin/login.cgi. The reply's
`login_ID` goes in a `login_ID` cookie. Without a valid session every CGI
redirects to /login.html. Five failed logins lock the login for 30 minutes.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from dataclasses import dataclass, replace
import ipaddress
import json
import re
import time
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

RESULT_OK = 0
RESULT_NOT_LOGGED_IN = 2
RESULT_LOCKED_OUT = 9

# After a rejected login, don't try again for this long: repeated bad logins
# can lock the account.
LOGIN_BACKOFF = 300

# PoE port status codes, as named by the switch's web page.
# Status codes as the switch's web page names them (status2str in its scripts).
POE_STATUS = {
    0: "disabled",
    1: "detecting",
    2: "delivering",
    3: "test_mode",
    4: "fault",
    5: "other_fault",
    6: "requesting",
    # Shown briefly while the switch applies a PoE setting change.
    7: "applying",
}
_DELIVERING = (2, 6)

# Same scaling the switch's web page uses for the raw voltage reading.
_VOLTAGE_SCALE = 64.45 / 1000

# Rows per dyn_mac request. The switch answers a plain dyn_mac with 50 rows.
_MAC_TABLE_PAGE_SIZE = 50

_TIMEOUT = aiohttp.ClientTimeout(total=20)


class AraknisError(Exception):
    """Base error for the Araknis client."""


class AraknisConnectionError(AraknisError):
    """The switch could not be reached or sent something unexpected."""


class AraknisAuthError(AraknisError):
    """The device rejected the username or password, or the account is locked."""


class _RouterFumbleError(AraknisConnectionError):
    """The router answered a CGI with HTTP 5xx or a reply that isn't JSON."""


class AraknisUnsupportedDeviceError(AraknisConnectionError):
    """The address answers, but not like a supported Araknis switch or router."""


@dataclass(frozen=True, slots=True)
class PoePort:
    """State of one PoE port."""

    number: int
    name: str
    poe_enabled: bool
    poe_status: str
    power: float
    """Watts."""
    voltage: float
    """Volts."""
    current: int
    """Milliamps."""
    link_up: bool
    link_speed: int | None
    """Mbit/s, None while the link is down."""
    connected_mac: str | None
    """MAC address of the device plugged into the port, from the switch's MAC table.

    None when the switch sees no device on the port, or several (an uplink to
    another switch or an access point).
    """


@dataclass(frozen=True, slots=True)
class SwitchData:
    """A snapshot of the switch's PoE state."""

    mac: str
    ports: dict[int, PoePort]
    power_total: float
    """Watts drawn by all PoE ports."""
    power_budget: int
    """Watts the switch can supply over PoE."""
    temperature: float | None
    """Switch temperature, None on models without a sensor (such as the 8-port).

    Presumably degrees Celsius: the web page never shows it, but readings
    around 30 match a switch in a rack.
    """
    uptime: int
    """Seconds since the switch started."""


@dataclass(frozen=True, slots=True)
class SwitchInfo:
    """Details about the switch that only change with a firmware update or setting."""

    mac: str
    model: str
    firmware: str
    serial_number: str
    name: str
    """Name set on the switch's System settings page."""


def _connected_macs(rows: list[dict[str, Any]]) -> dict[int, str]:
    """Return the MAC of the one device on each port that has one.

    A device can add locally administered MACs next to its own, like a NAS
    with a virtual machine on a bridged network, so those only count when a
    port has nothing else.
    """
    by_port: dict[int, set[str]] = {}
    for row in rows:
        by_port.setdefault(row["po"], set()).add(row["ma"].lower())
    connected: dict[int, str] = {}
    for port, macs in by_port.items():
        universal = {mac for mac in macs if not int(mac[:2], 16) & 0x02}
        candidates = universal or macs
        if len(candidates) == 1:
            connected[port] = candidates.pop()
    return connected


def _temperature(summary: dict[str, Any]) -> float | None:
    """Return the temperature, if the switch has a sensor (lm63 is set)."""
    if not summary.get("lm63") or "tmp" not in summary:
        return None
    return float(summary["tmp"])


class AraknisSwitchClient:
    """Talks to one Araknis switch."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._base = f"http://{host.strip()}"
        self._username = username
        self._password = password
        self._logged_in = False
        self._next_login = 0.0
        self._lock = asyncio.Lock()
        # Maps a port number to its row in the poe_port table, which is what
        # poe_port_setting addresses.
        self._rows: dict[int, int] = {}

    async def _request(
        self, method: str, path: str, data: dict[str, str] | str | None = None
    ) -> str:
        headers = None
        if isinstance(data, str):
            # poe_port_setting takes a pre-built form body like "p3=1,-1,-1,-1&".
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            async with self._session.request(
                method,
                f"{self._base}{path}",
                data=data,
                headers=headers,
                timeout=_TIMEOUT,
            ) as resp:
                resp.raise_for_status()
                return await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise AraknisConnectionError(
                f"Error talking to {self._base}: {err!r}"
            ) from err

    async def _request_json(
        self, method: str, endpoint: str, data: dict[str, str] | str | None = None
    ) -> dict[str, Any]:
        text = await self._request(method, f"/cgi-bin/datajson.cgi?{endpoint}", data)
        try:
            reply = json.loads(text)
        except ValueError as err:
            raise AraknisConnectionError(
                f"Switch sent invalid JSON for {endpoint}"
            ) from err
        if not isinstance(reply, dict):
            raise AraknisConnectionError(f"Switch sent unexpected data for {endpoint}")
        return reply

    async def _login(self) -> None:
        now = time.monotonic()
        if now < self._next_login:
            raise AraknisAuthError(
                "Login suppressed after a rejected login, "
                f"retrying in {int(self._next_login - now)} s"
            )
        pem = await self._request("GET", "/conf/key")
        try:
            key = serialization.load_pem_public_key(pem.strip().encode())
        except ValueError as err:
            raise AraknisConnectionError("Switch sent an invalid login key") from err
        if not isinstance(key, RSAPublicKey):
            raise AraknisConnectionError("Switch sent a non-RSA login key")

        def encrypt(value: str) -> str:
            return base64.b64encode(
                key.encrypt(value.encode(), padding.PKCS1v15())
            ).decode()

        reply = await self._request_json(
            "POST",
            "login",
            {"etvapqp": encrypt(self._username), "rctater": encrypt(self._password)},
        )
        if reply.get("result") != RESULT_OK:
            self._logged_in = False
            self._next_login = now + LOGIN_BACKOFF
            if reply.get("result") == RESULT_LOCKED_OUT:
                raise AraknisAuthError("The switch account is locked out")
            raise AraknisAuthError(
                f"The switch rejected the login (result {reply.get('result')})"
            )
        self._logged_in = True

    async def _call(
        self, method: str, endpoint: str, data: str | None = None
    ) -> dict[str, Any]:
        """Call an endpoint, logging in first or again when needed."""
        async with self._lock:
            for _ in range(2):
                if not self._logged_in:
                    await self._login()
                reply = await self._request_json(method, endpoint, data)
                result = reply.get("result")
                if result == RESULT_NOT_LOGGED_IN:
                    self._logged_in = False
                    continue
                if result != RESULT_OK:
                    raise AraknisConnectionError(
                        f"Switch returned result {result} for {endpoint}"
                    )
                return reply
        raise AraknisConnectionError("The switch keeps ending the session")

    async def _get_mac_table(self) -> list[dict[str, Any]]:
        """Read the switch's MAC address table, page by page as its web page does."""
        rows: list[dict[str, Any]] = []
        index = 0
        while True:
            reply = await self._call("GET", f"dyn_mac={index}-{_MAC_TABLE_PAGE_SIZE}")
            table = reply["dyn_mac"]
            rows.extend(table["mac"])
            # The web page stops at -1. Also stop if the switch points back,
            # so a bad reply can't keep the poll going forever.
            if table["next"] == -1 or table["next"] <= index:
                return rows
            index = table["next"]

    async def get_data(self) -> SwitchData:
        """Read PoE, link, PoE budget and MAC table data."""
        poe_reply = await self._call("GET", "poe_port")
        cfg_reply = await self._call("GET", "port_cfg")
        global_reply = await self._call("GET", "poe_global")
        summary_reply = await self._call("GET", "sys_summary")
        try:
            connected = _connected_macs(await self._get_mac_table())
            poe_rows = poe_reply["poe_port"]["port"]
            cfg_rows = {row["po"]: row for row in cfg_reply["port_cfg"]["port"]}
            poe_global = global_reply["poe_global"]
            mac = poe_reply["mac_address"]
            ports: dict[int, PoePort] = {}
            rows: dict[int, int] = {}
            for index, row in enumerate(poe_rows, start=1):
                number = row["po"]
                rows[number] = index
                cfg = cfg_rows.get(number, {})
                live = row["st"] in _DELIVERING
                link_up = cfg.get("up") == 1
                ports[number] = PoePort(
                    number=number,
                    name=cfg.get("na") or f"Port {number}",
                    poe_enabled=row["en"] == 1,
                    poe_status=POE_STATUS.get(row["st"], "unknown"),
                    power=round(row["ppw"] / 10, 1) if live else 0.0,
                    voltage=round(row["vol"] * _VOLTAGE_SCALE, 1) if live else 0.0,
                    current=row["amp"] if live else 0,
                    link_up=link_up,
                    link_speed=cfg.get("sp") if link_up else None,
                    connected_mac=connected.get(number) if link_up else None,
                )
            data = SwitchData(
                mac=mac,
                ports=ports,
                power_total=round(poe_global["ap"] / 10, 1),
                power_budget=poe_global["pb"],
                temperature=_temperature(summary_reply["sys_summary"]),
                uptime=int(summary_reply["sys_summary"]["up"]),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise AraknisConnectionError(
                f"Switch sent PoE data in an unexpected format: {err!r}"
            ) from err
        self._rows = rows
        return data

    async def get_info(self) -> SwitchInfo:
        """Read the model, firmware, serial number and name."""
        const_reply = await self._call("GET", "const_info")
        summary_reply = await self._call("GET", "sys_summary")
        info_reply = await self._call("GET", "sys_info")
        try:
            const = const_reply["const_info"]
            summary = summary_reply["sys_summary"]
            return SwitchInfo(
                mac=const["mac_address"],
                model=const["model_name"],
                firmware=const.get("switch_version") or summary["fv"],
                serial_number=summary["sn"],
                name=info_reply["sys_info"].get("na") or const["model_name"],
            )
        except (KeyError, TypeError) as err:
            raise AraknisConnectionError(
                f"Switch sent its details in an unexpected format: {err!r}"
            ) from err

    async def _set_port(self, port: int, enable: int = -1, cycle: int = -1) -> None:
        # Fields are enable, power class, priority, cycle; -1 leaves one unchanged.
        if not self._rows:
            await self.get_data()
        if port not in self._rows:
            raise AraknisError(f"Port {port} has no PoE")
        body = f"p{self._rows[port]}={enable},-1,-1,{cycle}&"
        await self._call("POST", "poe_port_setting", body)

    async def set_poe(self, port: int, enabled: bool) -> None:
        """Turn PoE on or off for a port."""
        await self._set_port(port, enable=1 if enabled else 0)

    async def power_cycle(self, port: int) -> None:
        """Briefly cut PoE power to a port and turn it back on."""
        await self._set_port(port, cycle=1)

    async def restart(self) -> None:
        """Restart the switch, as its Restart Device page does."""
        await self._call("POST", "restart", "op=0")
        # The restart ends the session.
        self._logged_in = False

    async def logout(self) -> None:
        """End the session on the switch."""
        if not self._logged_in:
            return
        self._logged_in = False
        with contextlib.suppress(AraknisConnectionError):
            await self._request("GET", "/cgi-bin/datajson.cgi?logout")


# The router's login page encrypts the password with this fixed key and IV.
_ROUTER_AES_KEY = bytes.fromhex("7C37B3AB57CB46B491126CBE93901B55")
_ROUTER_AES_IV = bytes.fromhex("AD9A57619E8A4F200C0015A02EA1DCDE")

# login_status values, as described in the router's login page.
_ROUTER_LOGIN_OK = 1
_ROUTER_LOGIN_DEFAULT_PASSWORD = 2
_ROUTER_LOGIN_LOCKED_OUT = 3
_ROUTER_LOGIN_UPGRADING = 4

_REDIRECTS = (301, 302, 303, 307, 308)
# How long to wait before reading a router CGI a second time.
_ROUTER_RETRY_DELAY = 1

_NO_ADDRESS = "0.0.0.0"


@dataclass(frozen=True, slots=True)
class RouterWan:
    """State of one WAN port."""

    interface: str
    """WAN1, WAN2 and so on."""
    name: str
    connected: bool
    """The port has a link and an IP address."""
    ip_address: str | None
    gateway: str | None
    connection_type: str
    """DHCP, static and so on."""
    link_speed: int | None
    """Mbit/s, None while the link is down."""
    data_sent: float | None = None
    """Gigabytes sent since the router started, None without a link."""
    data_received: float | None = None
    """Gigabytes received since the router started, None without a link."""
    errors: int | None = None
    """Errors counted on the port, None without a link."""


@dataclass(frozen=True, slots=True)
class RouterData:
    """A snapshot of the router's state."""

    cpu_usage: int
    """Percent."""
    memory_usage: float
    """Percent of memory in use."""
    uptime: int
    """Seconds since the router started."""
    cloud_connected: bool
    """Connected to the OvrC cloud service."""
    dhcp_clients: int
    """DHCP leases in use."""
    static_clients: int
    """Clients with an address set on the device itself rather than by DHCP."""
    total_clients: int
    """Every client the router knows of, DHCP and static, including ones that left."""
    wans: dict[str, RouterWan]
    port_forwards: dict[str, PortForward]
    """Port forwarding rules by key."""


@dataclass(frozen=True, slots=True)
class PortForward:
    """One port forwarding rule."""

    key: str
    """Protocol, WAN and external port, which the router keeps unique."""
    description: str
    enabled: bool
    protocol: str
    """TCP, UDP or BOTH."""
    external_port: str
    """A port or a range like "100-200"."""
    internal_ip: str
    internal_port: str


def _port_forward_key(rule: dict[str, Any]) -> str:
    """Identify a rule the way the router's page checks for conflicts.

    The router's token_index is only the rule's position in the list, so it
    changes when an earlier rule is deleted.
    """
    return "_".join(
        (
            str(rule["rule_protocol"]).lower(),
            f"wan{rule['rule_external_ip']}",
            str(rule["rule_external_port"]),
        )
    )


def _parse_port_forward(rule: dict[str, Any]) -> PortForward:
    return PortForward(
        key=_port_forward_key(rule),
        description=str(rule.get("rule_desc") or ""),
        enabled=str(rule["rule_enable"]) == "1",
        protocol=str(rule["rule_protocol"]),
        external_port=str(rule["rule_external_port"]),
        internal_ip=str(rule["rule_internal_ip"]),
        internal_port=str(rule["rule_internal_port"]),
    )


@dataclass(frozen=True, slots=True)
class RouterInfo:
    """Details about the router that only change with a firmware update or setting."""

    mac: str
    """MAC address of the LAN side."""
    model: str
    firmware: str
    serial_number: str
    """The service tag."""
    name: str
    dhcp_pool_size: int
    """Addresses in the DHCP ranges of all VLANs, reservations included."""


def _dhcp_pool_size(dhcp_status: dict[str, Any]) -> int:
    """Add up the size of each VLAN's DHCP range."""
    return sum(
        int(ipaddress.ip_address(vlan["ip_range_max"]))
        - int(ipaddress.ip_address(vlan["ip_range_min"]))
        + 1
        for vlan in dhcp_status["DHCP"]
    )


def _wan_speed(text: str) -> int | None:
    """Turn the router's link speed, like "1Gbps" or "N/C", into Mbit/s."""
    match = re.fullmatch(r"(\d+)\s*([GM])bps", text.strip(), re.IGNORECASE)
    if not match:
        return None
    value = int(match[1])
    if match[2].upper() == "G":
        # The router reports 2.5 Gbit/s as "2Gbps"; its web page shows it as 2.5.
        value = 2500 if value == 2 else value * 1000
    return value


def _address(value: str) -> str | None:
    return None if not value or value == _NO_ADDRESS else value


# The Ports page's counters come rounded and with a unit, like "414.3 GB".
_DATA_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*([KMGT])B", re.IGNORECASE)
_TO_GIGABYTES = {"K": 1e-6, "M": 1e-3, "G": 1.0, "T": 1e3}


def _data_size(text: str) -> float | None:
    """Turn a counter like "414.3 GB" into gigabytes. None for "-" or an unknown unit."""
    match = _DATA_SIZE.fullmatch(text.strip())
    if not match:
        return None
    return float(match[1]) * _TO_GIGABYTES[match[2].upper()]


def _error_count(text: str) -> int | None:
    """Turn the error counter into a number. None for "-" (no link)."""
    text = text.strip()
    return int(text) if text.isdigit() else None


def _parse_wan(
    row: dict[str, Any], counters: dict[str, Any] | None = None
) -> RouterWan:
    speed = _wan_speed(row["current_speed"])
    ip_address = _address(row["IP_address"])
    counters = counters or {}
    return RouterWan(
        data_sent=_data_size(counters.get("sent", "-")),
        data_received=_data_size(counters.get("received", "-")),
        errors=_error_count(counters.get("errors", "-")),
        interface=row["interface"],
        name=row.get("name") or row["interface"],
        connected=speed is not None and ip_address is not None,
        ip_address=ip_address,
        gateway=_address(row["default_gateway"]),
        connection_type=row["connection_type"],
        link_speed=speed,
    )


class AraknisRouterClient:
    """Talks to one Araknis router."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._base = f"http://{host.strip()}"
        self._username = username
        self._password = password
        self._login_id: str | None = None
        self._next_login = 0.0
        self._lock = asyncio.Lock()

    def _headers(self) -> dict[str, str] | None:
        # Sent by hand rather than through the session's cookie jar, which
        # Home Assistant shares between integrations.
        if self._login_id is None:
            return None
        return {"Cookie": f"login_ID={self._login_id}"}

    async def _request(
        self, method: str, path: str, data: dict[str, str] | str | None = None
    ) -> str | None:
        """Send a request. Return the body, or None if the router redirected to its login page."""
        headers = self._headers() or {}
        if isinstance(data, str):
            # The settings pages post JSON text with a form content type.
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            async with self._session.request(
                method,
                f"{self._base}{path}",
                data=data,
                headers=headers or None,
                allow_redirects=False,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status in _REDIRECTS:
                    return None
                if resp.status >= 500:
                    raise _RouterFumbleError(
                        f"Router answered {path} with HTTP {resp.status}"
                    )
                resp.raise_for_status()
                return await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise AraknisConnectionError(
                f"Error talking to {self._base}: {err!r}"
            ) from err

    @staticmethod
    def _parse(text: str | None, what: str) -> dict[str, Any]:
        if text is None:
            raise AraknisConnectionError(f"Router redirected {what} to its login page")
        try:
            reply = json.loads(text)
        except ValueError as err:
            raise _RouterFumbleError(f"Router sent invalid JSON for {what}") from err
        if not isinstance(reply, dict):
            raise AraknisConnectionError(f"Router sent unexpected data for {what}")
        return reply

    async def _login(self) -> None:
        now = time.monotonic()
        if now < self._next_login:
            raise AraknisAuthError(
                "Login suppressed after a rejected login, "
                f"retrying in {int(self._next_login - now)} s"
            )
        encryptor = Cipher(
            algorithms.AES(_ROUTER_AES_KEY), modes.CTR(_ROUTER_AES_IV)
        ).encryptor()
        password = base64.b64encode(
            encryptor.update(self._password.encode()) + encryptor.finalize()
        ).decode()
        self._login_id = None
        reply = self._parse(
            await self._request(
                "POST",
                "/cgi-bin/login.cgi",
                {"username": self._username, "password": password},
            ),
            "login",
        )
        try:
            status = int(reply["login_status"])
        except (KeyError, TypeError, ValueError) as err:
            raise AraknisConnectionError(
                "Router sent an unexpected login reply"
            ) from err
        if status == _ROUTER_LOGIN_OK and reply.get("login_ID"):
            self._login_id = str(reply["login_ID"])
            return
        if status == _ROUTER_LOGIN_UPGRADING:
            raise AraknisConnectionError("The router is installing a firmware update")
        self._next_login = now + LOGIN_BACKOFF
        if status == _ROUTER_LOGIN_LOCKED_OUT:
            raise AraknisAuthError(
                "The router locked the login after too many failed attempts "
                f"({reply.get('pending')} s left)"
            )
        if status == _ROUTER_LOGIN_DEFAULT_PASSWORD:
            raise AraknisAuthError(
                "The router still has its default password; "
                "set a new one on its web page first"
            )
        raise AraknisAuthError(
            f"The router rejected the login ({reply.get('remain')} attempts left)"
        )

    async def _send(
        self,
        method: str,
        endpoint: str,
        data: dict[str, str] | str | None = None,
        query: str = "",
    ) -> str:
        """Call a CGI, logging in first or again when needed."""
        async with self._lock:
            for _ in range(2):
                if self._login_id is None:
                    await self._login()
                text = await self._request(
                    method, f"/cgi-bin/{endpoint}.cgi{query}", data
                )
                if text is None:
                    # The session expired.
                    self._login_id = None
                    continue
                return text
        raise AraknisConnectionError("The router keeps ending the session")

    async def _get(self, endpoint: str, query: str = "") -> dict[str, Any]:
        """Read a CGI, trying a second time if the router fumbles the first.

        Its CGIs sometimes answer with HTTP 500 or a reply that isn't JSON
        when another client reads them at the same moment, as seen with two
        Home Assistant instances polling one router.
        """
        try:
            return self._parse(await self._send("GET", endpoint, query=query), endpoint)
        except _RouterFumbleError:
            await asyncio.sleep(_ROUTER_RETRY_DELAY)
        return self._parse(await self._send("GET", endpoint, query=query), endpoint)

    async def restart(self) -> None:
        """Restart the router, as the restart button on its web page does."""
        await self._send("POST", "system_restart", {"reboot_status": "1"})
        # The restart ends the session.
        self._login_id = None

    async def set_port_forward(self, key: str, enabled: bool) -> dict[str, PortForward]:
        """Turn one port forwarding rule on or off and return the rules.

        The router only takes the whole list, so this posts back what it just
        read with one rule changed, as its Port Forwarding page does, then reads
        the list again to check the change was saved and nothing else was.
        """
        settings = await self._get("port_forward_settings")
        try:
            rules = settings["forwardRule"]
            before = [_parse_port_forward(rule) for rule in rules]
            rule = next(r for r in rules if _port_forward_key(r) == key)
        except StopIteration as err:
            raise AraknisConnectionError(
                f"The router has no port forwarding rule {key}"
            ) from err
        except (KeyError, TypeError) as err:
            raise AraknisConnectionError(
                f"Router sent its port forwarding rules in an unexpected format: {err!r}"
            ) from err
        rule["rule_enable"] = "1" if enabled else "0"
        await self._send(
            "POST",
            "port_forward_settings",
            json.dumps(
                {
                    "forwardRule": rules,
                    "portTriggerEnable": settings.get("portTriggerEnable", "0"),
                    "portTrigger": settings.get("portTrigger", []),
                }
            ),
        )
        after = await self.get_port_forwards()
        expected = {
            r.key: replace(r, enabled=enabled) if r.key == key else r for r in before
        }
        if after != expected:
            raise AraknisConnectionError(
                "The router's port forwarding rules don't match what was sent"
            )
        return after

    async def get_port_forwards(self) -> dict[str, PortForward]:
        """Read the port forwarding rules."""
        settings = await self._get("port_forward_settings")
        try:
            rules = [_parse_port_forward(rule) for rule in settings["forwardRule"]]
        except (KeyError, TypeError) as err:
            raise AraknisConnectionError(
                f"Router sent its port forwarding rules in an unexpected format: {err!r}"
            ) from err
        return {rule.key: rule for rule in rules}

    async def get_info(self) -> RouterInfo:
        """Read the model, firmware, service tag and name."""
        status = await self._get("system_status")
        # A big reply that lists every client, so it's read only here.
        dhcp_status = await self._get("dhcp_status")
        try:
            return RouterInfo(
                mac=status["LanMAC"],
                model=status["model"],
                firmware=status["firmware"],
                serial_number=status["service_tag"],
                name=status.get("name") or status["model"],
                dhcp_pool_size=_dhcp_pool_size(dhcp_status),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise AraknisConnectionError(
                f"Router sent its details in an unexpected format: {err!r}"
            ) from err

    async def get_data(self) -> RouterData:
        """Read CPU, memory, uptime, WAN and DHCP data."""
        stat = await self._get("stat")
        wan_reply = await self._get("wan_status_cards")
        clients = await self._get("client_count")
        # What the Ports page asks for; without the parameter the reply is empty.
        ports = await self._get("port_status_table", "?info=port_status_table")
        port_forwards = await self.get_port_forwards()
        try:
            counters = {row["interface"]: row for row in ports.get("WANs", [])}
            # Same formula as the router's web page.
            memory_total = int(stat["memTotal"])
            memory_used = memory_total - int(stat["memFree"])
            wans = [
                _parse_wan(row, counters.get(row["interface"]))
                for row in wan_reply["WANs"]
                if str(row.get("is_hidden")) != "1"
            ]
            return RouterData(
                cpu_usage=int(stat["cpu"]),
                memory_usage=round(memory_used / memory_total * 100, 1),
                uptime=int(stat["uptime"]),
                cloud_connected=str(stat["cloudConnected"]) == "1",
                dhcp_clients=int(clients["DHCP"]["used"]),
                static_clients=int(clients["static"]),
                total_clients=int(clients["DHCP"]["client_table_count"]),
                wans={wan.interface: wan for wan in wans},
                port_forwards=port_forwards,
            )
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as err:
            raise AraknisConnectionError(
                f"Router sent its status in an unexpected format: {err!r}"
            ) from err

    async def logout(self) -> None:
        """End the session on the router."""
        if self._login_id is None:
            return
        with contextlib.suppress(AraknisConnectionError):
            await self._request("POST", "/cgi-bin/login.cgi", {"logout": "1"})
        self._login_id = None


@dataclass(frozen=True, slots=True)
class WebSmartData:
    """A snapshot of a web-smart switch's state."""

    uptime: int
    """Seconds since the switch started."""
    cloud_connected: bool
    """Connected to the OvrC cloud service."""
    cloud_enabled: bool
    """The OvrC setting is turned on."""


@dataclass(frozen=True, slots=True)
class WebSmartInfo:
    """Details about a web-smart switch that only change with a firmware update or setting."""

    mac: str
    model: str
    firmware: str
    serial_number: str
    """The service tag."""
    name: str


_INFO_ROW = re.compile(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td", re.DOTALL)
_UPTIME = re.compile(r"(?:(\d+)\s*days?\s*)?(\d+):(\d{2}):(\d{2})")
_OVRC_CHECKED = re.compile(
    r'name="ovrc_state"\s+value="([01])"\s+checked', re.IGNORECASE
)


def _parse_uptime(text: str) -> int:
    """Turn an uptime like "85 days 15:51:39" into seconds."""
    match = _UPTIME.search(text)
    if not match:
        raise ValueError(f"Unexpected uptime {text!r}")
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


class AraknisWebSmartClient:
    """Talks to one Araknis web-smart switch (AN-110-SW), checked on an AN-110-SW-16.

    Every page sits behind HTTP Basic authentication, with no session. The
    pages are server-rendered HTML: info.cgi is the System Information table,
    webovrc.cgi the OvrC setting form.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._base = f"http://{host.strip()}"
        self._headers = {"Authorization": aiohttp.encode_basic_auth(username, password)}
        self._next_login = 0.0

    async def _request(
        self, method: str, page: str, data: dict[str, str] | None = None
    ) -> str:
        now = time.monotonic()
        if now < self._next_login:
            raise AraknisAuthError(
                "Login suppressed after a rejected login, "
                f"retrying in {int(self._next_login - now)} s"
            )
        try:
            async with self._session.request(
                method,
                f"{self._base}/{page}",
                data=data,
                headers=self._headers,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 401:
                    self._next_login = now + LOGIN_BACKOFF
                    raise AraknisAuthError("The switch rejected the login")
                resp.raise_for_status()
                return await resp.text(errors="replace")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise AraknisConnectionError(
                f"Error talking to {self._base}: {err!r}"
            ) from err

    async def _get_info_table(self) -> dict[str, str]:
        page = await self._request("GET", "info.cgi")
        return {
            re.sub(r"<[^>]+>", "", key).strip(): re.sub(r"<[^>]+>", "", value).strip()
            for key, value in _INFO_ROW.findall(page)
        }

    async def get_info(self) -> WebSmartInfo:
        """Read the model, MAC, firmware, service tag and name."""
        table = await self._get_info_table()
        try:
            return WebSmartInfo(
                mac=table["MAC Address"],
                model=table["Device Type"],
                firmware=table["Firmware Version"],
                serial_number=table["Service Tag"],
                name=table.get("Device Name") or table["Device Type"],
            )
        except KeyError as err:
            raise AraknisConnectionError(
                f"Switch sent its details in an unexpected format: {err!r}"
            ) from err

    async def get_data(self) -> WebSmartData:
        """Read the uptime and OvrC state."""
        table = await self._get_info_table()
        ovrc_page = await self._request("GET", "webovrc.cgi")
        checked = _OVRC_CHECKED.search(ovrc_page)
        if checked is None:
            raise AraknisConnectionError("Switch sent no OvrC setting")
        try:
            return WebSmartData(
                uptime=_parse_uptime(table["System Uptime"]),
                cloud_connected=table["OvrC State"] == "Connected",
                cloud_enabled=checked[1] == "1",
            )
        except (KeyError, ValueError) as err:
            raise AraknisConnectionError(
                f"Switch sent its status in an unexpected format: {err!r}"
            ) from err

    async def set_cloud(self, enabled: bool) -> None:
        """Turn the OvrC cloud service on or off, as the OvrC Setting page does."""
        await self._request(
            "POST",
            "webovrc.cgi",
            {"ovrc_state": "1" if enabled else "0", "cmd": "OvrC Setting"},
        )

    async def restart(self) -> None:
        """Restart the switch, as the Reboot button on its Restart Device page does."""
        await self._request("POST", "reboot.cgi", {"cmd": "reboot"})

    async def logout(self) -> None:
        """Do nothing: Basic authentication has no session to end."""


DEVICE_TYPE_SWITCH = "switch"
DEVICE_TYPE_ROUTER = "router"
DEVICE_TYPE_WEB_SMART = "web_smart"


async def async_detect_device_type(session: aiohttp.ClientSession, host: str) -> str:
    """Tell a switch from a router, without logging in.

    Switches serve their login key at /conf/key. Routers redirect that to their
    login page, which posts to /cgi-bin/login.cgi. Web-smart switches ask for
    a Basic authentication login (realm "Switch") on every page.
    """
    base = f"http://{host.strip()}"

    async def fetch(path: str) -> tuple[int, str, str]:
        try:
            async with session.get(
                f"{base}{path}", allow_redirects=False, timeout=_TIMEOUT
            ) as resp:
                return (
                    resp.status,
                    resp.headers.get("WWW-Authenticate", ""),
                    await resp.text(errors="replace"),
                )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise AraknisConnectionError(f"Error talking to {base}: {err!r}") from err

    status, auth, text = await fetch("/conf/key")
    if status == 200 and "PUBLIC KEY" in text:
        return DEVICE_TYPE_SWITCH
    if status == 401 and auth.startswith("Basic") and 'realm="Switch"' in auth:
        return DEVICE_TYPE_WEB_SMART
    status, _, text = await fetch("/login.html")
    if status == 200 and "/cgi-bin/login.cgi" in text:
        return DEVICE_TYPE_ROUTER
    raise AraknisUnsupportedDeviceError(
        f"{base} does not look like a supported Araknis switch or router"
    )
