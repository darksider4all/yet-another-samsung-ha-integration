# tv-agent watchdog

Loads the on-TV app **once** each time the TV powers on, then leaves it alone.
It does **not** relaunch continuously — that used to pull the app to the foreground
and interrupt whatever you were watching. It watches the TV's raw `PowerState` and,
on a transition to `on`, launches the app in **background** mode if it isn't already
running.

Runs on any always-on Linux host on your LAN that has Tizen's `sdb` and can reach the
TV in Developer Mode (the same "Host PC IP" you set in the TV's Developer Mode).

## Install (systemd)

```bash
sudo mkdir -p /opt/tv-agent-watchdog /etc/tv-agent-watchdog
sudo cp watchdog.py /opt/tv-agent-watchdog/
sudo cp watchdog.env.sample /etc/tv-agent-watchdog/watchdog.env
sudo nano /etc/tv-agent-watchdog/watchdog.env      # set BRIDGE_URL, TV_IP, SDB
sudo cp tv-agent-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tv-agent-watchdog
journalctl -u tv-agent-watchdog -f
```

## Configuration (watchdog.env)

| Var | What |
|---|---|
| `BRIDGE_URL` | the bridge, e.g. `http://192.168.1.10:8777` |
| `TV_IP` | the TV's IP on your LAN |
| `SDB` | path to the `sdb` binary (Tizen Studio tools) |
| `APP` | Tizen app id (default `ChanProbe0.ChannelProbe`) |
| `POLL` | seconds between checks (default 15) |

> **Prerequisite:** the TV must be in **Developer Mode** with this host set as the
> Host PC, so `sdb connect <TV_IP>` works.
