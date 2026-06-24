"""Config flow for KBeacon Ring (Blue Charm)."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import (
    CONF_LED_OFF,
    CONF_LED_ON,
    CONF_MAC,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_RING_MS,
    CONF_RING_TYPE,
    DEFAULT_LED_OFF,
    DEFAULT_LED_ON,
    DEFAULT_PASSWORD,
    DEFAULT_RING_MS,
    DEFAULT_RING_TYPE,
    DOMAIN,
)


def _normalize_mac(mac: str) -> str:
    return mac.strip().upper().replace("-", ":")


class KBeaconRingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for KBeacon Ring."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            mac = _normalize_mac(user_input[CONF_MAC])
            await self.async_set_unique_id(mac)
            self._abort_if_unique_id_configured()
            name = user_input.get(CONF_NAME) or ("KBeacon %s" % mac[-5:].replace(":", ""))
            data = {
                CONF_MAC: mac,
                CONF_NAME: name,
                CONF_PASSWORD: user_input.get(CONF_PASSWORD) or DEFAULT_PASSWORD,
                CONF_RING_MS: int(user_input.get(CONF_RING_MS, DEFAULT_RING_MS)),
                CONF_RING_TYPE: int(user_input.get(CONF_RING_TYPE, DEFAULT_RING_TYPE)),
                CONF_LED_ON: DEFAULT_LED_ON,
                CONF_LED_OFF: DEFAULT_LED_OFF,
            }
            return self.async_create_entry(title=name, data=data)

        schema = vol.Schema(
            {
                vol.Required(CONF_MAC): str,
                vol.Optional(CONF_NAME, default=""): str,
                vol.Optional(CONF_PASSWORD, default=DEFAULT_PASSWORD): str,
                vol.Optional(CONF_RING_MS, default=DEFAULT_RING_MS): int,
                vol.Optional(CONF_RING_TYPE, default=DEFAULT_RING_TYPE): vol.In(
                    {0: "LED only", 1: "Beep only", 2: "LED + Beep"}
                ),
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )
