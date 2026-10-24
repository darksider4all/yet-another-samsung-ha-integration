"""Regression tests for bridge runtime behaviour.

Covers defects found while wiring the TV app:
  * `_LAUNCH_MODE` was read before ever being defined, so the first
    GET /v1/launchmode raised NameError and dropped the connection;
  * queued commands had no age limit, so a notify queued while the TV was
    offline fired much later and stomped a fresh toast;
  * the access log is silent, so GET /v1/diag now exposes request counters.
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READER = os.path.join(REPO, "addon", "reader.py")
_ENV_KEYS = ("CONFIG_FILE", "TV_TOKEN_FILE", "IMG_DIR", "YAST_DATA_DIR", "DATA_DIR")
_counter = [0]


def load_reader(**env):
    # reader.py imports `websocket` at module level. CI pip-installs it, a dev box with
    # only stdlib does not have it, so every module-loading test died in exec_module
    # before this stub. Never talk to a real TV from a test.
    if "websocket" not in sys.modules:
        stub = types.ModuleType("websocket")

        def _no_ws(*_a, **_k):
            raise RuntimeError("websocket disabled in tests")

        stub.create_connection = _no_ws
        sys.modules["websocket"] = stub
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    os.environ.update({k: v for k, v in env.items() if v is not None})
    _counter[0] += 1
    name = f"yast_rt_{_counter[0]}"
    spec = importlib.util.spec_from_file_location(name, READER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mod = load_reader(
            CONFIG_FILE=os.path.join(self.tmp, "config.json"),
            IMG_DIR=os.path.join(self.tmp, "img"),
            TV_TOKEN_FILE=os.path.join(self.tmp, "tv_token"),
        )
        self.server = self.mod.ThreadingHTTPServer(("127.0.0.1", 0), self.mod.make_handler(self.mod.TVState()))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")

    def post(self, path, payload):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")


class LaunchModeTest(BridgeTest):
    def test_launch_mode_has_a_module_level_default(self):
        self.assertEqual(self.mod._LAUNCH_MODE, "foreground")

    def test_launchmode_get_no_longer_crashes(self):
        status, body = self.get("/v1/launchmode")
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "foreground")

    def test_launchmode_is_consumed_once(self):
        self.get("/v1/launchmode")  # first read returns "foreground"
        _, body = self.get("/v1/launchmode")
        self.assertEqual(body["mode"], "foreground", "handler resets to foreground after reading")


class CommandQueueTest(BridgeTest):
    def test_fresh_command_is_delivered(self):
        self.mod.enqueue_command({"action": "mute"})
        cmds = self.mod.take_commands()
        self.assertEqual(cmds, [{"action": "mute"}])

    def test_stale_command_is_dropped(self):
        self.mod.enqueue_command({"action": "notify", "message": "old"})
        # age the queued item beyond COMMAND_TTL
        with self.mod._cmd_cond:
            queued_at, cmd = self.mod._cmd_queue[0]
            self.mod._cmd_queue[0] = (queued_at - self.mod.COMMAND_TTL - 1, cmd)
        self.assertEqual(self.mod.take_commands(), [], "stale command must not be delivered")

    def test_queue_is_drained(self):
        self.mod.enqueue_command({"action": "mute"})
        self.mod.take_commands()
        self.assertEqual(self.mod.take_commands(), [])


class DiagTest(BridgeTest):
    def test_diag_counts_requests(self):
        self.get("/v1/ping")
        self.get("/v1/ping")
        _, body = self.get("/v1/diag")
        self.assertGreaterEqual(body["requests"]["/v1/ping"]["count"], 2)
        self.assertIn("agent_online", body)
        self.assertIn("toast_pending", body)
        self.assertIn("queued_commands", body)


class LaunchRoutingTest(BridgeTest):
    """launch_app must run bridge-side; a suspended app cannot launch itself."""

    def test_launch_app_is_not_an_agent_action(self):
        self.assertNotIn("launch_app", self.mod.AGENT_ACTIONS)

    def test_launch_app_is_a_server_side_control_action(self):
        self.assertIn("launch_app", self.mod.CONTROL_ACTIONS)

    def test_launch_app_calls_the_tv_rest_api(self):
        called = []
        self.mod.control_launch_app = lambda app_id: called.append(app_id) or True
        self.mod.control_close_app = lambda app_id: True
        status, body = self.post("/v1/control", {"action": "launch_app", "app_id": "ChanProbe0.ChannelProbe"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(called, ["ChanProbe0.ChannelProbe"])

    def test_launch_agent_closes_before_launching(self):
        order = []
        self.mod.control_close_app = lambda app_id: order.append(("close", app_id)) or True
        self.mod.app_state = lambda app_id: {"running": False}
        self.mod.control_launch_app = lambda app_id: order.append(("launch", app_id)) or True
        ok, err = self.mod.launch_agent()
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(order, [("close", self.mod.DEFAULT_AGENT_APP),
                                 ("launch", self.mod.DEFAULT_AGENT_APP)])

    def test_launch_agent_waits_for_the_close_to_land(self):
        """DELETE is async: launching too early gets the fresh instance killed."""
        order, states = [], [True, True, False]
        self.mod.control_close_app = lambda app_id: order.append("close") or True

        def state(app_id):
            return {"running": states.pop(0) if states else False}

        self.mod.app_state = state
        self.mod.control_launch_app = lambda app_id: order.append("launch") or True
        self.mod.CLOSE_WAIT = 2.0
        ok, _ = self.mod.launch_agent()
        self.assertTrue(ok)
        self.assertEqual(order, ["close", "launch"])

    def test_launch_agent_launches_even_if_the_close_never_lands(self):
        self.mod.control_close_app = lambda app_id: True
        self.mod.app_state = lambda app_id: {"running": True}
        self.mod.control_launch_app = lambda app_id: True
        self.mod.CLOSE_WAIT = 0.3
        start = time.time()
        ok, err = self.mod.launch_agent()
        self.assertTrue(ok, "the launch must not be skipped")
        self.assertIsNone(err)
        self.assertLess(time.time() - start, 2.0, "must respect CLOSE_WAIT, not hang")

    def test_launch_agent_survives_a_close_failure(self):
        def boom(app_id):
            raise RuntimeError("not running")

        self.mod.control_close_app = boom
        self.mod.app_state = lambda app_id: {}
        self.mod.control_launch_app = lambda app_id: True
        ok, err = self.mod.launch_agent()
        self.assertTrue(ok, "a failed close must not stop the launch")
        self.assertIsNone(err)


class NotifyLaunchesAgentTest(BridgeTest):
    def test_notify_launches_the_agent_and_does_not_queue(self):
        calls = []
        self.mod.control_launch_app = lambda app_id: calls.append(app_id) or True
        status, body = self.post("/v1/notify", {"message": "hello"})
        self.assertEqual(status, 200)
        self.assertTrue(body["launched"], "notify must launch the hidden agent")
        self.assertFalse(body["queued"], "a launch makes the queued notify redundant")
        self.assertEqual(calls, [self.mod.DEFAULT_AGENT_APP])
        self.assertEqual(self.mod.take_commands(), [], "nothing may be left queued")

    def test_notify_falls_back_to_the_queue_when_the_launch_fails(self):
        def boom(app_id):
            raise RuntimeError("tv unreachable")

        self.mod.control_launch_app = boom
        status, body = self.post("/v1/notify", {"message": "hello"})
        self.assertEqual(status, 200)
        self.assertFalse(body["launched"])
        self.assertTrue(body["queued"])
        self.assertIn("tv unreachable", body["launch_error"])
        cmds = self.mod.take_commands()
        self.assertEqual([c["action"] for c in cmds], ["notify"])

    def test_notify_still_stores_the_toast(self):
        self.mod.control_launch_app = lambda app_id: True
        self.post("/v1/notify", {"message": "hey", "duration": 12})
        _, diag = self.get("/v1/diag")
        self.assertTrue(diag["toast_pending"])


class PowerOnHttpTest(BridgeTest):
    """Over HTTP: a turn_on the bridge refuses must be a refusal, not a 200 that HA
    reads as success. power_on used to answer `ok: true` for every call."""

    def _stub_power(self, state):
        """power_on reads the live REST state now, so each test states it explicitly."""
        self.mod._read_power_now = lambda: state
        self.mod._arm_verify = lambda *a, **k: None
        self.mod._wake_on_lan = lambda *a, **k: False

    def test_unknown_power_is_refused_with_409(self):
        from urllib.error import HTTPError
        self._stub_power(None)

        with self.assertRaises(HTTPError) as ctx:
            self.post("/v1/control", {"action": "power_on"})
        self.assertEqual(ctx.exception.code, 409)
        body = json.loads(ctx.exception.read().decode() or "{}")
        self.assertEqual(body["reason"], "power_unknown")

    def test_already_on_is_200_and_sends_no_key(self):
        self._stub_power("on")
        status, body = self.post("/v1/control", {"action": "power_on"})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"], "nothing was sent, so ok must not claim it was")
        self.assertEqual(body["reason"], "already_on")

    def test_a_key_that_really_went_out_is_ok_true(self):
        """The success path, end to end: standby + a real send must read as success.
        Both HTTP tests above cover refusals only, so 'ok' could be False for ever."""
        self.mod.TV_TOKEN = "token"
        self._stub_power("standby")
        self.mod._send_remote_key = lambda key: True
        status, body = self.post("/v1/control", {"action": "power_on"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["sent"])
        self.assertIsNone(body["reason"])

    def test_a_refused_key_is_409_not_a_claimed_wake(self):
        """_send_remote_key's ms.error detection had no consumer in the power path."""
        self.mod.TV_TOKEN = "token"
        self.mod._POWER_CACHE = "standby"
        self._stub_power("standby")
        self.mod._send_remote_key = lambda key: False
        from urllib.error import HTTPError

        with self.assertRaises(HTTPError) as ctx:
            self.post("/v1/control", {"action": "power_on"})
        self.assertEqual(ctx.exception.code, 409)
        body = json.loads(ctx.exception.read().decode() or "{}")
        self.assertFalse(body["ok"])
        self.assertEqual(body["reason"], "send_failed")
        self.assertEqual(self.mod._POWER_CACHE, "standby", "a refused key changed no state")


if __name__ == "__main__":
    unittest.main()
