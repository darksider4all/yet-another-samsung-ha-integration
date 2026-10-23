"""Regression tests for bridge configuration persistence.

Background: the add-on used to write its config to ``/app/data/config.json`` — a
path *inside* the container image. It worked until the add-on was updated, at
which point the container was recreated and the wizard config, the HA token and
the TV pairing token vanished. These tests pin the rules that stop that from
happening again:

* the default storage paths are on a persistent volume (never /app/...),
* a saved config survives a restart (fresh import of the module),
* a save that cannot reach disk is reported as a failure, not as success,
* the two copies of ``reader.py`` (add-on and standalone bridge) stay identical.
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON_READER = os.path.join(REPO_ROOT, "addon", "reader.py")
BRIDGE_READER = os.path.join(REPO_ROOT, "server", "bridge", "reader.py")

_ENV_KEYS = ("CONFIG_FILE", "TV_TOKEN_FILE", "IMG_DIR", "YAST_DATA_DIR", "DATA_DIR")


def load_reader(path=ADDON_READER, **env):
    """Import a fresh, independent copy of reader.py with the given environment."""
    # reader.py imports `websocket` at module level; CI installs it, a stdlib-only box
    # does not, and then every test here dies in exec_module. Never touch a real TV.
    if "websocket" not in sys.modules:
        stub = types.ModuleType("websocket")

        def _no_ws(*_a, **_k):
            raise RuntimeError("websocket disabled in tests")

        stub.create_connection = _no_ws
        sys.modules["websocket"] = stub
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    os.environ.update(env)
    name = "yast_reader_%d" % len(sys.modules)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PersistentPathsTest(unittest.TestCase):
    def test_defaults_are_not_inside_the_image(self):
        reader = load_reader()
        self.assertEqual(reader.CONFIG_FILE, "/data/config.json")
        self.assertEqual(reader.TOKEN_FILE, "/data/tv_token")
        self.assertEqual(reader.IMG_DIR, "/data/img")
        for path in (reader.CONFIG_FILE, reader.TOKEN_FILE, reader.IMG_DIR):
            self.assertFalse(path.startswith("/app/"), "%s is wiped on update" % path)

    def test_data_dir_env_moves_all_paths(self):
        reader = load_reader(DATA_DIR="/srv/yast")
        self.assertEqual(reader.CONFIG_FILE, "/srv/yast/config.json")
        self.assertEqual(reader.TOKEN_FILE, "/srv/yast/tv_token")
        self.assertEqual(reader.IMG_DIR, "/srv/yast/img")


class SaveConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yast-test-")
        self.config = os.path.join(self.tmp, "config.json")
        self.env = {
            "CONFIG_FILE": self.config,
            "TV_TOKEN_FILE": os.path.join(self.tmp, "tv_token"),
            "IMG_DIR": os.path.join(self.tmp, "img"),
        }

    def test_config_survives_a_restart(self):
        reader = load_reader(**self.env)
        reader._CONFIG.update({"tv_ip": "10.0.0.9", "notify_seconds": 7})
        self.assertTrue(reader._save_config())

        with open(self.config) as handle:
            self.assertEqual(json.load(handle)["tv_ip"], "10.0.0.9")

        restarted = load_reader(**self.env)
        self.assertEqual(restarted._CONFIG["tv_ip"], "10.0.0.9")
        self.assertEqual(restarted._CONFIG["notify_seconds"], 7)

    def test_save_failure_is_reported(self):
        reader = load_reader(
            CONFIG_FILE="/proc/yast-test/config.json",
            TV_TOKEN_FILE=os.path.join(self.tmp, "tv_token"),
            IMG_DIR=os.path.join(self.tmp, "img"),
        )
        self.assertFalse(reader._save_config())
        self.assertIsNotNone(reader._LAST_SAVE_ERROR)


class HttpConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yast-http-")
        self.config = os.path.join(self.tmp, "config.json")
        self.env = {
            "CONFIG_FILE": self.config,
            "TV_TOKEN_FILE": os.path.join(self.tmp, "tv_token"),
            "IMG_DIR": os.path.join(self.tmp, "img"),
        }
        self.reader = load_reader(**self.env)
        self.server = self.reader.ThreadingHTTPServer(
            ("127.0.0.1", 0), self.reader.make_handler(self.reader.TVState())
        )
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return json.loads(response.read())

    def _post(self, path, payload):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def test_post_persists_and_reports_where(self):
        result = self._post("/v1/config", {"tv_ip": "10.0.0.42", "notify_seconds": 5})
        self.assertTrue(result["persisted"])
        self.assertEqual(result["config_file"], self.config)

        state = self._get("/v1/config")
        self.assertEqual(state["tv_ip"], "10.0.0.42")
        self.assertTrue(state["persisted"])
        self.assertEqual(state["config_file"], self.config)
        self.assertNotIn("ha_token", state)

        restarted = load_reader(**self.env)
        self.assertEqual(restarted._CONFIG["tv_ip"], "10.0.0.42")

    def test_unwritable_config_returns_500(self):
        self.reader.CONFIG_FILE = "/proc/yast-test/config.json"
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/v1/config", {"tv_ip": "10.0.0.1"})
        self.assertEqual(caught.exception.code, 500)
        body = json.loads(caught.exception.read())
        self.assertIn("error", body)


class ReaderCopiesTest(unittest.TestCase):
    def test_addon_and_bridge_reader_are_identical(self):
        with open(ADDON_READER, "rb") as handle:
            addon = handle.read()
        with open(BRIDGE_READER, "rb") as handle:
            bridge = handle.read()
        self.assertEqual(addon, bridge, "addon/reader.py and server/bridge/reader.py drifted apart")


if __name__ == "__main__":
    unittest.main()
