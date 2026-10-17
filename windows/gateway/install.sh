#!/usr/bin/env bash
# Install / update the publish gateway inside the ServerKit distro (as root).
#   install.sh            install or update, keep the existing token
#   install.sh rotate     replace the token (the old one stops working at once)
#   install.sh token      print the current token
#   install.sh off        stop the gateway and remove its public route
# Also installs serverkit-netlink (keeps site containers on the database network).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB=/usr/local/lib/serverkit-gateway
TOKEN=/etc/serverkit-gateway/token
NETLINKS=/etc/serverkit-gateway/netlinks
UNIT=serverkit-gateway
NETUNIT=serverkit-netlink
HOST=deploy.tadadigital.shop
PORT=8711

new_token() {
    mkdir -p --mode=0700 "$(dirname "$TOKEN")"
    ( umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN" )
}

case "${1:-install}" in
    token) cat "$TOKEN"; exit 0 ;;
    rotate) new_token; systemctl restart "$UNIT"; echo "token replaced"; exit 0 ;;
    off)
        systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
        echo "gateway stopped (the DNS name stays; the tunnel answers 502 for it)"
        exit 0 ;;
    install) ;;
    *) echo "usage: $0 [install|rotate|token|off]" >&2; exit 2 ;;
esac

mkdir -p "$LIB"
install -m 644 "$SRC/gateway.py" "$LIB/gateway.py"
[ -s "$TOKEN" ] || new_token

# netlink: keeps site containers connected to the database network
install -m 755 "$SRC/netlink.sh" /usr/local/sbin/serverkit-netlink
mkdir -p --mode=0700 "$(dirname "$NETLINKS")"
[ -f "$NETLINKS" ] || ( umask 077; : > "$NETLINKS" )
chmod 600 "$NETLINKS"

cat > "/etc/systemd/system/$NETUNIT.service" <<EOF
[Unit]
Description=ServerKit netlink (site containers join the services network)
After=docker.service
Wants=docker.service

[Service]
ExecStart=/usr/local/sbin/serverkit-netlink watch
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > "/etc/systemd/system/$UNIT.service" <<EOF
[Unit]
Description=ServerKit publish gateway (https://$HOST)
After=network-online.target serverkit.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 $LIB/gateway.py
Environment=GATEWAY_PORT=$PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$UNIT" "$NETUNIT" >/dev/null 2>&1
systemctl restart "$NETUNIT"
systemctl restart "$UNIT"
sleep 2
for _ in 1 2 3 4 5 6 7 8 9 10; do systemctl is-active --quiet "$UNIT" && break; sleep 1; done
systemctl is-active "$UNIT"
systemctl is-active "$NETUNIT"
/usr/local/sbin/serverkit-domain-tunnel add tada "$HOST=$PORT" >/dev/null
echo "gateway on https://$HOST"
