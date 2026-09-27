#!/bin/sh
# Preflight for kanban-web.
#
# systemd performs ${VAR} substitution itself, with no shell involved. That
# makes a validation one-liner in the unit ambiguous: a `$$` that is correct
# inside ExecStartPre=/bin/sh -c is wrong in ExecStart, and getting it backwards
# hands gunicorn a literal string. Keeping the check in a file removes the
# question entirely.
#
# The board has no authentication of its own, so binding every interface is
# never the right answer. A missing variable is treated the same way: an empty
# --bind makes gunicorn fall back to its own default, 0.0.0.0:8000.
set -eu

BIND="${BIND_ADDR:-}"

if [ -z "$BIND" ]; then
  echo "kanban-web: BIND_ADDR is not set." >&2
  echo "  Set it in .env, for example: BIND_ADDR=127.0.0.1:8901" >&2
  echo "  For LAN access bind the LAN address explicitly, never 0.0.0.0." >&2
  exit 1
fi

case "$BIND" in
  0.0.0.0:*)
    echo "kanban-web: refusing to bind ${BIND}." >&2
    echo "  The board has no auth of its own; bind a specific interface." >&2
    exit 1
    ;;
  *:*) : ;;
  *)
    echo "kanban-web: BIND_ADDR must be HOST:PORT, got '${BIND}'." >&2
    exit 1
    ;;
esac

HOST=${BIND%:*}
PORT=${BIND##*:}

case "$PORT" in
  ''|*[!0-9]*) echo "kanban-web: '${PORT}' is not a port number." >&2; exit 1 ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo "kanban-web: port ${PORT} out of range." >&2
  exit 1
fi

if ! command -v ss >/dev/null 2>&1; then
  echo "kanban-web: preflight ok (${BIND}); 'ss' unavailable, skipping port check"
  exit 0
fi

if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qx "$HOST:$PORT"; then
  echo "kanban-web: ${HOST}:${PORT} is already in use by another process." >&2
  echo "  Pick a different BIND_ADDR, or stop whatever holds the port." >&2
  exit 1
fi

echo "kanban-web: preflight ok — binding ${BIND}"
