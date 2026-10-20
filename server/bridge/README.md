# Yet Another Samsung Thing — bridge

A tiny Python server (stdlib HTTP + `websocket-client`) that sits on your LAN
between the Samsung TV app and Home Assistant. The TV app reports telemetry to it
and long-polls it for commands; Home Assistant reads `/v1/state` and controls the
TV through it. No Samsung cloud.

## Run with Docker (recommended)

```bash
cp config.sample.json data/config.json   # optional; the setup wizard can also create it
docker compose up -d --build
```

Then open the **setup wizard**:

```
http://<this-host-ip>:8777/config
```

The wizard walks you through:

1. **TV** — *Discover* finds Samsung TVs on your network (or type the IP/MAC manually).
2. **Install the TV app** — *Check TV* reads the TV's Developer-Mode status over its local
   HTTP API (no sdb needed) and tells you exactly what to enable, then generates a ready
   install command (TV IP filled in) you run on any computer with `sdb`. The signed `.wgt`
   is served by the bridge at `/app.wgt`. (See the note on sideloading limits below.)
3. **Home Assistant** — your HA URL + a long-lived access token; *Test* loads your entities.
4. **Lights** — pick which lights/switches appear on the TV dashboard.
5. **Pair the remote** — click, then press **Allow** on the TV (needed for power/key control).

Everything is saved to `data/config.json` and applied live — no rebuild, no restart.

> **Host networking is required.** TV discovery uses SSDP multicast, which Docker's
> default bridge network does not forward. The compose file sets `network_mode: host`
> for this reason; if you run the container yourself, pass `--network host`. Without it,
> everything still works **except** auto-discovery — just enter the TV IP by hand.

## Sideloading the TV app

Samsung requires sideloaded apps to be installed in **Developer Mode** using Samsung's
`sdb` tool, which officially runs on **x86_64** only. So the wizard uses the friendliest
portable approach: it detects Developer Mode for you and hands you an exact, filled-in
command to run on any computer that has `sdb` (it ships with Tizen Studio's CLI tools).
The one-time manual bits — enabling Developer Mode on the TV (Apps → `12345` → set Host
PC IP → reboot) and tapping **Allow** on the first connect — can't be automated away.

The bundled `app.wgt` is pre-signed with a public distributor certificate and installs on
many TVs as-is. If your firmware rejects it, re-sign it with your own Tizen/Samsung
certificate (see the app repo's `sign/` instructions).

## Configuration

Config lives in `data/config.json` (gitignored). Fields:

| Key | What |
|---|---|
| `tv_ip` | TV IP on your LAN |
| `tv_mac` | TV MAC (optional, for Wake-on-LAN) |
| `ha_url` | Home Assistant base URL |
| `ha_token` | HA long-lived token (redacted on `GET /v1/config`) |
| `main_lights` | list of `{entity_id, name}` shown on the TV dashboard |
| `notify_seconds` | **splash time in seconds** — how long a pushed notification stays on screen (default 8) |

Environment variables of the same (UPPER-CASE) name act as bootstrap defaults if a
key is missing from `config.json`.

## HTTP API (summary)

`GET /v1/state` · `POST /v1/control` · `GET /v1/commands` (long-poll) ·
`POST /v1/report` · `GET/POST /v1/config` · `GET /config` (wizard) ·
`GET /v1/discover_tv` · `GET /v1/ha_entities` · `POST /v1/toast` · `POST /v1/cast` ·
`GET /v1/lights` · `POST /v1/lights/toggle` · `GET /img/<file>`.
