"""Entity classification tests.

A tile's control type must be derived from the entity itself — domain, then
device_class, then supported_features — the way HomeKit picks an accessory
type, so any entity the user selects renders with the right control.
"""
import unittest

from test_bridge_runtime import load_reader


def st(entity_id, state, **attributes):
    return {"entity_id": entity_id, "state": state, "attributes": attributes}


class CategoryTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_reader()

    def test_light_category_and_capabilities(self):
        e = st("light.sofa", "on", brightness=128,
               supported_color_modes=["color_temp"], supported_features=0)
        rec = self.mod.entity_record(e)
        self.assertEqual(rec["category"], "light")
        self.assertTrue(rec["on"])
        self.assertEqual(rec["brightness"], 50)
        self.assertIn("toggle", rec["capabilities"])
        self.assertIn("dim", rec["capabilities"])
        self.assertIn("color_temp", rec["capabilities"])

    def test_rgb_light_gets_colour(self):
        e = st("light.strip", "on", supported_color_modes=["hs", "color_temp"])
        self.assertIn("color", self.mod.entity_capabilities(e))

    def test_switch_outlet_gets_its_own_type(self):
        e = st("switch.plug", "on", device_class="outlet")
        self.assertEqual(self.mod.entity_category(e), "outlet")

    def test_cover_garage_and_position(self):
        e = st("cover.garage", "open", device_class="garage", supported_features=7)
        rec = self.mod.entity_record(e)
        self.assertEqual(rec["category"], "garage_door")
        self.assertIn("position", rec["capabilities"])
        self.assertTrue(rec["on"], "an open cover counts as active")

    def test_binary_sensor_motion_is_not_togglable(self):
        e = st("binary_sensor.hall_motion", "on", device_class="motion")
        rec = self.mod.entity_record(e)
        self.assertEqual(rec["category"], "motion")
        self.assertEqual(rec["capabilities"], [])
        self.assertTrue(rec["on"])

    def test_temperature_sensor_carries_value_and_unit(self):
        e = st("sensor.living_temp", "21.5", device_class="temperature",
               unit_of_measurement="°C")
        rec = self.mod.entity_record(e)
        self.assertEqual(rec["category"], "temperature")
        self.assertEqual(rec["value"], 21.5)
        self.assertEqual(rec["unit"], "°C")
        self.assertNotIn("toggle", rec["capabilities"])

    def test_climate_thermostat(self):
        e = st("climate.thermostat", "heat", supported_features=385)
        rec = self.mod.entity_record(e)
        self.assertEqual(rec["category"], "climate")
        self.assertIn("temperature", rec["capabilities"])
        self.assertTrue(rec["on"], "heating counts as active")

    def test_unavailable_entity_is_flagged(self):
        rec = self.mod.entity_record(st("light.gone", "unavailable"))
        self.assertFalse(rec["available"])
        self.assertFalse(rec["reachable"])

    def test_unknown_domain_falls_back_to_the_domain(self):
        self.assertEqual(self.mod.entity_category(st("thing.one", "on")), "thing")

    def test_scene_and_script_are_togglable(self):
        for eid in ("scene.movie", "script.night_mode"):
            self.assertIn("toggle", self.mod.entity_capabilities(st(eid, "on")))


class EntityViewTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_reader()
        self.states = [
            st("light.sofa", "on", brightness=255, supported_color_modes=["color_temp"]),
            st("sensor.living_temp", "22.0", device_class="temperature", unit_of_measurement="°C"),
            st("cover.garage", "closed", device_class="garage", supported_features=4),
        ]
        self.mod._ha_request = lambda *a, **k: self.states

    def test_main_entities_wins_over_main_lights(self):
        self.mod._CONFIG["main_lights"] = [{"entity_id": "light.sofa"}]
        self.mod._CONFIG["main_entities"] = [
            {"entity_id": "light.sofa"},
            {"entity_id": "sensor.living_temp"},
            {"entity_id": "cover.garage"},
        ]
        view = self.mod.ha_entity_view()
        self.assertEqual([r["category"] for r in view], ["light", "temperature", "garage_door"])

    def test_category_filter(self):
        self.mod._CONFIG["main_entities"] = [
            {"entity_id": "light.sofa"}, {"entity_id": "sensor.living_temp"},
        ]
        view = self.mod.ha_entity_view("light")
        self.assertEqual([r["entity_id"] for r in view], ["light.sofa"])

    def test_missing_entity_still_yields_a_tile(self):
        self.mod._CONFIG["main_entities"] = [{"entity_id": "light.not_in_ha", "name": "Ghost"}]
        view = self.mod.ha_entity_view()
        self.assertEqual(view[0]["name"], "Ghost")
        self.assertFalse(view[0]["available"])

    def test_legacy_lights_view_keeps_old_keys(self):
        self.mod._CONFIG["main_lights"] = [{"entity_id": "light.sofa", "name": "Sofa"}]
        view = self.mod.ha_lights()
        for key in ("entity_id", "name", "state", "on", "brightness", "reachable"):
            self.assertIn(key, view[0])


if __name__ == "__main__":
    unittest.main()
