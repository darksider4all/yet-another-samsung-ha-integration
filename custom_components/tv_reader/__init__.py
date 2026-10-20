"""Samsung TV Reader — local, cloud-free Tizen TV telemetry + control.

Talks to the local bridge (the container on .113:8777), which itself
reads/controls the Samsung TV directly over its own WebSocket. No Samsung
cloud, no SmartThings, no account.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_HOST,
    CONF_POWER_DELEGATE,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

PLATFORMS = ["media_player", "sensor"]

_LOGGER = logging.getLogger(__package__)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Integration set up (config-flow driven; nothing to do via YAML)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up tv_reader from a config entry."""
    host = entry.data[CONF_HOST]
    interval = timedelta(seconds=entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))

    power_delegate = entry.options.get(CONF_POWER_DELEGATE) or None
    coordinator = TvReaderCoordinator(hass, host, interval, power_delegate)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_services(hass, coordinator)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change (e.g. the power-delegate entity)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


class TvReaderCoordinator(DataUpdateCoordinator):
    """Poll GET /v1/state and offer POST /v1/control against the bridge."""

    def __init__(self, hass: HomeAssistant, host: str, interval: timedelta,
                 power_delegate: str | None = None) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=interval,
        )
        self._host = host.rstrip("/")
        self.power_delegate = power_delegate
        self._session = aiohttp.ClientSession()

    async def _async_update_data(self) -> dict:
        try:
            async with self._session.get(
                f"{self._host}/v1/state", timeout=_REQUEST_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                return await resp.json()
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"bridge unreachable at {self._host}: {err}") from err

    async def control(self, action: str, **kwargs) -> dict:
        """Forward a control action to POST /v1/control."""
        payload = {"action": action, **kwargs}
        async with self._session.post(
            f"{self._host}/v1/control", json=payload, timeout=_REQUEST_TIMEOUT
        ) as resp:
            return await resp.json(content_type=None)

    async def notify(self, message: str) -> dict:
        """Show a toast/message on the TV via POST /v1/notify."""
        async with self._session.post(
            f"{self._host}/v1/notify", json={"message": message}, timeout=_REQUEST_TIMEOUT
        ) as resp:
            return await resp.json(content_type=None)

    async def cast(self, url: str, kind=None, duration: int = 30) -> dict:
        """Cast an image/video/web page to the TV via POST /v1/cast."""
        payload = {"url": url, "duration": duration}
        if kind:
            payload["kind"] = kind
        async with self._session.post(
            f"{self._host}/v1/cast", json=payload, timeout=_REQUEST_TIMEOUT
        ) as resp:
            return await resp.json(content_type=None)

    async def _async_close_session(self) -> None:
        await self._session.close()


def _register_services(hass: HomeAssistant, coordinator: TvReaderCoordinator) -> None:
    """Register tv_reader.* services that proxy POST /v1/control."""

    async def _launch_app(call: ServiceCall) -> None:
        await coordinator.control("launch_app", app_id=call.data["app_id"])

    async def _close_app(call: ServiceCall) -> None:
        await coordinator.control("close_app", app_id=call.data["app_id"])

    async def _send_key(call: ServiceCall) -> None:
        await coordinator.control("send_key", key=call.data["key"])

    hass.services.async_register(
        DOMAIN, "launch_app", _launch_app, schema=vol.Schema({vol.Required("app_id"): cv.string})
    )
    hass.services.async_register(
        DOMAIN, "close_app", _close_app, schema=vol.Schema({vol.Required("app_id"): cv.string})
    )
    hass.services.async_register(
        DOMAIN, "send_key", _send_key, schema=vol.Schema({vol.Required("key"): cv.string})
    )