#!/usr/bin/env python3
"""On-TV agent watchdog. Policy: load the agent ONCE when the TV powers on,
otherwise leave it alone - no continuous relaunching (that used to yank the app
foreground and interrupt viewing). Trigger: TV raw PowerState edge to "on"; then
if the agent isn't running, do a fresh kill+launch in BACKGROUND mode.

All host-specific values come from the environment (see watchdog.env.sample):
  BRIDGE_URL  the bridge, e.g. http://192.168.1.10:8777
  TV_IP       the TV's IP on your LAN, e.g. 192.168.1.50
  SDB         path to the sdb binary (Tizen Studio tools)
  APP         the Tizen app id (default ChanProbe0.ChannelProbe)
  POLL        seconds between checks (default 15)
"""
import json, os, subprocess, time, urllib.request

BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8777").rstrip("/")
TV_IP = os.environ.get("TV_IP", "")
SDB = os.environ.get("SDB", "sdb")
APP = os.environ.get("APP", "ChanProbe0.ChannelProbe")
POLL = int(os.environ.get("POLL", "15"))

STATE_URL = BRIDGE_URL + "/v1/state"
LAUNCHMODE_URL = BRIDGE_URL + "/v1/launchmode"
TV_REST = "http://%s:8001/api/v2/" % TV_IP
SERIAL = "%s:26101" % TV_IP


def sh(args, timeout=25):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        print("sh error:", e, flush=True); return None


def raw_power():
    try:
        with urllib.request.urlopen(TV_REST, timeout=5) as r:
            d = json.load(r)
        return (d.get("device") or {}).get("PowerState") or None
    except Exception:
        return None


def agent_online():
    try:
        with urllib.request.urlopen(STATE_URL, timeout=5) as r:
            d = json.load(r)
        return bool((d.get("agent") or {}).get("online"))
    except Exception:
        return None


def auto_load_enabled():
    """Read the `auto_load` flag from the bridge config; default ON if unreachable.
    Lets the setup wizard's toggle turn power-on auto-launch on/off without redeploying."""
    try:
        with urllib.request.urlopen(BRIDGE_URL + "/v1/config", timeout=5) as r:
            return json.load(r).get("auto_load", True) is not False
    except Exception:
        return True


def set_background():
    try:
        req = urllib.request.Request(LAUNCHMODE_URL, data=json.dumps({"mode": "background"}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print("set_background error:", e, flush=True)


def relaunch_background():
    set_background()
    sh([SDB, "disconnect", TV_IP], timeout=10); time.sleep(1)
    sh([SDB, "connect", TV_IP], timeout=15); time.sleep(2)
    sh([SDB, "-s", SERIAL, "shell", "0", "was_kill", APP], timeout=15); time.sleep(1)
    r = sh([SDB, "-s", SERIAL, "shell", "0", "was_execute", APP], timeout=20)
    if r is not None:
        out = ((r.stdout or "") + (r.stderr or "")).replace("\r", "").strip()
        print("relaunch result:", out[-200:], flush=True)


def main():
    if not TV_IP:
        print("TV_IP is not set - edit watchdog.env. Exiting.", flush=True); return
    armed = True
    print("tv-agent watchdog started (load-once-on-power-on policy) TV=%s bridge=%s" % (TV_IP, BRIDGE_URL), flush=True)
    while True:
        try:
            p = raw_power()
            if p in ("standby", "off"):
                armed = True
            elif p == "on" and armed:
                online = agent_online()
                if online is False:
                    if auto_load_enabled():
                        print("TV powered on + agent not running -> background relaunch", flush=True)
                        relaunch_background()
                    else:
                        print("TV powered on + agent not running, but auto-load is OFF -> skipping", flush=True)
                    armed = False
                elif online is True:
                    armed = False
        except Exception as e:
            print("watchdog loop error:", e, flush=True)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
