#!/usr/bin/env bash
# Permanent public addresses through one named Cloudflare Tunnel.
#
# Needs a one-time `cloudflared tunnel login` (the owner authorises the zone
# in the browser; the certificate lands in /root/.cloudflared/cert.pem). One
# tunnel carries every hostname: each hostname gets a proxied CNAME to the
# tunnel in Cloudflare DNS (replacing whatever it pointed at before) and an
# ingress rule sending it to a local port. Cloudflare terminates HTTPS with
# its free certificate (apex + one subdomain level).
#
# Sites are reached on their container port directly, so the app sees
# cloudflared's X-Forwarded-Proto: https. Port 80 means "through nginx" (the
# panel, or anything routed by Host there).
#
# Wildcard mode: `wildcard <domain>` routes *.<domain> to local nginx (port 80)
# once - one ingress rule plus one wildcard DNS CNAME - so publishing a site
# needs no per-site DNS record or ingress rule any more; nginx picks the site by
# its Host header (the panel's domain record). Specific hostnames added with
# `add` keep priority (they are written before the wildcard). The free
# Cloudflare certificate covers one subdomain level: <site>.<domain>.
#
# Usage (as root inside the distro):
#   domain-tunnel.sh up <name> <hostname>=<port> [<hostname>=<port> ...]
#       Replaces the tunnel's whole hostname list with the given one.
#   domain-tunnel.sh add <name> <hostname>=<port>   (keeps the others)
#   domain-tunnel.sh remove <name> <hostname>
#   domain-tunnel.sh wildcard <domain> [<name>]     (name defaults to $SERVERKIT_TUNNEL)
#   domain-tunnel.sh wildcard-off <domain> [<name>] (removes the rule; the DNS record stays)
#   domain-tunnel.sh status <name>
#   domain-tunnel.sh down <name>          (stops it; DNS records stay)
#
# SERVERKIT_TUNNEL_SKIP_DNS=1 writes the ingress rules but creates no DNS
# records (used when a wildcard record already covers the hostname).

set -euo pipefail

usage() { echo "usage: $0 up|add|remove|status|down <name> ... | wildcard|wildcard-off <domain> [<name>]" >&2; exit 2; }

cmd="${1:-}"; domain=""
case "$cmd" in
    wildcard|wildcard-off)
        domain="${2:-}"; name="${3:-${SERVERKIT_TUNNEL:-}}"
        case "$domain" in ''|.*|*.|*..*|*[!a-z0-9.-]*) echo "invalid domain: $domain" >&2; exit 2 ;; esac
        [ -n "$name" ] || { echo "tunnel name required (third argument or \$SERVERKIT_TUNNEL)" >&2; exit 2; } ;;
    *) name="${2:-}" ;;
esac
[ -n "$cmd" ] && [ -n "$name" ] || usage
case "$name" in *[!a-z0-9-]*) echo "invalid name: $name" >&2; exit 2 ;; esac

DIR=/etc/cloudflared
CONF="$DIR/$name.yml"
UNIT="serverkit-tunnel-$name"

tunnel_id() {
    cloudflared tunnel list -o json 2>/dev/null | python3 -c '
import json, sys
for t in json.load(sys.stdin) or []:
    if t.get("name") == sys.argv[1] and str(t.get("deleted_at") or "0001").startswith("0001"):
        print(t["id"]); break' "$name"
}

up() {
    [ "$#" -gt 0 ] || { echo "at least one hostname=port required" >&2; exit 2; }
    [ -f /root/.cloudflared/cert.pem ] || { echo "run 'cloudflared tunnel login' first" >&2; exit 1; }
    local pair host port pass
    for pair in "$@"; do
        host="${pair%%=*}"; port="${pair#*=}"
        # a leading "*." marks a wildcard hostname (*.example.com)
        case "${host#\*.}" in ''|*[!a-z0-9.-]*) echo "invalid hostname: $host" >&2; exit 2 ;; esac
        case "$port" in ''|*[!0-9]*) echo "invalid port for $host: $port" >&2; exit 2 ;; esac
    done
    mkdir -p "$DIR"

    local id
    id="$(tunnel_id)"
    if [ -z "$id" ]; then
        cloudflared tunnel create "$name" >/dev/null
        id="$(tunnel_id)"
    fi
    local creds="/root/.cloudflared/$id.json"
    [ -f "$creds" ] || { echo "credentials file $creds missing for tunnel $name" >&2; exit 1; }

    local tmp="$CONF.new"
    {
        echo "tunnel: $id"
        echo "credentials-file: $creds"
        echo "ingress:"
        # Specific hostnames first, wildcards after them: cloudflared uses the first match.
        for pass in specific wildcard; do
            for pair in "$@"; do
                case "${pair%%=*}" in
                    \*.*) [ "$pass" = wildcard ] || continue ;;
                    *)     [ "$pass" = specific ] || continue ;;
                esac
                echo "  - hostname: \"${pair%%=*}\""
                echo "    service: http://127.0.0.1:${pair#*=}"
            done
        done
        echo "  - service: http_status:404"
    } > "$tmp"
    cloudflared tunnel ingress validate --config "$tmp" >/dev/null
    mv "$tmp" "$CONF"

    if [ "${SERVERKIT_TUNNEL_SKIP_DNS:-}" != 1 ]; then
        for pair in "$@"; do
            cloudflared tunnel route dns --overwrite-dns "$id" "${pair%%=*}" 2>&1 | grep -o 'Added CNAME.*tunnel\|already configured.*' || true
        done
    fi

    cat > "/etc/systemd/system/$UNIT.service" <<EOF
[Unit]
Description=Cloudflare Tunnel $name
After=network-online.target docker.service nginx.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/cloudflared --no-autoupdate tunnel --config $CONF run
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "$UNIT" >/dev/null 2>&1
    systemctl restart "$UNIT"
    sleep 5
    echo "$UNIT: $(systemctl is-active "$UNIT")"
}

# Add (or re-point) one hostname without retyping the whole list.
add() {
    local pair="${1:?hostname=port required}" want_host="${1%%=*}" current=() h p
    if [ -f "$CONF" ]; then
        while read -r h p; do
            [ "$h" = "$want_host" ] || current+=("$h=$p")
        done < <(python3 - "$CONF" <<'EOF'
import re, sys
host = None
for line in open(sys.argv[1]):
    m = re.match(r'\s*- hostname:\s*"?([^"\s]+)"?', line)
    if m:
        host = m.group(1); continue
    m = re.match(r'\s*service:\s*http://127\.0\.0\.1:(\d+)', line)
    if m and host:
        print(host, m.group(1)); host = None
EOF
)
    fi
    up "${current[@]}" "$pair"
}

# Remove one hostname from the tunnel (its DNS record stays; the tunnel then
# answers 404 for it).
remove() {
    local drop="${1:?hostname required}" keep=() h p
    while read -r h p; do
        [ "$h" = "$drop" ] || keep+=("$h=$p")
    done < <(python3 - "$CONF" <<'EOF'
import re, sys
host = None
for line in open(sys.argv[1]):
    m = re.match(r'\s*- hostname:\s*"?([^"\s]+)"?', line)
    if m:
        host = m.group(1); continue
    m = re.match(r'\s*service:\s*http://127\.0\.0\.1:(\d+)', line)
    if m and host:
        print(host, m.group(1)); host = None
EOF
)
    up "${keep[@]}"
}

case "$cmd" in
    wildcard)     add "*.$domain=80" ;;
    wildcard-off) remove "*.$domain" ;;
    up)     shift 2; up "$@" ;;
    add)    shift 2; add "$@" ;;
    remove) shift 2; remove "$@" ;;
    status) systemctl is-active "$UNIT" || true; grep -E 'hostname|service' "$CONF" 2>/dev/null || true ;;
    down)   systemctl disable --now "$UNIT" >/dev/null 2>&1 || true; echo "stopped $UNIT" ;;
    *) usage ;;
esac
