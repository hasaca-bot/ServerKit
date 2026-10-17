#!/usr/bin/env bash
# Remote access to the panel through a Cloudflare quick tunnel.
#
# A quick tunnel needs no Cloudflare account and no router changes: cloudflared
# dials out to Cloudflare, which hands back a random https://*.trycloudflare.com
# address. The address is new every time the tunnel process starts, so a
# watcher reads it from the tunnel's log and tells the panel about it
# (SERVERKIT_PUBLIC_URL), which is what lets the browser's realtime connection
# through the panel's origin check.
#
# Usage (as root inside the distro):
#   remote-link.sh on | off | url | apply-url

set -euo pipefail

UNIT=serverkit-remote
LOG=/var/log/serverkit-remote.log
URL_FILE=/var/lib/serverkit/remote-url
ENV_FILE=/opt/serverkit/.env
SELF="$(readlink -f "$0")"

install_cloudflared() {
    command -v cloudflared >/dev/null 2>&1 && return 0
    mkdir -p --mode=0755 /usr/share/keyrings
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
        > /etc/apt/sources.list.d/cloudflared.list
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cloudflared >/dev/null
}

# Point the panel at the current public address (idempotent).
apply_url() {
    local url
    for _ in $(seq 1 60); do
        url="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" 2>/dev/null | tail -n 1 || true)"
        [ -n "$url" ] && break
        sleep 2
    done
    [ -n "${url:-}" ] || { echo "tunnel address not found in $LOG" >&2; return 1; }
    mkdir -p "$(dirname "$URL_FILE")"
    printf '%s\n' "$url" > "$URL_FILE"
    if grep -q "^SERVERKIT_PUBLIC_URL=$url\$" "$ENV_FILE"; then
        return 0
    fi
    if grep -q '^SERVERKIT_PUBLIC_URL=' "$ENV_FILE"; then
        sed -i "s|^SERVERKIT_PUBLIC_URL=.*|SERVERKIT_PUBLIC_URL=$url|" "$ENV_FILE"
    else
        printf 'SERVERKIT_PUBLIC_URL=%s\n' "$url" >> "$ENV_FILE"
    fi
    systemctl restart serverkit
}

on() {
    install_cloudflared
    cat > "/etc/systemd/system/$UNIT.service" <<EOF
[Unit]
Description=ServerKit panel remote link (Cloudflare quick tunnel)
After=network-online.target nginx.service
Wants=network-online.target

[Service]
ExecStartPre=/bin/sh -c ': > $LOG'
ExecStart=/bin/sh -c 'exec /usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:80 >> $LOG 2>&1'
ExecStartPost=/bin/bash $SELF apply-url
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "$UNIT" >/dev/null 2>&1
    systemctl restart "$UNIT"
    cat "$URL_FILE"
}

off() {
    systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
    rm -f "$URL_FILE"
    if grep -q '^SERVERKIT_PUBLIC_URL=https://[a-z0-9-]*\.trycloudflare\.com' "$ENV_FILE"; then
        sed -i '/^SERVERKIT_PUBLIC_URL=https:\/\/[a-z0-9-]*\.trycloudflare\.com/d' "$ENV_FILE"
        systemctl restart serverkit
    fi
    echo "remote link off"
}

url() {
    # A permanent address (named tunnel) set by hand wins over the quick tunnel.
    local fixed
    fixed="$(sed -n 's/^SERVERKIT_PUBLIC_URL=\(https:\/\/.*\)$/\1/p' "$ENV_FILE" | grep -v trycloudflare | tail -n 1 || true)"
    if [ -n "$fixed" ]; then
        echo "$fixed"
    elif systemctl is-active --quiet "$UNIT" && [ -f "$URL_FILE" ]; then
        cat "$URL_FILE"
    else
        echo "remote link off"
    fi
}

case "${1:-}" in
    on) on ;;
    off) off ;;
    url) url ;;
    apply-url) apply_url ;;
    *) echo "usage: $0 on|off|url" >&2; exit 2 ;;
esac
