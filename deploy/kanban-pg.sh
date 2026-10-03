#!/usr/bin/env bash
#
# Idempotent recreate of the kanban-pg container.
#
# The container stores its data on the named volume `kanban-pgdata`, which
# is preserved across `docker rm` + `docker run`. So this script is safe to
# re-run to update restart policy, healthcheck, env vars, or image version.
#
# What this script guarantees, that a bare `docker run` would not:
#
# - restart=always       — comes back on host reboot
# - healthcheck           — restarts the container if postgres stops accepting
#                          connections (not just on host boot)
# - bind to 127.0.0.1     — postgres only listens on the loopback, never on
#                          the LAN; access is via the SSH tunnel/host itself
#
# Run on llmhost2 as root (or any user with passwordless docker):
#
#     sudo /opt/kanban-agent/kanban-agent/deploy/kanban-pg.sh
#
# After running, verify with:
#
#     sudo docker ps --filter name=kanban-pg
#     sudo docker inspect --format '{{.State.Health.Status}}' kanban-pg
#     sudo ss -tlnp | grep 5433
#
set -euo pipefail

NAME="kanban-pg"
IMAGE="pgvector/pgvector:pg16"
VOLUME="kanban-pgdata"
HOST_PORT="5433"
CONTAINER_PORT="5432"
DB_USER="kanban"
DB_PASSWORD="kanban"
DB_NAME="kanban"

HEALTH_CMD='pg_isready -U '"$DB_USER"' -d '"$DB_NAME"' -h 127.0.0.1 || exit 1'

# Idempotent: stop + remove any existing container with this name.
# The volume (data) is NOT removed.
if sudo docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    echo "removing existing container $NAME"
    sudo docker stop "$NAME" >/dev/null 2>&1 || true
    sudo docker rm "$NAME" >/dev/null
fi

# Create the data volume if it does not yet exist (first boot only).
if ! sudo docker volume inspect "$VOLUME" >/dev/null 2>&1; then
    echo "creating volume $VOLUME"
    sudo docker volume create "$VOLUME" >/dev/null
fi

echo "starting $NAME from $IMAGE"
sudo docker run -d \
    --name "$NAME" \
    --restart=always \
    -p 127.0.0.1:"$HOST_PORT":"$CONTAINER_PORT" \
    -v "$VOLUME":/var/lib/postgresql/data \
    -e POSTGRES_USER="$DB_USER" \
    -e POSTGRES_PASSWORD="$DB_PASSWORD" \
    -e POSTGRES_DB="$DB_NAME" \
    --health-cmd "$HEALTH_CMD" \
    --health-interval=10s \
    --health-timeout=5s \
    --health-retries=3 \
    --health-start-period=30s \
    "$IMAGE"

echo "waiting for postgres to be ready..."
for i in $(seq 1 30); do
    if sudo docker exec "$NAME" pg_isready -U "$DB_USER" -d "$DB_NAME" -h 127.0.0.1 >/dev/null 2>&1; then
        echo "ok (took ~${i}s)"
        sudo docker ps --filter "name=$NAME" --format "table {{.Names}}\t{{.Status}}"
        exit 0
    fi
    sleep 1
done
echo "postgres did not become ready in 30s — check 'docker logs $NAME'"
exit 1