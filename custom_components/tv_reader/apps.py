"""Launchable app catalogue -> source menu.

No HA imports: the menu is derived from what the bridge actually reports, so it
can be tested without Home Assistant installed.

The TV's REST catalogue (bridge GET /v1/state -> installed_apps) is the only
authority on which app ids can be launched. A hardcoded table rots: on this set
three of six ids (`org.tizen.netflix-app`, `com.samsung.tv.cobalt-yt`,
`org.tizen.primevideo`) 404'd, which is why every app switch from the card failed.
"""
from __future__ import annotations

LIVE_TV = "Live TV"

# appId -> shorter dropdown label; catalogue names can be long.
SHORT_NAMES = {"3201606009684": "Spotify"}

# Artwork, served by the bridge at <host>/img/<file>. Keyed by foreground appId;
# every id the catalogue reports must be here, or the card falls back to cat.png.
APP_IMG = {
    "111299001912": "app_youtube.png",
    "9Ur5IzDKqV.TizenYouTube": "app_youtube.png",
    "3201907018807": "app_netflix.png",
    "3201910019365": "app_prime.png",
    "3201901017640": "app_disney.png",
    "3201512006963": "app_plex.png",
    "3201606009684": "app_spotify.png",
    "org.tizen.browser": "app_internet.png",
    "com.samsung.tv.csfs": "home.png",
}


def source_entries(installed_apps) -> list[tuple[str, str]]:
    """(label, appId) for every app the bridge lists as installed/launchable."""
    out: list[tuple[str, str]] = []
    for app in installed_apps or []:
        app_id = (app or {}).get("appId")
        if not app_id:
            continue
        out.append((SHORT_NAMES.get(app_id) or app.get("name") or app_id, app_id))
    return out
