#!/usr/bin/env bash
# qrstatus.sh — one-glance health report for the QoderRoute proxy that backs
# the DSH provider "qoder-route" (Qoder (Route) models).
#
# Usage: qrstatus.sh [--probe]
#   --probe  also send a real chat completion (costs a little quota, slow)
set -uo pipefail

PORT=8010
SIGNER_PORT=8123
PROBE=0
[[ "${1:-}" == "--probe" ]] && PROBE=1

green=$'\033[32m'; red=$'\033[31m'; yellow=$'\033[33m'; dim=$'\033[2m'; off=$'\033[0m'

ok()   { printf '  %s✓%s %s\n' "$green" "$off" "$1"; }
bad()  { printf '  %s✗%s %s\n' "$red" "$off" "$1"; }
warn() { printf '  %s!%s %s\n' "$yellow" "$off" "$1"; }

echo "QoderRoute status"
echo "─────────────────"

# 1. systemd unit
if systemctl --user is-active --quiet qoder-route.service; then
    ok "service active (MainPID $(systemctl --user show qoder-route.service -p MainPID --value))"
else
    bad "service NOT active — run: systemctl --user start qoder-route.service"
fi
systemctl --user is-enabled --quiet qoder-route.service \
    && ok "enabled at boot" || bad "NOT enabled at boot"

# 2. proxy health (this already encodes the signer check: 503 = degraded)
CODE=$(curl -s -o /tmp/.qr_h.json -w '%{http_code}' --max-time 10 "http://127.0.0.1:$PORT/api/health" 2>/dev/null || echo 000)
case "$CODE" in
    200) ok "http://127.0.0.1:$PORT/api/health → 200 $(cat /tmp/.qr_h.json 2>/dev/null)" ;;
    503) bad "proxy up but DEGRADED (signer down): $(cat /tmp/.qr_h.json 2>/dev/null)" ;;
    000) bad "http://127.0.0.1:$PORT unreachable (connection refused)" ;;
    *)   warn "unexpected HTTP $CODE from /api/health" ;;
esac

# 3. signer sidecar
if curl -sf --max-time 5 "http://127.0.0.1:$SIGNER_PORT/health" >/dev/null 2>&1; then
    ok "signer sidecar on :$SIGNER_PORT healthy ($(pgrep -f signer_server.mjs | head -1))"
else
    bad "signer sidecar on :$SIGNER_PORT NOT healthy"
fi

# 4. watchdog timer
if systemctl --user is-active --quiet qoder-route-health.timer; then
    ok "health watchdog timer active"
    systemctl --user list-timers qoder-route-health.timer --no-pager 2>/dev/null | sed -n '2p' | sed "s/^/    ${dim}/;s/$/${off}/"
else
    bad "health watchdog timer NOT active"
fi

# 5. optional real completion
if (( PROBE )); then
    echo "  ${dim}probing a real completion (model=lite)…${off}"
    OUT=$(curl -s --max-time 120 "http://127.0.0.1:$PORT/v1/chat/completions" \
        -H 'Content-Type: application/json' \
        -H 'Authorization: Bearer local-qoder-route-any-value' \
        -d '{"model":"lite","messages":[{"role":"user","content":"reply with exactly: PING"}],"max_tokens":16,"stream":false}' 2>/dev/null)
    if grep -q '"content"' <<<"$OUT"; then
        ok "completion OK: $(sed -n 's/.*"content":"\([^"]*\)".*/\1/p' <<<"$OUT" | head -1)"
    else
        bad "completion FAILED: $(head -c 200 <<<"$OUT")"
    fi
fi

echo
echo "DSH mapping: provider 'qoder-route' → http://127.0.0.1:$PORT/v1"
echo "If DSH shows 'Connection error.' for Qoder (Route), this proxy is the cause."
