#!/usr/bin/env bash
# Install / update the publish gateway inside the ServerKit distro (as root).
#   install.sh            install or update, keep the existing token
#   install.sh rotate     replace the token (the old one stops working at once)
#   install.sh token      print the current token
#   install.sh off        stop the gateway and remove its public route
# Also installs serverkit-netlink (keeps site containers on the database network).
# Owner values (domain, tunnel, ports, gateway mode) come from windows/serverkit-windows.json.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/config.sh
. "$SRC/../lib/config.sh"
LIB=/usr/local/lib/serverkit-gateway
TOKEN=/etc/serverkit-gateway/token
NETLINKS=/etc/serverkit-gateway/netlinks
UNIT=serverkit-gateway
NETUNIT=serverkit-netlink
HOST=""
PORT=""
CONFIG_FILE="${SERVERKIT_WINDOWS_CONFIG:-$SWK_DIR/serverkit-windows.json}"

new_token() {
    mkdir -p --mode=0700 "$(dirname "$TOKEN")"
    ( umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN" )
}

# token / rotate / off do not need the config; install does.
case "${1:-install}" in
    install) swk_require_config
             HOST="$(swk_cfg gateway_host)"; PORT="$(swk_cfg gateway_port)"
             TUNNEL="$(swk_cfg tunnel)"; DOMAIN="$(swk_cfg domain)"
             WILDCARD="$(swk_cfg wildcard)"; MODE="$(swk_cfg gateway_mode)" ;;
esac

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
# public address, read by the panel extension (Settings > API card)
mkdir -p --mode=0700 "$(dirname "$TOKEN")"
echo "https://$HOST" > "$(dirname "$TOKEN")/public_url"
install -m 644 "$SRC/gateway.py" "$LIB/gateway.py"
install -m 644 "$SWK_DIR/swk_config.py" "$LIB/swk_config.py"
# the tunnel helper (domain-tunnel.sh) is what publish.py and the lines below call
install -m 755 "$SWK_DIR/domain-tunnel.sh" /usr/local/sbin/serverkit-domain-tunnel
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
Description=ServerKit publish gateway (https://$HOST, mode $MODE)
After=network-online.target serverkit.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 $LIB/gateway.py
Environment=GATEWAY_PORT=$PORT
Environment=SERVERKIT_WINDOWS_CONFIG=$CONFIG_FILE
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
# The gateway host keeps its own ingress rule (specific rules win over the wildcard);
# with a wildcard record its DNS name is already covered.
if [ "$WILDCARD" = true ]; then
    SERVERKIT_TUNNEL_SKIP_DNS=1 /usr/local/sbin/serverkit-domain-tunnel add "$TUNNEL" "$HOST=$PORT" >/dev/null
    /usr/local/sbin/serverkit-domain-tunnel wildcard "$DOMAIN" "$TUNNEL" >/dev/null
else
    /usr/local/sbin/serverkit-domain-tunnel add "$TUNNEL" "$HOST=$PORT" >/dev/null
fi
echo "gateway on https://$HOST (mode $MODE)"
