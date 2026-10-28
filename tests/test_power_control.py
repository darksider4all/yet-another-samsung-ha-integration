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
        self.m._arm_verify = lambda *a, **k: None

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
        """Pin port and path. Measured 2026-10-25: v2 answers ms.channel.timeOut and
        closes with the reserved 1005 "notack" when the pairing was never acknowledged
        (reported by every client library as a framing error), while v3 connects with
        no token at all and delivers keys while the set is awake."""
        self.m.CLIENT_NAME = "yast-bridge"
        self.m._cfg = lambda k, d=None: "192.168.0.154" if k == "tv_ip" else d
        url = self.m._remote_url("abc123")
        self.assertTrue(url.startswith("wss://192.168.0.154:8002/"), url)
        self.assertIn("/api/v3/channels/samsung.remote.control?", url)
        self.assertIn("token=abc123", url, "still offered for older firmware")


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
        self.m._arm_verify = lambda *a, **k: None

    def test_sends_when_off(self):
        self.m._POWER_CACHE = "standby"
        self.assertEqual(
            self.m.control_power_on(), {"sent": True, "power": "standby", "reason": None, "verified": False, "method": "key", "pending": True}
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
        rejected key read as a successful wake and poisoned the cache with 'on'.
        REST answered and v3 has no pairing, so the refusal is a send failure."""
        self.m._send_remote_key = lambda key: False
        self.m._POWER_CACHE = "standby"
        result = self.m.control_power_on()
        self.assertEqual(result, {"sent": False, "power": "standby", "reason": "send_failed", "verified": False})
        self.assertEqual(self.m._POWER_CACHE, "standby", "a refused key changed no state")

    def test_the_burst_goes_out_before_the_key(self):
        """A set that answers REST can still refuse the key with a stale pairing, so the
        (token-free, ~1.5 s) burst goes first — order is the whole point."""
        order = []
        self.m._wake_on_lan = lambda *a, **k: (order.append("wol"), True)[1]
        self.m._send_remote_key = lambda key: (order.append("key"), True)[1]
        self.m._POWER_CACHE = "standby"
        result = self.m.control_power_on()
        self.assertEqual(order, ["wol", "key"])
        self.assertEqual(result["method"], "wol+key")
        self.assertTrue(result["pending"])




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

    def test_unreachable_set_goes_to_wol(self):
        """A set that answers nothing is in deep standby: only WoL reaches it."""
        self.m._POWER_CACHE = "standby"
        self.m._read_power_now = lambda: None
        self.m._wake_on_lan = lambda *a, **k: True
        self.m._arm_verify = lambda *a, **k: None
        result = self.m.control_power_on()
        self.assertTrue(result["sent"])
        self.assertEqual(result["method"], "wol")
        self.assertTrue(result["pending"], "verification is deferred, not dropped")
        self.assertEqual(self.sent, [], "no key: nothing answers to receive one")


class WakeOnLanContract(unittest.TestCase):
    """Measured 2026-10-24: in deep standby the DU85 answers no socket at all, and a
    10-packet WoL burst woke it in ~3 s. WoL needs no pairing token, so it goes first."""

    def setUp(self):
        self.m = _load_reader()
        self.sent = []
        self.m._send_remote_key = lambda key: (self.sent.append(key), True)[1]
        self.m.TV_TOKEN = "token"
        self.m._POWER_CACHE = "standby"
        self.m._read_power_now = lambda: None

    def test_wol_alone_wakes_without_using_the_token(self):
        self.m._wake_on_lan = lambda *a, **k: True
        result = self.m.control_power_on()
        self.assertEqual(result["method"], "wol")
        self.assertTrue(result["pending"], "verification is deferred so HA does not time out")
        self.assertEqual(self.sent, [], "no key: the wake path must not need pairing")

    def test_a_set_that_answers_is_woken_by_burst_then_key(self):
        self.m._read_power_now = lambda: "standby"
        self.m._wake_on_lan = lambda *a, **k: True   # never a real packet from a test
        result = self.m.control_power_on()
        self.assertEqual(result["method"], "wol+key")
        self.assertEqual(self.sent, ["KEY_POWER"])

    def test_no_wol_and_no_answer_is_a_refusal(self):
        self.m._wake_on_lan = lambda *a, **k: False
        self.m._read_power_now = lambda: None
        result = self.m.control_power_on()
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "power_unknown")

    def test_no_mac_means_no_wol(self):
        self.m._cfg = lambda k, d=None: "" if k == "tv_mac" else d
        self.assertFalse(self.m._wake_on_lan(bursts=1, interval=0))


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


class RestAppPollContract(unittest.TestCase):
    """Telemetry over REST: the same answer as ms.application.get, no token, ~10 ms."""

    def setUp(self):
        self.m = _load_reader()
        self.m._cfg = lambda k, d=None: "192.168.0.154" if k == "tv_ip" else d

    def test_visible_app_is_the_running_app(self):
        def fake(path, method="GET"):
            if "111299001912" in path:
                return '{"id":"111299001912","name":"YouTube","running":true,"visible":true,"version":"2.1"}'
            if "3201907018807" in path:
                return '{"id":"3201907018807","name":"Netflix","running":true,"visible":false,"version":"80"}'
            raise RuntimeError("404")
        self.m._rest_request = fake
        installed, running, err = self.m.poll_apps()
        self.assertIsNone(err)
        self.assertEqual(running["name"], "YouTube")
        self.assertEqual([a["name"] for a in installed], ["YouTube", "Netflix"])

    def test_backgrounded_app_is_not_the_running_app(self):
        def fake(path, method="GET"):
            return '{"id":"x","name":"Netflix","running":true,"visible":false}'
        self.m._rest_request = fake
        _, running, err = self.m.poll_apps()
        self.assertIsNone(running, "running=true alone is not the screen")
        self.assertIsNone(err)

    def test_unreachable_tv_is_an_error_not_a_guess(self):
        def fake(path, method="GET"):
            raise RuntimeError("connection refused")
        self.m._rest_request = fake
        installed, running, err = self.m.poll_apps()
        self.assertEqual(installed, [])
        self.assertIsNone(running)
        self.assertEqual(err, "rest unreachable")

    def test_no_tv_ip_configured(self):
        self.m._cfg = lambda k, d=None: d
        self.assertEqual(self.m.poll_apps()[2], "no tv_ip configured")


class PollerWatchdogContract(unittest.TestCase):
    """Reported: after a full TV restart the card stayed frozen until the add-on was
    restarted, i.e. the poll thread was dead and nothing respawned it."""

    def setUp(self):
        self.m = _load_reader()

    def test_age_is_zero_before_the_first_cycle(self):
        self.m._POLL_BEAT = 0.0
        self.assertEqual(self.m.poller_age(), 0.0)

    def test_age_grows_after_a_beat(self):
        import time as _t
        self.m._POLL_BEAT = _t.monotonic() - 42
        self.assertGreater(self.m.poller_age(), 30)

    def test_poller_survives_a_raising_cycle(self):
        import threading as _t
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise RuntimeError("tv rebooted")

        self.m.poll_apps = boom
        st = self.m.TVState()
        ev = _t.Event()
        th = _t.Thread(target=self.m.poller_loop, args=(st, ev), daemon=True)
        th.start()
        _t.Event().wait(0.6)
        ev.set()
        th.join(timeout=2)
        self.assertGreater(calls["n"], 0, "the poll loop died on the first error")
        self.assertIn("tv rebooted", (st.snapshot().get("last_error") or ""))


class PowerOffClearsAppContract(unittest.TestCase):
    """Reported: after turn_off the card kept the previous app for 5-6 s, because the
    poller only refreshes every 2 s (30 s while off)."""

    def setUp(self):
        self.m = _load_reader()
        self.m._send_remote_key = lambda key: True
        self.m._verify_power = lambda want, tries=4, delay=0.5: True
        self.m._read_power_now = lambda: "on"
        self.state = self.m.TVState()
        self.state.update([{"appId": "3201907018807", "name": "Netflix", "visible": True}],
                          {"appId": "3201907018807", "name": "Netflix", "visible": True},
                          power="on")
        self.m._STATE = self.state

    def test_app_is_dropped_the_instant_the_key_is_off(self):
        self.m.control_power_off()
        snap = self.state.snapshot()
        self.assertIsNone(snap["running_app"], "the card would keep showing Netflix")
        self.assertEqual(snap["power"], "standby")

    def test_the_app_list_is_kept(self):
        self.m.control_power_off()
        self.assertEqual(len(self.state.snapshot()["installed_apps"]), 1)

    def test_no_state_object_is_not_a_crash(self):
        self.m._STATE = None
        self.m.control_power_off()   # must not raise


class FailedSendRestoresTheApp(unittest.TestCase):
    """Clearing the card before the key is honest only if a refusal puts it back."""

    def setUp(self):
        self.m = _load_reader()
        self.m._read_power_now = lambda: "on"
        self.m._verify_power = lambda *a, **k: True
        self.state = self.m.TVState()
        self.app = {"appId": "3201907018807", "name": "Netflix", "visible": True}
        self.state.update([self.app], self.app, power="on")
        self.m._STATE = self.state

    def test_refused_key_restores_the_app(self):
        self.m._send_remote_key = lambda key: False
        self.m.control_power_off()
        snap = self.state.snapshot()
        self.assertEqual((snap["running_app"] or {}).get("name"), "Netflix")
        self.assertEqual(snap["power"], "on")


class AgentErrorIsNotAnApp(unittest.TestCase):
    """Seen live: the card showed app_name "err: AppID is null"."""

    def setUp(self):
        self.m = _load_reader()

    def test_err_foreground_is_dropped(self):
        ag = self.m.AgentState()
        ag.update_from_report({"source": "tv-agent", "foregroundApp": "err: AppID is null",
                               "ts": 1})
        self.assertIsNone(ag.snapshot().get("foregroundApp"))

    def test_a_real_app_still_passes(self):
        ag = self.m.AgentState()
        ag.update_from_report({"source": "tv-agent", "foregroundApp": "111299001912", "ts": 1})
        self.assertEqual(ag.snapshot().get("foregroundApp"), "111299001912")


class WatchdogVsIdleCadence(unittest.TestCase):
    """The watchdog must not respawn a poller that is merely sleeping between idle polls."""

    def setUp(self):
        self.m = _load_reader()

    def test_watchdog_is_well_above_the_idle_interval(self):
        self.assertGreater(self.m._WATCHDOG_AFTER, self.m.POLL_INTERVAL_IDLE * 3)

    def test_a_poller_resting_between_idle_polls_is_not_stale(self):
        import time as _t
        self.m._POLL_BEAT = _t.monotonic() - (self.m.POLL_INTERVAL_IDLE + 5)
        self.assertLess(self.m.poller_age(), self.m._WATCHDOG_AFTER)


class OptimisticOffIsCorrectedContract(unittest.TestCase):
    """Pro's review: the optimistic clear could report "off" while the set was on."""

    def setUp(self):
        self.m = _load_reader()
        self.m._read_power_now = lambda: "on"
        self.app = {"appId": "3201907018807", "name": "Netflix", "visible": True}
        self.state = self.m.TVState()
        self.state.update([self.app], self.app, power="on")
        self.m._STATE = self.state

    def test_unconfirmed_off_does_not_lock_standby(self):
        self.m._send_remote_key = lambda key: True
        self.m._verify_power = lambda *a, **k: False       # the key did not land
        self.m.control_power_off()
        snap = self.state.snapshot()
        self.assertEqual(snap["power"], "on", "claimed off without evidence")
        self.assertEqual((snap["running_app"] or {}).get("name"), "Netflix",
                         "the app was dropped for an off that never happened")

    def test_confirmed_off_locks_standby(self):
        self.m._send_remote_key = lambda key: True
        self.m._verify_power = lambda *a, **k: True
        self.m.control_power_off()
        self.assertEqual(self.state.snapshot()["power"], "standby")
        self.assertIsNone(self.state.snapshot()["running_app"])

    def test_a_send_that_raises_restores_the_card(self):
        def boom(key):
            raise RuntimeError("socket closed")
        self.m._send_remote_key = boom
        self.m._verify_power = lambda *a, **k: True
        result = self.m.control_power_off()
        self.assertEqual(result["reason"], "send_error")
        snap = self.state.snapshot()
        self.assertEqual((snap["running_app"] or {}).get("name"), "Netflix")
        self.assertEqual(snap["power"], "on")

    def test_a_stale_wake_verify_cannot_override_a_later_off(self):
        """generation guard: the wake lands after the user turned the set off again."""
        self.m._note_power("standby", deliberate=True)
        old_gen = self.m.bump_power_gen()
        self.m.bump_power_gen()                    # a newer command supersedes it
        self.m._verify_power = lambda *a, **k: True
        self.m._arm_verify("on", gen=old_gen)
        import time as _t
        _t.sleep(0.3)                              # let the thread run
        self.assertEqual(self.m._POWER_CACHE, "standby",
                         "a stale wake locked 'on' over a newer off")



class PowerStateLiteralContract(unittest.TestCase):
    """Untested before the review: an unknown literal must be unknown, not 'standby'."""

    def setUp(self):
        self.m = _load_reader()      # _read_power_now left intact on purpose

    def test_unknown_literal_is_none(self):
        self.m.get_power_state = lambda: "Weird"
        self.assertIsNone(self.m._read_power_now())

    def test_aliases_are_normalised(self):
        for raw, want in (("on", "on"), ("On", "on"), ("PowerOn", "on"),
                          ("standby", "standby"), ("Off", "standby")):
            self.m.get_power_state = lambda r=raw: r
            self.assertEqual(self.m._read_power_now(), want, raw)


class NotifyRestoresPreviousApp(unittest.TestCase):
    """A toast launched the agent and never put the viewer back where they were."""

    def setUp(self):
        self.m = _load_reader()
        self.toasts, self.launched = [], []
        self.m.set_toast = lambda p: self.toasts.append(p)
        self.m.launch_agent = lambda: (self.launched.append(1), (True, None))[1]
        self.m.control_launch_app = lambda app_id: self.launched.append(app_id)
        self.prev = {"appId": "111299001912", "name": "YouTube", "visible": True}

    def test_no_previous_app_means_no_restore(self):
        self.m._visible_app_now = lambda: None
        self.m.notify_with_restore("hi", 1)
        self.assertEqual(self.launched, [1], "only the agent was launched")

    def test_previous_app_is_relaunched_after_the_toast(self):
        seq = [self.prev, None]          # before: YouTube; after the toast: agent showing
        self.m._visible_app_now = lambda: seq.pop(0) if seq else None
        self.m.notify_with_restore("hi", 1)
        import time as _t
        _t.sleep(3.5)
        self.assertIn("111299001912", self.launched, "YouTube was never brought back")

    def test_viewer_who_switched_apps_is_left_alone(self):
        seq = [self.prev, {"appId": "3201907018807", "name": "Netflix", "visible": True}]
        self.m._visible_app_now = lambda: seq.pop(0) if seq else None
        self.m.notify_with_restore("hi", 1)
        import time as _t
        _t.sleep(3.5)
        self.assertNotIn("111299001912", self.launched, "we fought the viewer")

class PayloadValidationContract(unittest.TestCase):
    """The bridge answered {"ok": true} to {"action":"mute","mute":true} while doing
    nothing (the real key is "value") — a no-op dressed as success, the same lie the
    reserved 1005 close code told. Malformed payloads are refused now, with the shape."""

    def setUp(self):
        self.m = _load_reader()

    def test_the_wrong_key_is_refused(self):
        msg = self.m.validate_agent_action({"action": "mute", "mute": True})
        self.assertIn("value", msg)

    def test_the_right_shape_passes(self):
        self.assertIsNone(self.m.validate_agent_action({"action": "mute", "value": True}))
        self.assertIsNone(self.m.validate_agent_action({"action": "set_volume", "value": 12}))

    def test_a_type_mismatch_is_refused(self):
        self.assertIsNotNone(self.m.validate_agent_action({"action": "mute", "value": "yes"}))
        self.assertIsNotNone(self.m.validate_agent_action({"action": "set_volume", "value": True}),
                             "a boolean is not a volume")

    def test_actions_without_required_args_are_untouched(self):
        self.assertIsNone(self.m.validate_agent_action({"action": "volume_up"}))
        self.assertIsNone(self.m.validate_agent_action({"action": "nope"}))
