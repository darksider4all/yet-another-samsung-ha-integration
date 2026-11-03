"""The source dropdown must only offer apps this TV will actually launch.

Regression (2026-11-03): SOURCE_APPS hardcoded six app ids, three of which
(org.tizen.netflix-app, com.samsung.tv.cobalt-yt, org.tizen.primevideo) answer 404 on
POST /api/v2/applications/<id> on UE55DU8572UXXH. Selecting Netflix therefore failed
with "bridge refused launch_app (HTTP 500): HTTP Error 404: Not Found".

The fix resolves names against the catalogue the TV reports (bridge /v1/state ->
installed_apps[]), so the assertion here is the invariant that matters: every source
offered must resolve to an id that the catalogue contains.

The HA stubs come from test_media_player_volume so both suites share one import shim.
"""
import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS = pathlib.Path(__file__).resolve().parent


def _load_volume_module():
    spec = importlib.util.spec_from_file_location(
        "test_media_player_volume", TESTS / "test_media_player_volume.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_vol = _load_volume_module()
_load_media_player = _vol._load_media_player
_Coordinator = _vol._Coordinator

# What the bridge reports for UE55DU8572UXXH: the launchable ids, not catalogue ids.
INSTALLED = [
    {"appId": "111299001912", "name": "YouTube", "running": False, "visible": False},
    {"appId": "3201907018807", "name": "Netflix", "running": False, "visible": False},
    {"appId": "3201910019365", "name": "Prime Video", "running": False, "visible": False},
    {"appId": "3201512006963", "name": "Plex", "running": False, "visible": False},
    {"appId": "3201606009684", "name": "Spotify - Music and Podcasts"},
    {"appId": "3201901017640", "name": "Disney+"},
]

# The agent's catalogue carries ids that look right and cannot be launched.
AGENT_APPS = [
    {"id": "org.tizen.netflix-app", "name": "Netflix"},
    {"id": "com.samsung.tv.cobalt-yt", "name": "YouTube"},
]


class SourceResolution(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mod = _load_media_player()

    def _entity(self, installed=None, agent_apps=None):
        e = object.__new__(self.mod.TvReaderMediaPlayer)
        e.coordinator = _Coordinator()
        e.coordinator.data = {
            "installed_apps": INSTALLED if installed is None else installed,
            "agent": {"apps": AGENT_APPS if agent_apps is None else agent_apps},
        }
        return e

    def test_launchable_ids_win_over_catalogue_ids(self):
        e = self._entity()
        self.assertEqual(e._source_apps()["Netflix"], "3201907018807")
        self.assertEqual(e._source_apps()["YouTube"], "111299001912")

    async def test_select_source_launches_the_launchable_id(self):
        e = self._entity()
        await e.async_select_source("Netflix")
        self.assertEqual(
            e.coordinator.calls, [{"action": "launch_app", "app_id": "3201907018807"}]
        )

    async def test_every_offered_source_resolves_to_a_catalogued_id(self):
        """The invariant: nothing in source_list may point at an id we can't launch."""
        e = self._entity()
        apps = e._source_apps()
        catalogue = {a["appId"] for a in INSTALLED}
        for name in e.source_list:
            if name == "Live TV":
                continue
            self.assertIn(apps[name], catalogue, f"{name} points outside the catalogue")

    def test_curated_order_is_kept(self):
        e = self._entity()
        self.assertEqual(
            e.source_list,
            ["Live TV", "Netflix", "YouTube", "Prime Video", "Disney+", "Plex", "Spotify"],
        )

    def test_tv_label_is_normalised(self):
        e = self._entity()
        self.assertIn("Spotify", e.source_list)
        self.assertNotIn("Spotify - Music and Podcasts", e.source_list)

    def test_no_hardcoded_id_table_remains(self):
        """SOURCE_APPS is what rotted; it must not come back."""
        self.assertFalse(hasattr(self.mod, "SOURCE_APPS"))

    async def test_agent_only_name_falls_back_but_curated(self):
        """With no bridge list, only shortlisted names are offered, never 200 apps."""
        e = self._entity(installed=[], agent_apps=AGENT_APPS)
        self.assertEqual(e.source_list, ["Live TV", "Netflix", "YouTube"])

    async def test_unknown_source_is_a_no_op_not_a_crash(self):
        e = self._entity()
        await e.async_select_source("MySpace")
        self.assertEqual(e.coordinator.calls, [])

    def test_every_offered_source_has_artwork(self):
        """APP_IMG is keyed by the ids the TV really reports, so cards don't fall back
        to the cat. A curated name whose id has no artwork is a silent regression."""
        e = self._entity()
        apps = e._source_apps()
        for name in e.source_list:
            if name == "Live TV":
                continue
            self.assertIn(apps[name], self.mod.APP_IMG, f"{name} has no artwork")


if __name__ == "__main__":
    unittest.main()
