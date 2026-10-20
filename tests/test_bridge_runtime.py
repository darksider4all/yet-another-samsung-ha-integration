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
import unittest
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READER = os.path.join(REPO, "addon", "reader.py")
_ENV_KEYS = ("CONFIG_FILE", "TV_TOKEN_FILE", "IMG_DIR", "YAST_DATA_DIR", "DATA_DIR")
_counter = [0]


def load_reader(**env):
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


if __name__ == "__main__":
    unittest.main()
