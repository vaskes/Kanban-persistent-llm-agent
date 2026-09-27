#!/usr/bin/env bash
# End-to-end smoke test against a live server.
#
# The board requires a session, so this logs in first and keeps the cookie.
# Credentials come from the environment and default to the operator account
# created during setup.
#
#   USERNAME=vaskes PASSWORD=... bash scripts/smoke.sh
set -euo pipefail

# Resolve paths relative to the repository, not the caller's working directory.
# Reading .env from $PWD meant the script silently fell back to loopback and
# reported 000 (which reads as an outage) when run from anywhere else.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BASE="${BASE:-}"
if [ -z "$BASE" ]; then
  # Take the address the service is actually bound to. Guessing 127.0.0.1 fails
  # once BIND_ADDR is set to a LAN address, and the failure looks like an
  # outage rather than a wrong address.
  if [ -f "$HERE/.env" ]; then
    BIND=$(grep -E '^BIND_ADDR=' "$HERE/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"'')
  fi
  BASE="http://${BIND:-127.0.0.1:8901}"
fi
# Credentials are required, never defaulted.
#
# A default password baked into a script is how the account below ended up
# with a password nobody but the script knew. Smoke needs real credentials or
# it cannot check the half of the surface that matters.
USERNAME="${USERNAME:-}"
PASSWORD="${PASSWORD:-}"
if [ -z "$USERNAME" ] || [ -z "$PASSWORD" ]; then
  echo "smoke: USERNAME and PASSWORD must be set." >&2
  echo "  e.g.  USERNAME=vaskes PASSWORD=... bash scripts/smoke.sh" >&2
  exit 2
fi
JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

fail=0
pass() { echo "  ok   $1"; }
bad()  { echo "  FAIL $1 — $2"; fail=1; }

echo "smoke: $BASE as $USERNAME"

# --- unauthenticated surface -------------------------------------------
code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$BASE/healthz" || true)
if [ "$code" = "200" ]; then pass "healthz (open to probes)"; else bad "healthz" "got $code"; fi

for p in / /reports/; do
  code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$BASE$p" || true)
  if [ "$code" = "302" ]; then pass "$p redirects anonymous to login"; else bad "$p" "expected 302, got $code"; fi
done

# --- login --------------------------------------------------------------
curl -s -m 5 -c "$JAR" -o /dev/null "$BASE/login/"
token=$(curl -s -m 5 -b "$JAR" -c "$JAR" "$BASE/login/" \
        | grep -oP 'name="csrfmiddlewaretoken" value="\K[^"]+' | head -1)

if [ -z "$token" ]; then
  bad "login" "could not read csrf token"
else
  code=$(curl -s -m 5 -b "$JAR" -c "$JAR" -o /dev/null -w '%{http_code}' \
         -e "$BASE/login/" \
         -d "csrfmiddlewaretoken=$token&username=$USERNAME&password=$PASSWORD" \
         "$BASE/login/" || true)
  if [ "$code" = "302" ]; then pass "login accepted"; else bad "login" "got $code"; fi
fi

# --- authenticated surface ---------------------------------------------
check_auth() {
  local name="$1" url="$2" expect="$3"
  local body; body=$(curl -s -m 5 -b "$JAR" "$url" || true)
  if echo "$body" | grep -q "$expect"; then pass "$name"; else bad "$name" "expected '$expect'"; fi
}

check_auth "board renders"    "$BASE/"          "Ready"
check_auth "reports renders"  "$BASE/reports/"  "reports"

# Once a session exists, /admin/login/ redirects to the index, so checking for
# the word "password" in its body is wrong. Check reachability instead: a
# superuser session must get the admin index, not a login page.
code=$(curl -s -m 5 -b "$JAR" -o /dev/null -w '%{http_code}' "$BASE/admin/" || true)
if [ "$code" = "200" ]; then pass "admin reachable with session"; else bad "admin reachable" "got $code"; fi

# --- registration exists; password recovery does not ---------------------
code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$BASE/register/" || true)
if [ "$code" = "200" ]; then pass "registration available"; else bad "registration" "expected 200, got $code"; fi

code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$BASE/password_reset/" || true)
if [ "$code" = "404" ]; then pass "no password reset"; else bad "password reset" "expected 404, got $code"; fi

if [ "$fail" -eq 0 ]; then echo "smoke: PASS"; else echo "smoke: FAIL"; exit 1; fi
