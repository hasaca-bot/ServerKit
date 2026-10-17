#!/usr/bin/env bash
# Linux-side half of the Windows launcher (windows/ServerKit.ps1).
#
# Runs as root inside the dedicated WSL2 distro. It does not change how
# ServerKit works: it prepares the distro so it behaves like a normal Linux
# server (systemd as PID 1, no Windows programs on PATH) and then hands over
# to the unmodified upstream install.sh.
#
# Usage: wsl-setup.sh <configure|wait-systemd|install|health|status|theme> [args]

set -euo pipefail

cmd="${1:-}"
[ "$#" -gt 0 ] && shift

WSL_CONF=/etc/wsl.conf

configure() {
    # systemd=true           every service-management feature drives systemctl
    # appendWindowsPath=false  otherwise Windows node/python/docker shims under
    #                          /mnt/c answer `command -v` and the panel believes
    #                          tools are installed that cannot run here
    # default=root           the panel is administered as root, as on a VPS
    local wanted
    wanted="$(cat <<'EOF'
# Managed by ServerKit windows/wsl-setup.sh
[boot]
systemd=true

[user]
default=root

[interop]
enabled=true
appendWindowsPath=false

[network]
hostname=serverkit
generateHosts=true
generateResolvConf=true
EOF
)"
    if [ -f "$WSL_CONF" ] && [ "$(cat "$WSL_CONF")" = "$wanted" ]; then
        echo "unchanged"
        return 0
    fi
    if [ -f "$WSL_CONF" ] && [ ! -f "$WSL_CONF.serverkit-orig" ]; then
        cp "$WSL_CONF" "$WSL_CONF.serverkit-orig"
    fi
    printf '%s\n' "$wanted" > "$WSL_CONF"
    echo "changed"
}

wait_systemd() {
    local waited=0 state=""
    while [ "$waited" -lt 120 ]; do
        if [ -d /run/systemd/system ]; then
            state="$(systemctl is-system-running 2>/dev/null || true)"
            case "$state" in
                running|degraded) echo "systemd: $state"; return 0 ;;
            esac
        fi
        sleep 2
        waited=$((waited + 2))
    done
    echo "systemd did not come up (last state: ${state:-none})" >&2
    return 1
}

install() {
    local repo="${1:?repo path required}"
    [ -f "$repo/install.sh" ] || { echo "install.sh not found in $repo" >&2; exit 1; }
    [ -d /run/systemd/system ] || { echo "systemd is not running in this distro" >&2; exit 1; }

    # The repo lives on the Windows drive, where every file reads as mode 777
    # and git may have left CRLF behind. Run the installer from a clean copy
    # on the Linux filesystem instead.
    local stage=/root/.serverkit-installer
    rm -rf "$stage"
    mkdir -p "$stage/scripts"
    cp "$repo/install.sh" "$stage/install.sh"
    cp -r "$repo/scripts/lib" "$stage/scripts/lib"
    find "$stage" -type f -exec sed -i 's/\r$//' {} +
    chmod 755 "$stage/install.sh"

    # No domain and no public IP on a desktop: plain HTTP on localhost, which
    # is what the installer falls back to anyway when PANEL_DOMAIN is empty.
    export SERVERKIT_SKIP_SSL="${SERVERKIT_SKIP_SSL:-1}"
    export SERVERKIT_PROFILE="${SERVERKIT_PROFILE:-standard}"
    export DEBIAN_FRONTEND=noninteractive

    cd /root
    bash "$stage/install.sh" < /dev/null
    panel_ipv6
    ai_extras
}

health() {
    curl -sf --max-time 5 http://127.0.0.1:5000/api/v1/system/health >/dev/null
}

status() {
    local svc
    for svc in serverkit nginx docker fail2ban; do
        if systemctl list-unit-files "$svc.service" --no-legend 2>/dev/null | grep -q .; then
            printf '%-10s %s\n' "$svc" "$(systemctl is-active "$svc" 2>/dev/null || true)"
        else
            printf '%-10s %s\n' "$svc" "not installed"
        fi
    done
    # Tunnels are plain systemd units the panel has no page for (its own
    # tunnel feature is for remote agent servers), so list them here.
    local unit
    for unit in $(systemctl list-unit-files 'serverkit-remote.service' 'serverkit-site-*.service' \
                  'serverkit-tunnel-*.service' --no-legend 2>/dev/null | awk '{print $1}'); do
        printf '%-10s %s (%s)\n' "tunnel" "${unit%.service}" "$(systemctl is-active "$unit" 2>/dev/null || true)"
    done
    if [ -f /opt/serverkit/VERSION ]; then
        printf '%-10s %s\n' "version" "$(tr -d '\n\r ' < /opt/serverkit/VERSION)"
    fi
}

theme() {
    local file="${1:?theme.json path required}" tool
    tool="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/apply_theme.py"
    # Same invocation shape as the serverkit CLI's run_cli.
    cd /opt/serverkit/backend
    /opt/serverkit/venv/bin/python "$tool" "$file"
}

# Windows resolves "localhost" to ::1 first, and WSL forwards that to the
# distro's IPv6 loopback. The panel vhost only listens on IPv4, while every
# site vhost the panel generates also listens on [::]:80 -- so as soon as one
# site exists, http://localhost stops being the panel and becomes that site.
# Give the panel vhost the IPv6 default as well.
panel_ipv6() {
    local conf changed=0
    for conf in /etc/nginx/sites-available/serverkit.conf \
                /etc/nginx/sites-available/serverkit-insecure.conf; do
        [ -f "$conf" ] || continue
        if grep -q 'listen 80 default_server;' "$conf" && ! grep -q 'listen \[::\]:80 default_server;' "$conf"; then
            sed -i 's/^\( *\)listen 80 default_server;/&\n\1listen [::]:80 default_server;/' "$conf"
            changed=1
        fi
    done
    if [ "$changed" = 1 ]; then
        nginx -t >/dev/null 2>&1 && systemctl reload nginx
        echo "panel vhost now answers on IPv6 too"
    fi
}

# The panel ships its AI library (prompture) with only the OpenAI and
# Anthropic drivers; picking Google Gemini in Settings -> AI then fails with
# "Missing Python dependencies: google.genai". Add the library's own "google"
# extra at the version the panel pins, so the dependency resolution stays
# the library's.
ai_extras() {
    local venv=/opt/serverkit/venv pinned
    pinned="$("$venv/bin/pip" show prompture 2>/dev/null | sed -n 's/^Version: //p')"
    [ -n "$pinned" ] || { echo "prompture not installed in $venv" >&2; return 1; }
    "$venv/bin/python" -c 'import google.genai' 2>/dev/null && return 0
    "$venv/bin/pip" install -q "prompture[openai,anthropic,google]==$pinned"
    systemctl restart serverkit
    echo "Gemini support added"
}

case "$cmd" in
    configure)    configure ;;
    wait-systemd) wait_systemd ;;
    install)      install "$@" ;;
    health)       health ;;
    status)       status ;;
    theme)        theme "$@" ;;
    panel-ipv6)   panel_ipv6 ;;
    ai-extras)    ai_extras ;;
    *) echo "usage: $0 <configure|wait-systemd|install|health|status|theme>" >&2; exit 2 ;;
esac
