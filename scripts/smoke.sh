#!/usr/bin/env bash
# End-to-end smoke test. Assumes the web server is on 127.0.0.1:8901.
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8901}"
fail=0
check() {
  local name="$1" url="$2" expect="$3"
  local body; body=$(curl -s -m 5 "$url" || true)
  if echo "$body" | grep -q "$expect"; then
    echo "  ok   $name"
  else
    echo "  FAIL $name (expected '$expect' from $url)"
    fail=1
  fi
}
echo "smoke: $BASE"
check "healthz"        "$BASE/healthz"      '"ok": true'
check "board renders"  "$BASE/"             "Ready"
check "admin"          "$BASE/admin/login/" "password"
check "reports"        "$BASE/reports/"     "reports"
if [ $fail -eq 0 ]; then echo "smoke: PASS"; else echo "smoke: FAIL"; exit 1; fi
