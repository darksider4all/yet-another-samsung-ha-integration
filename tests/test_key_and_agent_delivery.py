"""Regression tests for "the card said ok and the TV did nothing".

Measured on the living-room DU85 (Tizen 9, TIZEN-TRUNK2025, T-...0090-2123.9):
  * Live TV: the tuner (org.tizen.tv-viewer) is a system app, absent from the REST
    app catalogue (POST applications/<id> -> 404) and absent from the on-TV agent's
    app list, so source "Live TV" could never launch. `launch_app
    org.tizen.tv-viewer` does work from the TV's own Tizen runtime (proven over
    root/sdb), which is what the on-TV agent calls.
  * Agent actions: Tizen suspends a hidden web app's timers, so a backgrounded agent
    stops long-polling /v1/commands. Commands queued, expired and were still
    answered {"ok": true} — volume/mute dead whenever an app was playing.
  * send_key: the TV never confirms a keypress, and on this firmware v2 silently
    ignores keys while v3 answers ms.error, so a bare {"ok": true} claimed an action
    that never happened.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_bridge_runtime import BridgeTest  # noqa: E402  (load_reader + server fixture)


class AgentPickupTest(BridgeTest):
    """A queued command must be proven taken, not assumed."""

    def setUp(self):
        super().setUp()
        self.mod.AGENT_PICKUP_WAIT = 0.3
        self.launches = []
        self.mod.control_close_app = lambda app_id: True
        self.mod.app_state = lambda app_id: {"running": False}
        self.mod.control_launch_app = (
            lambda app_id: self.launches.append(app_id) or True)
        # the bridge pokes HA after state-changing commands; no HA in tests
        self.mod._poke_ha_state = lambda delay=0.0: None

    def _poller(self, delay):
        def run():
            time.sleep(delay)
            self.mod.take_commands()
        threading.Thread(target=run, daemon=True).start()

    def test_a_polling_agent_counts_as_delivered(self):
        self._poller(0.05)
        ok, err = self.mod.run_agent_command({"action": "mute", "value": True})
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(self.launches, [], "a polling agent must not be relaunched")

    def test_a_suspended_agent_is_woken_and_the_command_survives(self):
        """launch_agent reloads the page; the queued command must still be there."""
        def launch(app_id):
            self.launches.append(app_id)
            self._poller(0.05)  # the freshly booted instance starts polling
            return True

        self.mod.control_launch_app = launch
        ok, err = self.mod.run_agent_command({"action": "volume_up"})
        self.assertTrue(ok, err)
        self.assertEqual(self.launches, [self.mod.DEFAULT_AGENT_APP])

    def test_the_wake_is_silent(self):
        """Waking the agent to run a volume step must not paint the dashboard."""
        seen = []
        self.mod.control_launch_app = lambda app_id: seen.append(self.mod._LAUNCH_MODE) or True
        self.mod.run_agent_command({"action": "volume_up"})
        self.assertEqual(seen, ["background"])

    def test_an_undeliverable_command_is_reported_and_dropped(self):
        ok, err = self.mod.run_agent_command({"action": "mute", "value": True})
        self.assertFalse(ok)
        self.assertIn("did not pick up", err)
        self.assertEqual(self.mod.take_commands(), [],
                         "an undelivered command must not fire minutes later")

    def test_a_failed_wake_is_reported(self):
        def boom(_app_id):
            raise RuntimeError("tv unreachable")

        self.mod.control_launch_app = boom
        ok, err = self.mod.run_agent_command({"action": "mute", "value": True})
        self.assertFalse(ok)
        self.assertIn("could not be woken", err)

    def test_the_control_endpoint_stops_claiming_ok(self):
        status, body = self.post("/v1/control", {"action": "mute", "value": True})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"], "an undelivered agent action is not ok")
        self.assertIn("did not pick up", body["error"])


class LiveTvRoutingTest(BridgeTest):
    """Live TV is the tuner: not in the REST catalogue, launchable only by Tizen."""

    def setUp(self):
        super().setUp()
        self.mod.AGENT_PICKUP_WAIT = 0.05

    def test_live_tv_constant_is_the_tuner_app(self):
        self.assertEqual(self.mod.LIVE_TV_APP, "org.tizen.tv-viewer")

    def test_a_404_hands_the_launch_to_the_agent(self):
        sent = []

        def rest_404(path, method="GET"):
            err = RuntimeError("HTTP Error 404: Not Found")
            err.code = 404
            raise err

        self.mod._rest_request = rest_404
        self.mod.run_agent_command = lambda cmd: sent.append(cmd) or (True, None)
        self.assertTrue(self.mod.control_launch_app(self.mod.LIVE_TV_APP))
        self.assertEqual(sent, [{"action": "launch_app",
                                 "app_id": self.mod.LIVE_TV_APP}])

    def test_a_real_rest_failure_is_not_masked(self):
        def rest_500(path, method="GET"):
            err = RuntimeError("HTTP Error 500")
            err.code = 500
            raise err

        self.mod._rest_request = rest_500
        with self.assertRaises(RuntimeError):
            self.mod.control_launch_app("3201907018807")

    def test_an_installed_app_never_reaches_the_agent(self):
        calls = []
        self.mod._rest_request = lambda path, method="GET": calls.append((path, method)) or "{}"
        self.mod.run_agent_command = lambda cmd: (_ for _ in ()).throw(AssertionError("no agent"))
        self.assertTrue(self.mod.control_launch_app("3201907018807"))
        self.assertEqual(calls, [("applications/3201907018807", "POST")])

    def test_the_agent_refusal_surfaces_to_the_caller(self):
        def rest_404(path, method="GET"):
            err = RuntimeError("HTTP Error 404: Not Found")
            err.code = 404
            raise err

        self.mod._rest_request = rest_404
        self.mod.run_agent_command = lambda cmd: (False, "agent did not pick up the command")
        with self.assertRaises(RuntimeError) as ctx:
            self.mod.control_launch_app(self.mod.LIVE_TV_APP)
        self.assertIn("did not pick up", str(ctx.exception))


class SendKeyHonestyTest(BridgeTest):
    def test_a_sent_key_is_reported_as_unverified(self):
        self.mod._send_remote_key = lambda key: True
        status, body = self.post("/v1/control", {"action": "send_key", "key": "KEY_TV"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["verified"], "the TV never confirms a keypress")

    def test_a_refused_key_is_not_ok(self):
        self.mod._send_remote_key = lambda key: False
        status, body = self.post("/v1/control", {"action": "send_key", "key": "KEY_TV"})
        self.assertFalse(body["ok"])


if __name__ == "__main__":
    import unittest
    unittest.main()
