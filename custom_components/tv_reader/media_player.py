"""Media player for the Samsung TV Reader integration.

One device entity surfacing power/volume/mute/source + live channel, program and
foreground app, with control routed through the bridge coordinator
(POST /v1/control, agent-executed) and toast via POST /v1/notify.
"""
from __future__ import annotations

import logging
import re

import voluptuous as vol

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from . import TvReaderCoordinator

_LOGGER = logging.getLogger(__name__)

# Power on/off is delegated to the samsungtv_smart entity, which wakes this Wi-Fi
# TV reliably via the SmartThings cloud (power_on_method=2). (Plain WoL/WoWLAN does
# NOT wake this TV.) Set to None to disable delegation.

# Foreground-app ids with special meaning. "Watching TV" is itself an app on Tizen
# (org.tizen.tv-viewer); the Smart Hub / home screen is com.samsung.tv.csfs; our own
# hidden agent briefly shows itself as foreground right after a (re)launch.
LIVE_TV_ID = "org.tizen.tv-viewer"
HOME_ID = "com.samsung.tv.csfs"
AGENT_ID = "ChanProbe0.ChannelProbe"

# Foreground app id -> friendly label (the App row / app_name). AGENT_ID maps to None
# so our own overlay never shows up as "the app".
FRIENDLY = {
    LIVE_TV_ID: "Live TV",
    HOME_ID: "Home",
    AGENT_ID: None,
    "com.samsung.tv.cobalt-yt": "YouTube",
    "111299001912": "YouTube",
    "3201907018807": "Netflix",
    "11101200001": "Netflix",
    "3201910019365": "Prime Video",
    "3201512006963": "Plex",
    "3201901017640": "Disney+",
    "3201606009684": "Spotify",
    "3202203026841": "Twitch",
    "3201601007230": "HBO Max",
    "3202301029760": "Max",
    "3202008022128": "Max",
    "3201807016597": "Apple TV",
    "3201907018784": "Apple TV",
    "org.tizen.browser": "Internet",
}

# Curated source menu for the card dropdown (Live TV + a few apps), instead of the
# full ~200-app catalog. (name, tizen_app_id) - edit this list to taste.
# Source menu = "Live TV" (launches the tuner app) + the TV's HDMI inputs (switched
# via tizen.tvwindow.setSource, which does drive the real display here) + a curated
# app shortlist. HDMI inputs + names come dynamically from the agent (VIDEOSOURCE).
LIVE_TV_APP = "org.tizen.tv-viewer"
SOURCE_APPS = [
    ("Netflix",     "org.tizen.netflix-app"),
    ("YouTube",     "com.samsung.tv.cobalt-yt"),
    ("Prime Video", "org.tizen.primevideo"),
    ("Disney+",     "MCmYXNxgcu.DisneyPlus"),
    ("Plex",        "kIciSQlYEM.plex"),
    ("Spotify",     "rJeHak5zRg.Spotify"),
]

# Artwork is served by the bridge at <host>/img/<file> (cat fallback for anything missing).
# foreground app id -> artwork file (original themed tiles, not brand logos)
APP_IMG = {
    "com.samsung.tv.cobalt-yt": "app_youtube.png", "111299001912": "app_youtube.png",
    "9Ur5IzDKqV.TizenYouTube": "app_youtube.png",
    "org.tizen.netflix-app": "app_netflix.png", "3201907018807": "app_netflix.png",
    "org.tizen.primevideo": "app_prime.png", "3201910019365": "app_prime.png",
    "MCmYXNxgcu.DisneyPlus": "app_disney.png", "3201901017640": "app_disney.png",
    "kIciSQlYEM.plex": "app_plex.png", "3201512006963": "app_plex.png",
    "rJeHak5zRg.Spotify": "app_spotify.png", "3201606009684": "app_spotify.png",
    "org.tizen.browser": "app_internet.png",
    "com.samsung.tv.csfs": "home.png",
}

SUPPORT = (
    MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.SELECT_SOURCE
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: TvReaderCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TvReaderMediaPlayer(coordinator)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "send_toast", {vol.Required("message"): cv.string}, "async_send_toast"
    )
    platform.async_register_entity_service(
        "tune_channel", {vol.Required("number"): cv.string}, "async_tune_channel"
    )
    platform.async_register_entity_service(
        "cast",
        {vol.Required("url"): cv.string, vol.Optional("kind"): cv.string,
         vol.Optional("duration"): cv.positive_int},
        "async_cast",
    )


class TvReaderMediaPlayer(CoordinatorEntity, MediaPlayerEntity):
    _attr_has_entity_name = True
    _attr_name = None  # the main device entity
    _attr_unique_id = "samsung_tv_media_player"
    _attr_supported_features = SUPPORT

    def __init__(self, coordinator: TvReaderCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, "samsung-tv")},
            "name": "Samsung TV",
            "manufacturer": "Samsung",
            "model": "UE55DU8572UXXH",
        }

    @property
    def _d(self) -> dict:
        return self.coordinator.data or {}

    @property
    def _agent(self) -> dict:
        return self._d.get("agent") or {}

    @property
    def _fg(self):
        fg = self._agent.get("foregroundApp")
        return fg if isinstance(fg, str) else None

    @property
    def _is_live_tv(self) -> bool:
        return self._fg == LIVE_TV_ID

    @property
    def state(self):
        # The TV's REST power is the accurate real-time source: 'on' vs 'standby'.
        # It reports 'standby' (not 'off') when the screen is off, and the agent keeps
        # running in standby - so ONLY 'on' counts as on (else the card's toggle stays
        # stuck and won't offer turn-on when the TV is actually off).
        return MediaPlayerState.ON if self._d.get("power") == "on" else MediaPlayerState.OFF

    @property
    def volume_level(self):
        v = (self._agent.get("audio") or {}).get("volume")
        return (v / 100.0) if isinstance(v, (int, float)) else None

    @property
    def is_volume_muted(self):
        return bool((self._agent.get("audio") or {}).get("mute"))

    @property
    def media_channel(self):
        # Channel row: only meaningful on live TV (stale/irrelevant inside an app).
        if not self._is_live_tv:
            return None
        ch = self._agent.get("channel") or {}
        name = (ch.get("name") or "").strip()
        num = ch.get("major")
        if name and num is not None:
            return f"{name} ({num})"
        return name or (str(num) if num is not None else None)

    @property
    def media_title(self):
        # "Now playing" / Program row. On live TV -> the current program (EPG). In an
        # app the App row carries the app name, so leave this blank there.
        if self._is_live_tv:
            prog = (self._agent.get("program") or {}).get("title")
            if prog:
                return prog
            ch = self._agent.get("channel") or {}
            return (ch.get("name") or "").strip() or None
        return None

    @property
    def media_content_type(self):
        return MediaType.CHANNEL if self._is_live_tv else None

    @property
    def _img_base(self):
        return self.coordinator._host.rstrip("/") + "/img/"

    @property
    def media_image_url(self):
        fg = self._fg
        if fg and fg in APP_IMG:
            return self._img_base + APP_IMG[fg]
        if self._is_live_tv:
            return self._img_base + "livetv.png"
        return self._img_base + "cat.png"

    @property
    def media_image_remotely_accessible(self):
        return True

    @property
    def source(self):
        # Reflect the current app (so the dropdown shows the active selection); fall
        # back to the physical input (TV/HDMI) on live TV / home.
        ra = self._d.get("running_app") or {}
        if ra.get("visible") and ra.get("name"):
            return ra.get("name")
        return (self._agent.get("input") or {}).get("type")

    @property
    def source_list(self):
        return ["Live TV"] + [name for name, _ in SOURCE_APPS]

    @property
    def app_id(self):
        return self._fg or (self._d.get("running_app") or {}).get("appId")

    @property
    def app_name(self):
        fg = self._fg
        if fg is None:
            return None
        if fg in FRIENDLY:
            return FRIENDLY[fg]  # Live TV / Home / None (our own agent overlay)
        for a in (self._agent.get("apps") or []):  # friendly name from the catalog
            if a.get("id") == fg:
                return a.get("name")
        return fg  # genuinely unknown -> raw id

    @property
    def extra_state_attributes(self):
        a = self._agent
        ch = a.get("channel") or {}
        return {
            "channel_name": (ch.get("name") or "").strip() or None,
            "channel_number": ch.get("major"),
            "program": (a.get("program") or {}).get("title"),
            "input": (a.get("input") or {}).get("type"),
            "foreground_app": a.get("foregroundApp"),
            "live_tv": self._is_live_tv,
            "agent_online": a.get("online"),
            "agent_last_seen": a.get("last_seen"),
        }

    # ---- commands ----
    async def async_set_volume_level(self, volume: float) -> None:
        await self.coordinator.control("set_volume", value=int(round(volume * 100)))

    async def async_volume_up(self) -> None:
        await self.coordinator.control("volume_up")

    async def async_volume_down(self) -> None:
        await self.coordinator.control("volume_down")

    async def async_mute_volume(self, mute: bool) -> None:
        if bool(self.is_volume_muted) != bool(mute):
            await self.coordinator.control("mute")

    async def async_turn_off(self) -> None:
        # Local, cloud-free: KEY_POWER over the TV's own remote-control WS (via the
        # bridge). No SmartThings in this path. The bridge answers
        # {sent, power, reason}; refresh afterwards so HA shows the state it caused
        # instead of the stale one it predicted.
        try:
            await self.coordinator.control("power_off")
        finally:
            await self.coordinator.async_request_refresh()

    async def async_turn_on(self) -> None:
        # Local: KEY_POWER over the TV's remote WS wakes it from network-standby
        # (verified twice on this set, 2026-10-23). No cloud: SmartThings is retired
        # and is not in this path. `power_delegate` is a legacy optional hook and is
        # unset on this install. No post-command recheck — that raced the local wake
        # and toggled the set back off.
        ok = False
        try:
            res = await self.coordinator.control("power_on")
            ok = isinstance(res, dict) and res.get("ok")
        except Exception:  # bridge timeout/unreachable -> treat as failed, fall back
            ok = False
        delegate = self.coordinator.power_delegate
        if not ok and delegate:
            await self.hass.services.async_call(
                "media_player", "turn_on", {"entity_id": delegate}, blocking=False
            )
        await self.coordinator.async_request_refresh()

    async def async_select_source(self, source: str) -> None:
        if source == "Live TV":
            await self.coordinator.control("launch_app", app_id=LIVE_TV_APP)
            return
        appid = next((aid for name, aid in SOURCE_APPS if name == source), None)
        if appid:
            await self.coordinator.control("launch_app", app_id=appid)
        else:
            _LOGGER.warning("tv_reader: unknown source %s", source)

    async def async_play_media(self, media_type: str, media_id: str, **kwargs) -> None:
        if media_type in (MediaType.CHANNEL, "channel"):
            await self.coordinator.control("tune_channel", number=str(media_id))
        else:
            _LOGGER.warning("tv_reader: unsupported play_media type %s", media_type)

    async def async_send_toast(self, message: str) -> None:
        await self.coordinator.notify(message)

    async def async_tune_channel(self, number: str) -> None:
        await self.coordinator.control("tune_channel", number=str(number))

    async def async_cast(self, url: str, kind: str = None, duration: int = 30) -> None:
        await self.coordinator.cast(url, kind=kind, duration=duration)
