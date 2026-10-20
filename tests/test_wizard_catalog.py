"""Setup-wizard entity catalog tests.

The wizard used to ask HA only for light.* and switch.* entities, so the TV
dashboard could never hold anything else however the app classified tiles.
The picker now offers every tileable entity, grouped by the same category the
app renders with, and saves the choice as `main_entities`.
"""
import unittest

from test_bridge_runtime import load_reader


def st(entity_id, state, **attributes):
    return {"entity_id": entity_id, "state": state, "attributes": attributes}


class WizardCatalogTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_reader()
        self.states = [
            st("light.sofa", "on", friendly_name="Sofa", brightness=255,
               supported_color_modes=["color_temp"]),
            st("switch.plug", "on", friendly_name="Plug", device_class="outlet"),
            st("cover.garage", "closed", friendly_name="Garage", device_class="garage"),
            st("climate.thermostat", "heat", friendly_name="Thermostat"),
            st("sensor.living_temp", "22.0", friendly_name="Living temp",
               device_class="temperature", unit_of_measurement="°C"),
            st("scene.away_mode", "scening", friendly_name="Away"),
            # bookkeeping domains must never clutter the picker
            st("automation.doorbell", "on", friendly_name="Doorbell automation"),
            st("person.adrian", "home", friendly_name="Adrian"),
            st("update.hacs", "on", friendly_name="HACS update"),
            st("device_tracker.phone", "home", friendly_name="Phone"),
            st("sun.sun", "above_horizon"),
        ]
        self.mod._ha_request = lambda *a, **k: self.states

    def ids(self):
        return [e["entity_id"] for e in self.mod.ha_entities()]

    def test_catalog_is_not_limited_to_lights_and_switches(self):
        ids = self.ids()
        for eid in ("cover.garage", "climate.thermostat", "sensor.living_temp",
                    "scene.away_mode"):
            self.assertIn(eid, ids)

    def test_bookkeeping_domains_are_excluded(self):
        ids = self.ids()
        for eid in ("automation.doorbell", "person.adrian", "update.hacs",
                    "device_tracker.phone", "sun.sun"):
            self.assertNotIn(eid, ids)

    def test_each_entry_carries_its_rendered_category(self):
        by_id = {e["entity_id"]: e for e in self.mod.ha_entities()}
        self.assertEqual(by_id["light.sofa"]["category"], "light")
        self.assertEqual(by_id["switch.plug"]["category"], "outlet")
        self.assertEqual(by_id["cover.garage"]["category"], "garage_door")
        self.assertEqual(by_id["climate.thermostat"]["category"], "climate")
        self.assertEqual(by_id["sensor.living_temp"]["category"], "temperature")

    def test_names_fall_back_to_entity_id(self):
        by_id = {e["entity_id"]: e for e in self.mod.ha_entities()}
        self.assertEqual(by_id["light.sofa"]["name"], "Sofa")
        self.assertEqual(by_id["scene.away_mode"]["name"], "Away")

    def test_sorted_by_category_then_name(self):
        rows = self.mod.ha_entities()
        keys = [(r["category"], r["name"].lower()) for r in rows]
        self.assertEqual(keys, sorted(keys))

    def test_empty_backend_response_is_survivable(self):
        self.mod._ha_request = lambda *a, **k: None
        self.assertEqual(self.mod.ha_entities(), [])


if __name__ == "__main__":
    unittest.main()
