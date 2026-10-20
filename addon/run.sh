#!/usr/bin/with-contenv sh
# Home Assistant base images ship s6-overlay with S6_KEEP_ENV unset, which sweeps
# the container environment into /run/s6/container_environment and empties it for
# the CMD. Without re-importing it here, every ENV declared in the Dockerfile is
# invisible to this script and to reader.py — which is how the bridge silently
# ended up writing its config inside the image.
set -eu

# Persistent storage. /data is the add-on's own volume: it survives updates and
# rebuilds and is only removed when the add-on is uninstalled. Never point these
# at a path inside the image (/app/...): that is wiped on every container
# recreate, taking the wizard config and the TV pairing token with it.
: "${CONFIG_FILE:=/data/config.json}"
: "${TV_TOKEN_FILE:=/data/tv_token}"
: "${IMG_DIR:=/data/img}"
export CONFIG_FILE TV_TOKEN_FILE IMG_DIR

echo "[yast-bridge] starting bridge on :8777 (config=$CONFIG_FILE, token=$TV_TOKEN_FILE, img=$IMG_DIR)"
exec python3 /app/reader.py
