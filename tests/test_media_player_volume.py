"""The mute button must post what the bridge accepts.

Regression (2026-11-03): async_mute_volume sent a bare {"action":"mute"}, which the
bridge refuses with 400 bad_payload since it began validating payload shapes
(AGENT_ACTION_ARGS in addon/reader.py). It also skipped the call whenever the cached
is_volume_muted already matched — and that cache reads false while the TV is unmuted
but the agent is stale, so unmute never left HA at all.

media_player.py imports homeassistant, which this suite does not install, so the HA
surface it touches is stubbed below. The assertions are on the payload, which is the
contract with the bridge.
"""
import importlib.util
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Feature:
    VOLUME_SET = 1
    VOLUME_STEP = 2
    VOLUME_MUTE = 4
    TURN_ON = 8
    TURN_OFF = 16
    PLAY_MEDIA = 32
    SELECT_SOURCE = 64


class _State:
    ON = "on"
    OFF = "off"


class _MediaType:
    CHANNEL = "channel"


class _Any:
    """Permissive stand-in for the leftover HA helpers (cv.string, vol.Required...)."""

    def __getattr__(self, _name):
        return _Any()

    def __call__(self, *a, **k):
        return _Any()


def _install_stubs():
    """Just enough of homeassistant/voluptuous for media_player.py to import."""
    mods = {}

    def mod(name, parent=None, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        mods[name] = m
        if parent:  # submodules are reachable as attributes of their package
            setattr(parent, name.rsplit(".", 1)[1], m)
        return m

    def _base():
        # A fresh class per role: media_player subclasses two of them.
        class _B:
            def __init__(self, *a, **k):
                pass

        return _B

    ha = mod("homeassistant")
    comps = mod("homeassistant.components", ha)
    mod(
        "homeassistant.components.media_player",
        comps,
        MediaPlayerEntity=_base(),
        MediaPlayerEntityFeature=_Feature,
        MediaPlayerState=_State,
        MediaType=_MediaType,
    )
    mod("homeassistant.config_entries", ha, ConfigEntry=_base())
    mod("homeassistant.core", ha, HomeAssistant=_base())
    helpers = mod("homeassistant.helpers", ha)
    mod("homeassistant.helpers.entity_platform", helpers, AddEntitiesCallback=_base())
    mod(
        "homeassistant.helpers.config_validation",
        helpers,
        string=_Any(),
        positive_int=_Any(),
    )
    mod("homeassistant.helpers.update_coordinator", helpers, CoordinatorEntity=_base())
    mod("voluptuous", Required=_Any(), Optional=_Any(), Schema=_Any())

    pkg = mod("tvpkg")
    pkg.__path__ = [str(ROOT / "custom_components" / "tv_reader")]
    pkg.TvReaderCoordinator = _base()
    mod("tvpkg.const", pkg, DOMAIN="tv_reader")


def _load_media_player():
    _install_stubs()
    spec = importlib.util.spec_from_file_location(
        "tvpkg.media_player", ROOT / "custom_components/tv_reader/media_player.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Coordinator:
    def __init__(self):
        self.calls = []

    async def control(self, action, **kwargs):
        self.calls.append({"action": action, **kwargs})
        return {"ok": True, "queued": True}


class MutePayload(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mod = _load_media_player()

    def _entity(self, agent_audio):
        e = object.__new__(self.mod.TvReaderMediaPlayer)
        e.coordinator = _Coordinator()
        # stale/absent audio is the normal case: is_volume_muted reads false
        e.coordinator.data = {"agent": {"audio": agent_audio}}
        return e

    async def test_mute_sends_value_true(self):
        e = self._entity({"mute": False, "volume": 20})
        await e.async_mute_volume(True)
        self.assertEqual(e.coordinator.calls, [{"action": "mute", "value": True}])

    async def test_unmute_sends_value_false(self):
        e = self._entity({"mute": True, "volume": 20})
        await e.async_mute_volume(False)
        self.assertEqual(e.coordinator.calls, [{"action": "mute", "value": False}])

    async def test_sent_even_when_cached_state_already_matches(self):
        """The old guard swallowed the call here; it must still go out."""
        e = self._entity(None)  # agent silent -> is_volume_muted is False
        self.assertFalse(e.is_volume_muted)
        await e.async_mute_volume(False)
        self.assertEqual(e.coordinator.calls, [{"action": "mute", "value": False}])

    async def test_value_is_a_real_bool(self):
        """The bridge rejects non-bools with 400, so 1/0 must not leak through."""
        e = self._entity(None)
        await e.async_mute_volume(1)
        self.assertIs(e.coordinator.calls[0]["value"], True)


class VolumeUnit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mod = _load_media_player()

    def _entity(self):
        e = object.__new__(self.mod.TvReaderMediaPlayer)
        e.coordinator = _Coordinator()
        e.coordinator.data = {"agent": {"audio": {"volume": 20}}}
        return e

    async def test_set_volume_is_percent_0_100(self):
        """The on-TV agent calls tizen.tvaudiocontrol.setVolume(parseInt(value))."""
        e = self._entity()
        await e.async_set_volume_level(0.35)
        self.assertEqual(e.coordinator.calls, [{"action": "set_volume", "value": 35}])

    async def test_step_actions_carry_no_argument(self):
        e = self._entity()
        await e.async_volume_up()
        await e.async_volume_down()
        self.assertEqual(
            e.coordinator.calls, [{"action": "volume_up"}, {"action": "volume_down"}]
        )


if __name__ == "__main__":
    unittest.main()
