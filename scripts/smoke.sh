#!/usr/bin/env bash
# End-to-end smoke test against a live server.
#
# The board requires a session, so this logs in first and keeps the cookie.
# Credentials come from the environment and default to the operator account
# created during setup.
#
#   USERNAME=vaskes PASSWORD=... bash scripts/smoke.sh
set -euo pipefail

BASE="${BASE:-}"
if [ -z "$BASE" ]; then
  # Take the address the service is actually bound to. Guessing 127.0.0.1 fails
  # once BIND_ADDR is set to a LAN address, and the failure looks like an
  # outage rather than a wrong address.
  if [ -f .env ]; then
    BIND=$(grep -E '^BIND_ADDR=' .env | tail -1 | cut -d= -f2- | tr -d '"'"'"'')
  fi
  BASE="http://${BIND:-127.0.0.1:8901}"
fi
USERNAME="${USERNAME:-vaskes}"
PASSWORD="${PASSWORD:-kanbanadmin}"
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
check_auth "admin reachable"  "$BASE/admin/login/" "password"

# --- registration must not exist ----------------------------------------
for p in /register/ /signup/ /password_reset/; do
  code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$BASE$p" || true)
  if [ "$code" = "404" ]; then pass "no $p"; else bad "no $p" "expected 404, got $code"; fi
done

if [ "$fail" -eq 0 ]; then echo "smoke: PASS"; else echo "smoke: FAIL"; exit 1; fi
