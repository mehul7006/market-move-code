#!/bin/sh
# Prepare the persistent data folder, then run the app as the unprivileged "app" user.
set -e
mkdir -p "${CACHE_DIR:-/data/cache}" "$(dirname "${DESK_ENV_FILE:-/data/desk.env}")"
chown -R app:app /data 2>/dev/null || true
exec runuser -u app -- "$@"
