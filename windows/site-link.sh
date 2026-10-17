#!/usr/bin/env bash
# Public link for one hosted site through its own Cloudflare quick tunnel.
#
# Same mechanism as remote-link.sh (no account, HTTPS, random
# https://*.trycloudflare.com address that changes whenever the tunnel
# restarts), but the tunnel sends the site's local hostname as the Host
# header, so nginx routes it to that site's vhost.
#
# Usage (as root inside the distro):
#   site-link.sh on  <name> <local-host>   e.g. on safranbolu safranbolu.localhost
#   site-link.sh off <name>
#   site-link.sh url <name>

set -euo pipefail

name="${2:-}"
[ -n "$name" ] || { echo "usage: $0 on|off|url <name> [local-host]" >&2; exit 2; }
case "$name" in *[!a-z0-9-]*) echo "invalid name: $name" >&2; exit 2 ;; esac

UNIT="serverkit-site-$name"
LOG="/var/log/$UNIT.log"
URL_FILE="/var/lib/serverkit/site-url-$name"

wait_url() {
    local url=""
    for _ in $(seq 1 60); do
        url="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" 2>/dev/null | tail -n 1 || true)"
        [ -n "$url" ] && break
        sleep 2
    done
    [ -n "$url" ] || { echo "tunnel address not found in $LOG" >&2; return 1; }
    mkdir -p "$(dirname "$URL_FILE")"
    printf '%s\n' "$url" > "$URL_FILE"
    echo "$url"
}

on() {
    local host="${1:?local host required}"
    command -v cloudflared >/dev/null 2>&1 || { echo "cloudflared missing: run remote-link.sh on first" >&2; exit 1; }
    cat > "/etc/systemd/system/$UNIT.service" <<EOF
[Unit]
Description=Public link for site $name (Cloudflare quick tunnel)
After=network-online.target nginx.service
Wants=network-online.target

[Service]
ExecStartPre=/bin/sh -c ': > $LOG'
ExecStart=/bin/sh -c 'exec /usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:80 --http-host-header $host >> $LOG 2>&1'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "$UNIT" >/dev/null 2>&1
    if ! systemctl is-active --quiet "$UNIT"; then
        systemctl restart "$UNIT"
    fi
    wait_url
}

case "${1:-}" in
    on) on "${3:-}" ;;
    off)
        systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
        rm -f "$URL_FILE" "/etc/systemd/system/$UNIT.service"
        systemctl daemon-reload
        echo "off" ;;
    url)
        if systemctl is-active --quiet "$UNIT"; then wait_url; else echo "off"; fi ;;
    *) echo "usage: $0 on|off|url <name> [local-host]" >&2; exit 2 ;;
esac
