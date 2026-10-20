# Brand assets

Original artwork for this integration (adapted from the project's own icon — no
third-party logos). In Home Assistant, the integration's real logo is served from the
central [`home-assistant/brands`](https://github.com/home-assistant/brands) repository,
keyed by the integration **domain** (`tv_reader`). A custom integration can't inject its
own logo into HA directly, so to get the logo to show in the UI you submit these files
there.

## Files

| File | Size | Purpose |
|---|---|---|
| `icon.png` / `icon@2x.png` | 256 / 512 | square icon |
| `logo.png` / `logo@2x.png` | 256 / 512 | logo |
| `dark_icon.png` / `dark_icon@2x.png` | 256 / 512 | dark-theme icon |

## Submit to home-assistant/brands

Place them under `custom_integrations/tv_reader/` in a fork of `home-assistant/brands`:

```
custom_integrations/tv_reader/icon.png
custom_integrations/tv_reader/icon@2x.png
custom_integrations/tv_reader/logo.png
custom_integrations/tv_reader/logo@2x.png
custom_integrations/tv_reader/dark_icon.png
custom_integrations/tv_reader/dark_icon@2x.png
```

Open a PR. Until it merges, HA shows the default puzzle-piece icon — everything still works.
