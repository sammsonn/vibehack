FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RADAR_HOME=/app \
    RADAR_DB_PATH=/data/radar.db \
    GOOGLE_TOKEN_PATH=/data/token.json \
    RADAR_TEAM_FILE=/secrets/team.json \
    GITHUB_APP_KEY_PATH=/secrets/github-app.pem \
    GOOGLE_CREDENTIALS_PATH=/secrets/credentials.json

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY radar ./radar
COPY scripts ./scripts
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# No USER line: the entrypoint starts as root only to fix /data ownership, then runs Radar as this user.
RUN useradd --system --uid 10001 --home-dir /app radar \
    && mkdir -p /data \
    && chown radar:radar /data \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

# radar.db, the Google token (refreshed in place), the instance lock and the heartbeat.
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=15s --start-period=90s --retries=3 \
    CMD ["setpriv", "--reuid=radar", "--regid=radar", "--init-groups", "python", "-m", "radar", "health"]

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "radar", "run"]
