"""A control that did not happen must reach Home Assistant as an error.

Measured on the living-room DU85 (2026-11-03): the bridge answered HTTP 200 with
``{"ok": false, "error": ...}`` for agent actions it had queued but could not get
executed, and the HA side only raised on status >= 400 — so the card reported success
while the tuner never launched (the agent's ``tizen.application.launch`` was refused
for a missing ``appmanager.launch`` privilege). Two fixes are locked in here:

  * ``TvReaderCoordinator.control`` raises on ``ok: false``, not just on 4xx/5xx
    (the single choke point every volume/mute/tune/launch call goes through);
  * ``run_agent_command`` gates ``launch_app`` on the agent's own execution report
    (``cmd:launch_app`` / ``tv-agent-cmderr``), because pickup is not execution.
"""
import asyncio
import importlib.util
import json
import os
import pathlib
import sys
import time
import types
import unittest
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_bridge_runtime import BridgeTest  # noqa: E402  (load_reader + server fixture)


# ---- coordinator.control ---------------------------------------------------

class _Body:
    def __init__(self, payload):
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload


class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return self._payload


class _Session:
    def __init__(self, status, payload):
        self.status, self.payload = status, payload
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        return _Resp(self.status, self.payload)


class _UpdateFailed(Exception):
    pass


def _load_coordinator():
    """Import custom_components/tv_reader/__init__.py with HA and aiohttp stubbed out."""
    if "aiohttp" not in sys.modules:
        aio = types.ModuleType("aiohttp")

        class ClientSession:
            pass

        class ClientTimeout:
            def __init__(self, *a, **k):
                pass

        aio.ClientSession = ClientSession
        aio.ClientTimeout = ClientTimeout
        aio.ClientError = Exception
        sys.modules["aiohttp"] = aio
    if "ha_stub" not in sys.modules:
        ha = types.ModuleType("homeassistant")

        class HomeAssistantError(Exception):
            pass

        class DataUpdateCoordinator:
            def __init__(self, *a, **k):
                pass

            async def async_config_entry_first_refresh(self):
                pass

        class ConfigEntry:
            pass

        def mod(name, parent=None, **attrs):
            m = types.ModuleType(name)
            for k, v in attrs.items():
                setattr(m, k, v)
            sys.modules[name] = m
            if parent is not None:
                setattr(parent, name.rsplit(".", 1)[1], m)
            return m

        mod("homeassistant.const", ha, EVENT_HOMEASSISTANT_STOP="stop",
            Platform=types.SimpleNamespace(SENSOR="sensor", MEDIA_PLAYER="media_player"))
        mod("homeassistant.core", ha, HomeAssistant=object, ServiceCall=object,
            callback=lambda f: f)
        mod("homeassistant.exceptions", ha, HomeAssistantError=HomeAssistantError)
        helpers = mod("homeassistant.helpers", ha)
        mod("homeassistant.helpers.update_coordinator", helpers,
            DataUpdateCoordinator=DataUpdateCoordinator, UpdateFailed=_UpdateFailed)
        mod("homeassistant.helpers.entity_platform", helpers, AddEntitiesCallback=object)
        mod("homeassistant.helpers.config_validation", helpers, string=object)
        mod("homeassistant.config_entries", ha, ConfigEntry=ConfigEntry,
            OptionsFlow=None)
        mod("voluptuous", Optional=lambda *a, **k: None, Required=lambda *a, **k: None,
            Schema=lambda *a, **k: None)
        for sub in ("sensor", "media_player"):
            mod(f"homeassistant.components.{sub}", ha,
                SensorEntity=object, MediaPlayerEntity=object)
        ha.HomeAssistantError = HomeAssistantError
        sys.modules["homeassistant"] = ha
        sys.modules["ha_stub"] = ha

    pkgdir = ROOT / "custom_components" / "tv_reader"
    spec = importlib.util.spec_from_file_location(
        "tvcoord", pkgdir / "__init__.py",
        submodule_search_locations=[str(pkgdir)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tvcoord"] = mod
    # load the real const.py rather than a stub: it is plain constants + HA Platform
    cspec = importlib.util.spec_from_file_location("tvcoord.const", pkgdir / "const.py")
    const = importlib.util.module_from_spec(cspec)
    sys.modules["tvcoord.const"] = const
    cspec.loader.exec_module(const)
    spec.loader.exec_module(mod)
    return mod, sys.modules["ha_stub"].HomeAssistantError


def _coordinator(session):
    mod, error = _load_coordinator()
    c = mod.TvReaderCoordinator.__new__(mod.TvReaderCoordinator)
    c._host = "http://bridge"
    c._session = session
    return c, error


class ControlRaisesOnNotOk(unittest.IsolatedAsyncioTestCase):
    def test_a_200_with_ok_false_raises(self):
        session = _Session(200, {"ok": False, "queued": True,
                                 "error": "agent failed launch_app: SecurityError"})
        coord, HomeAssistantError = _coordinator(session)
        with self.assertRaises(HomeAssistantError) as ctx:
            asyncio.run(coord.control("launch_app", app_id="org.tizen.tv-viewer"))
        self.assertIn("SecurityError", str(ctx.exception))

    def test_a_200_with_ok_true_returns_the_body(self):
        session = _Session(200, {"ok": True, "queued": True})
        coord, _ = _coordinator(session)
        self.assertEqual(asyncio.run(coord.control("mute", value=True))["ok"], True)

    def test_a_refusal_status_still_raises(self):
        session = _Session(409, {"ok": False, "reason": "power_unknown"})
        coord, HomeAssistantError = _coordinator(session)
        with self.assertRaises(HomeAssistantError):
            asyncio.run(coord.control("power_on"))

    def test_already_on_is_a_satisfied_request_not_an_error(self):
        session = _Session(200, {"ok": False, "sent": False, "reason": "already_on",
                                 "power": "on", "verified": True})
        coord, _ = _coordinator(session)
        self.assertEqual(asyncio.run(coord.control("power_on"))["reason"], "already_on")


# ---- agent execution report gate -------------------------------------------

class LaunchReportedNotAssumed(BridgeTest):
    def setUp(self):
        super().setUp()
        self.mod.AGENT_PICKUP_WAIT = 0.2
        self.mod.AGENT_RESULT_WAIT = 0.4
        # keep the real control_launch_app: most tests here stub the agent away, but the
        # re-entry guard test must call the real one.
        self._real_control_launch_app = self.mod.control_launch_app
        self.mod.control_close_app = lambda app_id: True
        self.mod.app_state = lambda app_id: {"running": False}
        self.mod.control_launch_app = lambda app_id: True
        self.mod._poke_ha_state = lambda delay=0.0: None

    def _poller(self, delay, report=None):
        def run():
            import time
            time.sleep(delay)
            self.mod.take_commands()
            if report is not None:
                with self.mod._report_lock:
                    self.mod._REPORT_RX.append((time.time(), report))
        import threading
        threading.Thread(target=run, daemon=True).start()

    def test_a_picked_up_launch_without_a_report_is_not_ok(self):
        self._poller(0.05)
        ok, err = self.mod.run_agent_command(
            {"action": "launch_app", "app_id": self.mod.LIVE_TV_APP})
        self.assertFalse(ok)
        self.assertIn("never reported", err)

    def test_the_agents_error_report_denies_and_explains(self):
        self._poller(0.05, {"source": "tv-agent-cmderr", "action": "launch_app",
                            "err": "SecurityError: permission denied"})
        ok, err = self.mod.run_agent_command(
            {"action": "launch_app", "app_id": self.mod.LIVE_TV_APP})
        self.assertFalse(ok)
        self.assertIn("permission denied", err)

    def test_the_agents_success_report_is_ok(self):
        self._poller(0.05, {"source": "tv-agent", "event": "cmd:launch_app"})
        ok, err = self.mod.run_agent_command(
            {"action": "launch_app", "app_id": self.mod.LIVE_TV_APP})
        self.assertTrue(ok, err)
        self.assertIsNone(err)

    def test_other_actions_keep_the_pickup_contract(self):
        """mute has no report gate: the phone-side test proved pickup is enough there."""
        self._poller(0.05)
        ok, err = self.mod.run_agent_command({"action": "mute", "value": True})
        self.assertTrue(ok, err)

    def test_a_launch_while_already_waking_the_agent_is_refused(self):
        def rest_404(path, method="GET"):
            err = RuntimeError("HTTP Error 404: Not Found")
            err.code = 404
            raise err

        self.mod._rest_request = rest_404
        self.mod.run_agent_command = lambda cmd: (_ for _ in ()).throw(
            AssertionError("must not recurse"))
        self.mod._AGENT_LAUNCH.busy = True
        try:
            with self.assertRaises(RuntimeError) as ctx:
                self._real_control_launch_app(self.mod.LIVE_TV_APP)
            self.assertIn("already waking the agent", str(ctx.exception))
        finally:
            self.mod._AGENT_LAUNCH.busy = False


# ---- launch gated on the TV's screen, not on the agent's callback -----------

class LaunchGatedOnScreen(BridgeTest):
    """`ok` is the TV's own ``visible`` flag (measured on the DU85 2026-11-03).

    The deployed agent posted ``cmd:launch_app`` twice while Netflix kept the screen and
    the tuner never appeared, so a callback can never be the proof. Exactly one app
    reports visible:true at a time (YouTube and the agent kept running:true while Netflix
    had it), and the tuner is not in the REST catalogue at all (404).
    """

    NETFLIX = "3201907018807"
    YOUTUBE = "111299001912"
    TUNER = "org.tizen.tv-viewer"

    def setUp(self):
        super().setUp()
        self.mod.LAUNCH_SCREEN_WAIT = 0.3
        self.mod.LAUNCH_SETTLE = 0.05
        self.mod.control_close_app = lambda app_id: True
        self.mod.app_state = lambda app_id: {"running": True}
        self.mod.run_agent_command = lambda cmd, wait=None: (True, None)
        self.mod._poke_ha_state = lambda delay=0.0: None
        self.screen = {}          # app_id -> visible; missing key = 404, not catalogued

        def app_status(app_id):
            if app_id not in self.screen:
                return {}
            return {"id": app_id, "name": app_id, "running": True,
                    "visible": self.screen[app_id]}

        def rest(path, method="GET"):
            if path.endswith("/" + self.TUNER):
                err = RuntimeError("HTTP Error 404: Not Found")
                err.code = 404
                raise err
            return "{}"

        self.mod._rest_app_status = app_status
        self.mod._rest_request = rest

    def _was_showing(self, app_id, name="Netflix"):
        self.mod.poll_apps = lambda: ([], {"appId": app_id, "name": name, "visible": True}, None)

    def test_a_listed_app_is_ok_once_the_tv_shows_it(self):
        self._was_showing(self.NETFLIX)
        self.screen = {self.NETFLIX: False, self.YOUTUBE: True}
        self.assertTrue(self.mod.control_launch_app(self.YOUTUBE))

    def test_a_launch_the_tv_ignores_is_refused(self):
        """The measured failure: accepted, never on screen -> ok:false, not ok:true."""
        self._was_showing(self.NETFLIX)
        self.screen = {self.NETFLIX: True, self.YOUTUBE: False}
        with self.assertRaises(RuntimeError) as ctx:
            self.mod.control_launch_app(self.YOUTUBE)
        self.assertIn("screen never changed", str(ctx.exception))
        self.assertIn("Netflix still foreground", str(ctx.exception))

    def test_the_tuner_is_proven_by_the_previous_app_giving_up_the_screen(self):
        self._was_showing(self.NETFLIX)
        self.screen = {self.NETFLIX: False}          # tuner 404s: unlisted, but on screen
        self.assertTrue(self.mod.control_launch_app(self.TUNER))

    def test_the_tuner_gate_closes_when_the_previous_app_lies_back_down(self):
        """The splash flashes through the gate; the returning app must close it again."""
        self._was_showing(self.NETFLIX)
        self.screen = {self.NETFLIX: True}           # never gave the screen up
        with self.assertRaises(RuntimeError) as ctx:
            self.mod.control_launch_app(self.TUNER)
        self.assertIn("screen never changed", str(ctx.exception))

    def test_nothing_on_screen_before_leaves_the_gate_closed(self):
        self.mod.poll_apps = lambda: ([], None, None)   # e.g. the tuner was already up
        self.screen = {}
        with self.assertRaises(RuntimeError) as ctx:
            self.mod.control_launch_app(self.TUNER)
        self.assertIn("cannot verify", str(ctx.exception))

    def test_the_already_foreground_app_needs_no_proof(self):
        self._was_showing(self.NETFLIX)
        self.screen = {self.NETFLIX: True}
        self.assertTrue(self.mod.control_launch_app(self.NETFLIX))

    def test_waking_our_own_agent_is_not_gated_on_the_screen(self):
        """launch_agent must not wait for the agent's own splash: it is plumbing."""
        self.mod.poll_apps = lambda: ([], None, None)
        self.screen = {}
        self.assertTrue(self.mod.control_launch_app(self.mod.DEFAULT_AGENT_APP))

    def test_a_refused_launch_answers_409_not_500(self):
        self._was_showing(self.NETFLIX)
        self.screen = {self.NETFLIX: True, self.YOUTUBE: False}
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/control", {"action": "launch_app", "app_id": self.YOUTUBE})
        self.assertEqual(ctx.exception.code, 409)


# ---- the agent's report over the real wire ----------------------------------

class ReportArrivesOverTheWire(BridgeTest):
    """`_REPORT_RX` is filled by the POST handler, so exercise the handler itself."""

    TUNER = "org.tizen.tv-viewer"

    def test_a_posted_report_is_what_unblocks_the_launch(self):
        since = time.time()
        self.post("/v1/report", {"source": "tv-agent", "event": "cmd:launch_app",
                                 "app_id": self.TUNER})
        ok, err = self.mod._wait_agent_result("launch_app", since, app_id=self.TUNER, wait=0.2)
        self.assertTrue(ok, err)
        self.assertIsNone(err)

    def test_a_report_for_another_app_does_not_count(self):
        since = time.time()
        self.post("/v1/report", {"source": "tv-agent", "event": "cmd:launch_app",
                                 "app_id": "3201907018807"})
        ok, err = self.mod._wait_agent_result("launch_app", since, app_id=self.TUNER, wait=0.2)
        self.assertFalse(ok)
        self.assertIn("never reported", err)


if __name__ == "__main__":
    unittest.main()
