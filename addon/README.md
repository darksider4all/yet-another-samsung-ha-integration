# Yet Another Samsung Thing - Bridge (Home Assistant add-on)

Runs the bridge as a Home Assistant OS / Supervised add-on, so you don't
need a separate Docker host. Config, the paired TV token, and cast images are stored in
the add-on's persistent `/data`.

## Install

This folder is an add-on. To use it, add this repository to Home Assistant:

**Settings → Add-ons → Add-on Store → ⋮ → Repositories** and paste:

```
https://github.com/darksider4all/yet-another-samsung-ha-integration
```

Then install **"Yet Another Samsung Thing - Bridge"**, start it, and open its **Web UI**
(the setup wizard) from the add-on page or the sidebar (ingress).

## Why host networking

The add-on sets `host_network: true` because TV auto-discovery uses **SSDP multicast**,
which does not reach containers on HA's internal network. With host networking the bridge
also listens on `<ha-host-ip>:8777`, which is the URL you give the TV app and the
companion integration. If you'd rather not use host networking, discovery is disabled but
you can still enter the TV IP manually in the wizard.

## After starting

1. Open the Web UI → complete the wizard:
   - **TV** — discover or enter the TV's IP.
   - **Install the TV app** — guided sideload (see the app repo below).
   - **Home Assistant** — URL + long-lived token.
   - **Lights** — pick the lights/switches for the TV dashboard.
   - **Splash time** — how long a pushed notification stays on the TV, **in seconds**.
   - **Pair the remote** — tap *Allow* on the TV.
2. Install the companion **custom integration** (`custom_components/tv_reader/`, via HACS)
   and point it at `http://<ha-host-ip>:8777`.
3. Install the **TV app** (see the
   [app repo](https://github.com/darksider4all/yet-another-samsung-tv-app)) and set the same
   bridge URL on the TV.

### Configuration values

All settings are entered in the wizard and saved to `/data/config.json`. Note the units:

| Setting | Unit / format |
|---|---|
| Splash time (`notify_seconds`) | **seconds** (default 8) |
| TV IP / MAC | IP address / MAC |
| Lights | Home Assistant `entity_id`s |

**Where settings live:** `/data/config.json` on the add-on's own volume, together with the
paired-TV token (`/data/tv_token`) and cast images (`/data/img`). `/data` survives add-on
updates and rebuilds — it is only removed when the add-on is uninstalled. Settings are
never written inside the image or to `/share` (which is network-exported).

`CONFIG_FILE`, `TV_TOKEN_FILE` and `IMG_DIR` can override the three paths; the add-on's
`run.sh` re-imports the environment with `with-contenv` because Home Assistant's
s6-overlay base sweeps it away from the CMD.

`GET /v1/config` reports `config_file` and `persisted`, and `POST /v1/config` answers
**500** if the file cannot be written — so the wizard can no longer claim success while
the settings are being dropped on the floor.

> `addon/reader.py` is a copy of `server/bridge/reader.py` (HA add-ons build from
> their own folder). They are kept identical; edit `server/bridge/reader.py` and copy.
