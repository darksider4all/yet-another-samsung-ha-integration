# Yet Another Samsung Thing

Local control + rich telemetry for modern Samsung (Tizen) TVs in Home Assistant —
no Samsung cloud. A `media_player` plus diagnostic sensors, backed by a small local
bridge and a companion sideloaded TV app.

- **media_player**: power (local), volume/mute, channel, launch apps, now-playing with artwork.
- **sensors**: current channel + EPG program, foreground app, firmware/model, network, memory/uptime.
- **lights on the TV**: toggle your HA lights from an on-screen dashboard.
- **toast & cast**: push a message, image, video, or web page to the screen from HA.

Install the bridge (HA add-on or Docker), then this integration via HACS, then the
[TV app](https://github.com/darksider4all/yet-another-samsung-tv-app). The bridge's
setup wizard walks you through TV discovery, the HA token, lights, and remote pairing.
