"""control_power_off must report what actually happened.

Regression: it returned a bare True on a cache miss, so a power-off that never left the
bridge looked identical to a successful one — HA reported success while the TV stayed on.
"""
import importlib.util
import os
import pathlib
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_reader():
    """Import the bridge module without needing websocket-client or a real config."""
    if "websocket" not in sys.modules:
        stub = types.ModuleType("websocket")

        def _no_ws(*_a, **_k):
            raise RuntimeError("websocket disabled in tests")

        stub.create_connection = _no_ws
        sys.modules["websocket"] = stub
    tmp = tempfile.mkdtemp()
    for key in ("CONFIG_FILE", "TV_TOKEN_FILE", "IMG_DIR", "YAST_DATA_DIR", "DATA_DIR"):
        os.environ.setdefault(key, os.path.join(tmp, key.lower()))
    spec = importlib.util.spec_from_file_location(
        "bridge_reader_power", ROOT / "server/bridge/reader.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load server/bridge/reader.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class PowerOffContract(unittest.TestCase):
    def setUp(self):
        self.m = _load_reader()
        self.sent = []
        # never touch a real TV from a test
        self.m._send_remote_key = lambda key: (self.sent.append(key), True)[1]
        self.m.TV_TOKEN = "token"

    def test_sends_when_on(self):
        self.m._POWER_CACHE = "on"
        self.assertEqual(
            self.m.control_power_off(), {"sent": True, "power": "on", "reason": None}
        )
        self.assertEqual(self.sent, ["KEY_POWER"])

    def test_already_off_never_toggles(self):
        self.m._POWER_CACHE = "standby"
        result = self.m.control_power_off()
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "already_off")
        self.assertEqual(self.sent, [], "KEY_POWER is a toggle: never send it again")

    def test_unknown_power_refused(self):
        self.m._POWER_CACHE = None
        result = self.m.control_power_off()
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "power_unknown")
        self.assertEqual(self.sent, [], "guessing could switch the TV ON")

    def test_missing_token_bootstraps_pairing(self):
        """No token is not a refusal any more: the key path connects token-less so the
        TV can ask once, then saves what it hands back. (The old 'no_remote_token'
        branch was dead code and its test was red on main.)"""
        self.m._POWER_CACHE = "on"
        self.m.TV_TOKEN = ""
        result = self.m.control_power_off()
        self.assertTrue(result["sent"])
        self.assertEqual(self.sent, ["KEY_POWER"])

    def test_deliberate_standby_survives_a_poller_write(self):
        """Review item 3: a poller inference must never undo a commanded power state.

        Without this, the next poll snapped the cache back to 'on' and a second
        turn_off re-fired the blind toggle and switched the TV ON.
        """
        self.m._POWER_CACHE = "on"
        self.m.control_power_off()
        self.assertEqual(self.m._POWER_CACHE, "standby")
        self.m._note_power("on")            # what get_power_state() used to do
        self.assertEqual(self.m._POWER_CACHE, "standby", "poller clobbered a command")

    def test_remote_channel_wire_format(self):
        """Pin port and path: the 1005 red herring was a wrong-port attempt (:8009)."""
        self.m.CLIENT_NAME = "yast-bridge"
        self.m._cfg = lambda k, d=None: "192.168.0.154" if k == "tv_ip" else d
        url = self.m._remote_url("abc123")
        self.assertTrue(url.startswith("wss://192.168.0.154:8002/"), url)
        self.assertIn("/api/v2/channels/samsung.remote.control?", url)
        self.assertIn("token=abc123", url)


class ConnectBudget(unittest.TestCase):
    """A retry loop must not be able to turn itself into a dialog storm."""

    def setUp(self):
        self.m = _load_reader()
        self.m._CONNECT_TIMES[:] = []

    def test_budget_stops_a_storm(self):
        self.m.CONNECT_BUDGET = 3
        for _ in range(3):
            self.m._connect_allowed()
        with self.assertRaises(RuntimeError):
            self.m._connect_allowed()

    def test_budget_window_expires(self):
        self.m.CONNECT_BUDGET = 1
        self.m._connect_allowed()
        self.m._CONNECT_TIMES[:] = [0.0]        # long ago
        self.m._connect_allowed()               # must not raise


if __name__ == "__main__":
    unittest.main()
