"""Config flow for the Samsung TV Reader integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_HOST,
    CONF_NAME,
    CONF_POWER_DELEGATE,
    CONF_SCAN_INTERVAL,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)


class TvReaderConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Yet Another Samsung Thing."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return TvReaderOptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        """Handle the initial step: point at the bridge."""
        errors = {}
        if user_input is not None:
            return self.async_create_entry(
                title=user_input.get(CONF_NAME, DEFAULT_NAME),
                data={
                    CONF_HOST: user_input[CONF_HOST].rstrip("/"),
                    CONF_NAME: user_input.get(CONF_NAME, DEFAULT_NAME),
                    CONF_SCAN_INTERVAL: user_input.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST, default="http://homeassistant.local:8777"
                    ): str,
                    vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                    ): int,
                }
            ),
            errors=errors,
        )


class TvReaderOptionsFlow(config_entries.OptionsFlow):
    """Options: the optional power-on fallback (e.g. a SmartThings media_player that can
    wake the TV from deep standby when the local KEY_POWER path cannot)."""

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = self.config_entry.options.get(CONF_POWER_DELEGATE, "")
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Optional(CONF_POWER_DELEGATE, default=current): str}
            ),
        )
