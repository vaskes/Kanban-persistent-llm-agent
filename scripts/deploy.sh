#!/usr/bin/env bash
# Deploy the working tree to the host without clobbering local configuration.
#
# .env is deliberately NOT shipped: it holds the bind address, credentials and
# the listen interface for one specific host. A tarball that includes it will
# silently overwrite the host's real configuration on the next deploy.
set -euo pipefail

TREE="${1:-kanban-agent}"
TARBALL="/tmp/deploy-$(basename "$TREE").tgz"

tar czf "$TARBALL" \
    --exclude='.git' --exclude='__pycache__' --exclude='.venv' \
    --exclude='.env' --exclude='*.pyc' --exclude='staticfiles' \
    -C "$(dirname "$TREE")" "$(basename "$TREE")"

echo "built $TARBALL (no .env)"
echo "  extract on the host, then:"
echo "    sudo install -m 644 deploy/kanban-web.service /etc/systemd/system/"
echo "    sudo systemctl restart kanban-web"
