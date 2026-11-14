"""Config flow for the Araknis Networks integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any
from urllib.parse import urlparse

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.service_info.ssdp import (
    ATTR_UPNP_DEVICE_TYPE,
    ATTR_UPNP_UDN,
    SsdpServiceInfo,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
import voluptuous as vol

from .api import (
    DEVICE_TYPE_ROUTER,
    DEVICE_TYPE_WEB_SMART,
    AraknisAuthError,
    AraknisConnectionError,
    AraknisRouterClient,
    AraknisSwitchClient,
    AraknisUnsupportedDeviceError,
    AraknisWebSmartClient,
    RouterInfo,
    SwitchInfo,
    WebSmartInfo,
    async_detect_device_type,
)
from .const import CONF_DEVICE_TYPE, DEFAULT_USERNAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)
LOGIN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class AraknisConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Araknis switches and routers."""

    VERSION = 1
    MINOR_VERSION = 2

    _host: str = ""

    _device_type: str = ""

    async def _async_try_login(
        self, host: str, username: str, password: str
    ) -> tuple[SwitchInfo | RouterInfo | WebSmartInfo | None, dict[str, str]]:
        """Log in and read the device's details. Return them and any errors.

        Also sets self._device_type.
        """
        session = async_get_clientsession(self.hass)
        client: AraknisSwitchClient | AraknisRouterClient | AraknisWebSmartClient
        try:
            self._device_type = await async_detect_device_type(session, host)
            if self._device_type == DEVICE_TYPE_ROUTER:
                client = AraknisRouterClient(session, host, username, password)
            elif self._device_type == DEVICE_TYPE_WEB_SMART:
                client = AraknisWebSmartClient(session, host, username, password)
            else:
                client = AraknisSwitchClient(session, host, username, password)
            info = await client.get_info()
        except AraknisAuthError:
            return None, {"base": "invalid_auth"}
        except AraknisUnsupportedDeviceError:
            return None, {"base": "not_supported"}
        except AraknisConnectionError:
            return None, {"base": "cannot_connect"}
        except Exception:
            _LOGGER.exception("Unexpected error setting up the Araknis device")
            return None, {"base": "unknown"}
        await client.logout()
        return info, {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the device's address and login."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._async_abort_entries_match({CONF_HOST: user_input[CONF_HOST]})
            info, errors = await self._async_try_login(
                user_input[CONF_HOST],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            if info:
                await self.async_set_unique_id(format_mac(info.mac))
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: user_input[CONF_HOST]}
                )
                return self.async_create_entry(
                    title=info.name,
                    data={**user_input, CONF_DEVICE_TYPE: self._device_type},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, user_input or {CONF_USERNAME: DEFAULT_USERNAME}
            ),
            errors=errors,
        )

    async def async_step_ssdp(
        self, discovery_info: SsdpServiceInfo
    ) -> ConfigFlowResult:
        """Handle a switch found over SSDP."""
        assert discovery_info.ssdp_location is not None
        host = urlparse(discovery_info.ssdp_location).hostname
        assert host is not None
        # The UDN ends with the switch's MAC address, e.g.
        # uuid:7545c99a-0232-4f55-888e-AABBCCDDEEFF.
        udn = discovery_info.upnp[ATTR_UPNP_UDN]
        await self.async_set_unique_id(format_mac(udn[-12:]))
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        self._host = host
        # The device type is urn:<model>:device:Switch:1.
        model = discovery_info.upnp[ATTR_UPNP_DEVICE_TYPE].split(":")[1]
        self.context["title_placeholders"] = {"name": f"{model} ({host})"}
        return await self.async_step_discovery_confirm()

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a router found over mDNS."""
        # The router's advertisement carries no MAC address, so a router that
        # is already set up is recognized by its address until the login
        # gives its MAC.
        host = discovery_info.host
        self._async_abort_entries_match({CONF_HOST: host})
        self._host = host
        # The service is named after the model, e.g. AN-310-RT-4L2W.
        model = discovery_info.name.split(".")[0]
        self.context["title_placeholders"] = {"name": f"{model} ({host})"}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the login of a discovered switch or router."""
        errors: dict[str, str] = {}
        if user_input is not None:
            info, errors = await self._async_try_login(
                self._host, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if info:
                await self.async_set_unique_id(format_mac(info.mac))
                self._abort_if_unique_id_configured(updates={CONF_HOST: self._host})
                return self.async_create_entry(
                    title=info.name,
                    data={
                        CONF_HOST: self._host,
                        **user_input,
                        CONF_DEVICE_TYPE: self._device_type,
                    },
                )
        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=self.add_suggested_values_to_schema(
                LOGIN_SCHEMA, user_input or {CONF_USERNAME: DEFAULT_USERNAME}
            ),
            description_placeholders={"host": self._host},
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after the device rejected the login."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new login."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            info, errors = await self._async_try_login(
                entry.data[CONF_HOST],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            if info:
                await self.async_set_unique_id(format_mac(info.mac))
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                LOGIN_SCHEMA, {CONF_USERNAME: entry.data[CONF_USERNAME]}
            ),
            description_placeholders={"host": entry.data[CONF_HOST]},
            errors=errors,
        )
