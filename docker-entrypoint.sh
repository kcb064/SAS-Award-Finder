#!/usr/bin/env bash
# Seed the config volume from the image's baked-in defaults on first run, then start the app.
# This lets you mount an (initially empty) ./config volume and still get points_table.yaml, while
# keeping any file you later edit there.
set -euo pipefail

CONFIG_DIR="${AF_CONFIG_DIR:-/config}"
mkdir -p "$CONFIG_DIR" "${AF_DATA_DIR:-/data}"

if [ ! -f "$CONFIG_DIR/points_table.yaml" ] && [ -f /app/config-default/points_table.yaml ]; then
  echo "[entrypoint] seeding $CONFIG_DIR from image defaults"
  cp -n /app/config-default/points_table.yaml "$CONFIG_DIR/points_table.yaml"
fi

exec "$@"
