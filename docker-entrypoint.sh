#!/bin/sh
set -e

# Google refreshes token.json in place, so it has to live in the writable data volume.
# On first start, seed it from the read-only secret mounted by docker-compose.yml.
if [ -n "$GOOGLE_TOKEN_PATH" ] && [ ! -f "$GOOGLE_TOKEN_PATH" ] && [ -f /secrets/token.json ]; then
    cp /secrets/token.json "$GOOGLE_TOKEN_PATH"
fi

# Started as root only to repair ownership of the data volume: files copied in with `docker compose cp`
# (e.g. an existing radar.db) keep the host's uid and would be read-only for Radar. Then drop privileges.
if [ "$(id -u)" = "0" ]; then
    chown -R radar:radar /data
    exec setpriv --reuid=radar --regid=radar --init-groups "$@"
fi

exec "$@"
