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
        # Offline and deterministic: the fresh REST read the power path now does is
        # stubbed from the cache the test sets, and a sent toggle is assumed to land
        # unless the test says otherwise.
        self.m._read_power_now = lambda: self.m._POWER_CACHE
        self.m._verify_power = lambda want, tries=6, delay=0.7: True
        # never put a real magic packet on the LAN from a test
        self.m._wake_on_lan = lambda *a, **k: False

    def test_sends_when_on(self):
        self.m._POWER_CACHE = "on"
        self.assertEqual(
            self.m.control_power_off(), {"sent": True, "power": "on", "reason": None, "verified": True}
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

    def test_a_refused_power_off_key_is_reported(self):
        """Same lie on the off path: a rejected key was reported as sent."""
        self.m._send_remote_key = lambda key: False
        self.m._POWER_CACHE = "on"
        result = self.m.control_power_off()
        self.assertEqual(result, {"sent": False, "power": "on", "reason": "send_failed", "verified": False})
        self.assertEqual(self.m._POWER_CACHE, "on", "a refused key changed no state")

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


class PowerOnContract(unittest.TestCase):
    """control_power_on had no guard: KEY_POWER is a blind toggle, so a stale cache
    (or the no-op path) could switch a live TV off, and HA could not tell."""

    def setUp(self):
        self.m = _load_reader()
        self.sent = []
        self.m._send_remote_key = lambda key: (self.sent.append(key), True)[1]
        self.m.TV_TOKEN = "token"
        # Offline and deterministic: the fresh REST read the power path now does is
        # stubbed from the cache the test sets, and a sent toggle is assumed to land
        # unless the test says otherwise.
        self.m._read_power_now = lambda: self.m._POWER_CACHE
        self.m._verify_power = lambda want, tries=6, delay=0.7: True
        # never put a real magic packet on the LAN from a test
        self.m._wake_on_lan = lambda *a, **k: False

    def test_sends_when_off(self):
        self.m._POWER_CACHE = "standby"
        self.assertEqual(
            self.m.control_power_on(), {"sent": True, "power": "standby", "reason": None, "verified": True, "method": "key"}
        )
        self.assertEqual(self.sent, ["KEY_POWER"])

    def test_already_on_never_toggles(self):
        self.m._POWER_CACHE = "on"
        result = self.m.control_power_on()
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "already_on")
        self.assertEqual(self.sent, [], "KEY_POWER would have switched the TV off")

    def test_unknown_power_refused(self):
        self.m._POWER_CACHE = None
        result = self.m.control_power_on()
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "power_unknown")
        self.assertEqual(self.sent, [])

    def test_a_refused_send_is_not_reported_as_a_wake(self):
        """_send_remote_key returns False on an explicit ms.error; ignoring it made a
        rejected key read as a successful wake and poisoned the cache with 'on'."""
        self.m._send_remote_key = lambda key: False
        self.m._POWER_CACHE = "standby"
        result = self.m.control_power_on()
        self.assertEqual(result, {"sent": False, "power": "standby", "reason": "send_failed", "verified": False})
        self.assertEqual(self.m._POWER_CACHE, "standby", "a refused key changed no state")


class StandbyPrimingTest(unittest.TestCase):
    """P1-2: the WS poller's standby inference was computed and thrown away, so after a
    turn_off + power loss the cache stayed None and turn_on refused as power_unknown.
    The fix is a pure decision helper plus a _note_power() call - pinned here because
    the poller loop itself needs a TV."""

    def setUp(self):
        self.m = _load_reader()
        self.sent = []
        self.m._send_remote_key = lambda key: (self.sent.append(key), True)[1]
        self.m._POWER_CACHE = None

    def test_rest_confirmed_standby_primes_the_cache(self):
        self.m._note_power(self.m._ws_power_note(None, "standby"))
        self.assertEqual(self.m._POWER_CACHE, "standby")
        self.assertTrue(self.m.control_power_on()["sent"], "turn_on must fire after priming")
        self.assertEqual(self.sent, ["KEY_POWER"])

    def test_a_visible_app_still_means_on(self):
        self.m._note_power(self.m._ws_power_note({"appId": "x", "visible": True}, "standby"))
        self.assertEqual(self.m._POWER_CACHE, "on")

    def test_no_inference_without_solid_evidence(self):
        # a hidden running app proves nothing, and REST 'on' may only confirm on
        self.assertIsNone(
            self.m._ws_power_note({"appId": "x", "running": True, "visible": False}, None)
        )
        self.assertIsNone(self.m._ws_power_note(None, "on"))
        self.assertIsNone(self.m._ws_power_note(None, None))


if __name__ == "__main__":
    unittest.main()


class PowerTruthContract(unittest.TestCase):
    """The set's real state must beat our cache: a viewer who switches the TV on with
    the remote leaves the cache saying 'standby', and KEY_POWER is a blind toggle."""

    def setUp(self):
        self.m = _load_reader()
        self.sent = []
        self.m._send_remote_key = lambda key: (self.sent.append(key), True)[1]
        self.m.TV_TOKEN = "token"
        self.m._verify_power = lambda want, tries=6, delay=0.7: True
        self.m._wake_on_lan = lambda *a, **k: False

    def test_stale_cache_standby_rest_on_sends_nothing(self):
        self.m._POWER_CACHE = "standby"
        self.m._read_power_now = lambda: "on"
        result = self.m.control_power_on()
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "already_on")
        self.assertEqual(self.sent, [], "a blind toggle here switched the TV off")

    def test_stale_cache_on_rest_standby_sends_nothing_off(self):
        self.m._POWER_CACHE = "on"
        self.m._read_power_now = lambda: "standby"
        result = self.m.control_power_off()
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "already_off")
        self.assertEqual(self.sent, [])

    def test_toggle_that_does_not_land_is_not_claimed(self):
        self.m._POWER_CACHE = "standby"
        self.m._read_power_now = lambda: "standby"
        self.m._verify_power = lambda want, tries=6, delay=0.7: False
        result = self.m.control_power_on()
        self.assertTrue(result["sent"])
        self.assertFalse(result["verified"], "a key is never acked: only the poll proves it")
        self.assertEqual(self.m._POWER_CACHE, "standby", "an unverified toggle must not lock the cache")

    def test_rest_unreachable_falls_back_to_the_cache(self):
        self.m._POWER_CACHE = "standby"
        self.m._read_power_now = lambda: None
        result = self.m.control_power_on()
        self.assertTrue(result["sent"])


class WakeOnLanContract(unittest.TestCase):
    """Measured 2026-10-24: in deep standby the DU85 answers no socket at all, and a
    10-packet WoL burst woke it in ~3 s. WoL needs no pairing token, so it goes first."""

    def setUp(self):
        self.m = _load_reader()
        self.sent = []
        self.m._send_remote_key = lambda key: (self.sent.append(key), True)[1]
        self.m.TV_TOKEN = "token"
        self.m._POWER_CACHE = "standby"
        self.m._read_power_now = lambda: "standby"

    def test_wol_alone_wakes_without_using_the_token(self):
        self.m._wake_on_lan = lambda *a, **k: True
        self.m._verify_power = lambda want, tries=12, delay=1.0: True
        result = self.m.control_power_on()
        self.assertTrue(result["verified"])
        self.assertEqual(result["method"], "wol")
        self.assertEqual(self.sent, [], "no key: the wake path must not need pairing")

    def test_wol_then_key_when_the_set_is_only_in_standby(self):
        self.m._wake_on_lan = lambda *a, **k: True
        self.m._verify_power = lambda want, tries=12, delay=1.0: False
        result = self.m.control_power_on()
        self.assertEqual(result["method"], "wol+key")
        self.assertEqual(self.sent, ["KEY_POWER"])

    def test_neither_path_lands_is_reported_unverified(self):
        self.m._wake_on_lan = lambda *a, **k: True
        self.m._verify_power = lambda want, tries=12, delay=1.0: False
        self.m._send_remote_key = lambda key: False
        result = self.m.control_power_on()
        self.assertTrue(result["sent"], "a packet did leave the host")
        self.assertFalse(result["verified"])
        self.assertEqual(self.m._POWER_CACHE, "standby", "an unverified wake must not lock the cache")

    def test_no_mac_means_no_wol(self):
        self.m._cfg = lambda k, d=None: "" if k == "tv_mac" else d
        self.assertFalse(self.m._wake_on_lan(bursts=1, interval=0))


class PollerBackoffContract(unittest.TestCase):
    """60 s of blindness after a dropped read is what made the card lag by a minute."""

    def setUp(self):
        self.m = _load_reader()

    def test_dropped_read_retries_fast(self):
        self.assertEqual(self.m._retry_delay(True), 5.0)

    def test_refused_connect_still_waits_a_minute(self):
        # every failed connect can raise an "Allow this device?" dialog on the TV
        self.assertEqual(self.m._retry_delay(False), 60.0)


class StaleSnapshotContract(unittest.TestCase):
    """A frozen poll was replayed forever: the TV sat in standby while the card still
    showed 'on, YouTube' 50 s later."""

    def setUp(self):
        self.m = _load_reader()

    def test_no_timestamp_is_infinitely_stale(self):
        self.assertGreater(self.m._snapshot_age({}), self.m.STALE_AFTER)

    def test_fresh_snapshot_is_believed(self):
        from datetime import datetime, timezone
        snap = {"timestamp": datetime.now(timezone.utc).isoformat()}
        self.assertLess(self.m._snapshot_age(snap), self.m.STALE_AFTER)

    def test_old_snapshot_is_stale(self):
        snap = {"timestamp": "2020-01-01T00:00:00+00:00"}
        self.assertGreater(self.m._snapshot_age(snap), self.m.STALE_AFTER)

    def test_garbage_timestamp_is_stale(self):
        self.assertGreater(self.m._snapshot_age({"timestamp": "not-a-date"}), self.m.STALE_AFTER)
