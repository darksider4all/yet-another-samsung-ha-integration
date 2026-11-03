"""The source menu must come from the catalogue the bridge really reports.

Regression: three of six hardcoded ids (org.tizen.netflix-app,
com.samsung.tv.cobalt-yt, org.tizen.primevideo) 404'd on this set, so every app
switch from the card failed with a 500. The menu is now derived from
GET /v1/state -> installed_apps, which is launchable by definition.
"""
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPS = os.path.join(REPO, "custom_components", "tv_reader", "apps.py")

# Captured verbatim from the TV (UE55DU8572UXXH) via the bridge.
CATALOGUE = [
    {"appId": "111299001912", "name": "YouTube", "running": False, "visible": False},
    {"appId": "3201907018807", "name": "Netflix", "running": True, "visible": False},
    {"appId": "3201910019365", "name": "Prime Video", "running": False, "visible": False},
    {"appId": "3201512006963", "name": "Plex", "running": False, "visible": False},
    {"appId": "3201606009684", "name": "Spotify - Music and Podcasts", "running": False, "visible": False},
    {"appId": "3201901017640", "name": "Disney+", "running": False, "visible": False},
]
DEAD_IDS = {"org.tizen.netflix-app", "com.samsung.tv.cobalt-yt", "org.tizen.primevideo"}


def load_apps():
    spec = importlib.util.spec_from_file_location("tv_reader_apps", APPS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SourceMenuTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_apps()

    def test_every_menu_entry_is_in_the_catalogue(self):
        entries = self.mod.source_entries(CATALOGUE)
        ids = [app_id for _, app_id in entries]
        self.assertEqual(ids, [a["appId"] for a in CATALOGUE])
        self.assertFalse(DEAD_IDS & set(ids))

    def test_labels_are_friendly(self):
        names = dict((app_id, name) for name, app_id in self.mod.source_entries(CATALOGUE))
        self.assertEqual(names["3201606009684"], "Spotify")
        self.assertEqual(names["3201907018807"], "Netflix")

    def test_artwork_covers_the_catalogue(self):
        for a in CATALOGUE:
            self.assertIn(a["appId"], self.mod.APP_IMG)

    def test_dangling_and_empty_payloads(self):
        self.assertEqual(self.mod.source_entries(None), [])
        self.assertEqual(self.mod.source_entries([]), [])
        self.assertEqual(self.mod.source_entries([{"name": "no id"}]), [])
        # unknown app still launchable, labelled by its id
        self.assertEqual(self.mod.source_entries([{"appId": "x.y"}]), [("x.y", "x.y")])


if __name__ == "__main__":
    unittest.main()
