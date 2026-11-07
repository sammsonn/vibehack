#!/bin/sh
set -e

# Google refreshes token.json in place, so it has to live in the writable data volume.
# On first start, seed it from the read-only secret mounted by docker-compose.yml.
if [ -n "$GOOGLE_TOKEN_PATH" ] && [ ! -f "$GOOGLE_TOKEN_PATH" ] && [ -f /secrets/token.json ]; then
    cp /secrets/token.json "$GOOGLE_TOKEN_PATH"
fi

exec "$@"
