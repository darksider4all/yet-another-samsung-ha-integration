#!/usr/bin/env sh
set -e
mkdir -p /share/yast-bridge/img
echo "[yast-bridge] starting bridge on :8777 (config=$CONFIG_FILE)"
exec python3 /app/reader.py
