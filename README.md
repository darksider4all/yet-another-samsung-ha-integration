<p align="center">
  <img src="brands/icon.png" width="140" alt="Yet Another Samsung Thing">
</p>

<h1 align="center">yet-another-samsung-ha-integration</h1>

<p align="center">
  Local control &amp; rich telemetry for modern Samsung (Tizen) TVs in Home Assistant —
  <b>no Samsung cloud</b>. A custom integration, a small local bridge, and an optional
  on-TV watchdog.
</p>

---

## What's in here

| Part | Path | What it is |
|---|---|---|
| **HA integration** | `custom_components/tv_reader/` | Config-flow custom integration (`media_player` + sensors). Install via HACS. |
| **Bridge** | `server/bridge/` | The local API the TV app and HA talk to. Docker + a browser **setup wizard**. |
| **HA add-on** | `addon/` | The same bridge packaged as a Home Assistant add-on (with ingress + host networking). |
| **Watchdog** | `server/watchdog/` | Optional systemd service that loads the TV app once on power-on. |
| **Brands** | `brands/` | Original logo assets + how to submit them to `home-assistant/brands`. |

Companion TV app (sideloaded Tizen): 👉 **[yet-another-samsung-tv-app](https://github.com/darksider4all/yet-another-samsung-tv-app)**

## Architecture

```
┌────────────┐   HTTP    ┌──────────────┐  local API   ┌──────────────┐
│  TV app    │◄─────────►│   bridge     │◄────────────►│ Home         │
│ (Tizen)    │           │    bridge    │              │ Assistant    │
│            │           │ (this repo)  │              │ integration  │
└────────────┘           └──────────────┘              └──────────────┘
```

Nothing talks to Samsung's cloud. Everything is on your LAN.

## Quick start

### 1. Run the bridge

**Home Assistant add-on** (easiest on HA OS / Supervised): add this repo under
**Settings → Add-ons → Add-on Store → ⋮ → Repositories**, install
*"Yet Another Samsung Thing - Bridge"*, start it, open its Web UI. See [`addon/`](addon/).

**or Docker** anywhere on your LAN:

```bash
cd server/bridge
docker compose up -d --build
# then open the wizard:
#   http://<host-ip>:8777/config
```

> The bridge uses **host networking** so TV auto-discovery (SSDP multicast) works.
> Without it, discovery is off but you can enter the TV IP manually. See
> [`server/bridge/README.md`](server/bridge/README.md).

Complete the wizard: discover the TV, paste your HA long-lived token, pick the lights for
the TV dashboard, and pair the remote (press **Allow** on the TV).

### 2. Install the integration (HACS)

Add this repository as a **HACS custom repository** (category: Integration), install
*"Yet Another Samsung Thing"*, restart HA, then **Settings → Devices & Services → Add
Integration → Yet Another Samsung Thing** and enter the bridge URL
(`http://<host-ip>:8777`). An **options** flow lets you set an optional power-delegate
entity (e.g. a SmartThings switch) for waking the TV from deep standby.

### 3. Install the TV app

Sideload it in Developer Mode — see the
[app repo](https://github.com/darksider4all/yet-another-samsung-tv-app). Set the same
bridge URL on the TV's first-run Setup screen.

## Configuration is all runtime

Nothing is hardcoded. The bridge keeps TV IP/MAC, HA URL/token, and the lights list in
`data/config.json`, editable live from the `/config` wizard or `GET/POST /v1/config`. The
integration's bridge host comes from the config flow. The watchdog reads its host/TV from
an env file. The shipped TV app has no IP baked in.

## Notes &amp; limits

- Power **on from deep standby**, discrete source switching, picture controls, and screen
  capture are privilege-gated on modern Tizen and are intentionally not attempted here.
  Local power-off and light-standby power-on via the remote key work after pairing.
- The in-HA integration logo needs a PR to
  [`home-assistant/brands`](https://github.com/home-assistant/brands) (see [`brands/`](brands/)).
- Unofficial hobby project, not affiliated with Samsung. Use at your own risk.

## License

MIT — see [LICENSE](LICENSE).
