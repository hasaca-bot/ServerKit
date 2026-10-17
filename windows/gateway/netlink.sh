#!/usr/bin/env bash
# serverkit-netlink: keep listed site containers connected to the docker network
# "serverkit-services" (where the database engines live), also after redeploys.
#   serverkit-netlink add <container>     remember + connect now
#   serverkit-netlink remove <container>  forget + disconnect now
#   serverkit-netlink reconcile           connect every listed running container
#   serverkit-netlink watch               reconcile, then follow docker start events
# Names: site containers serverkit-app-<number> and the gateway's database engines
# <site>-<postgresql|mysql|mariadb|redis>. State: one name per line. The network is
# created here when missing (the panel does not create it on this host).
set -uo pipefail

STATE=/etc/serverkit-gateway/netlinks
NET=serverkit-services
NAME_RE='^(serverkit-app-[0-9]+|[a-z][a-z0-9-]{1,40}-(postgresql|mysql|mariadb|redis))$'

need_name() {
    [[ "${1:-}" =~ $NAME_RE ]] || { echo "container name must match $NAME_RE" >&2; exit 2; }
}

net_exists() { docker network inspect "$NET" >/dev/null 2>&1; }

is_running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }

is_connected() {
    docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$1" 2>/dev/null | grep -qw -- "$NET"
}

listed() { [ -f "$STATE" ] && grep -qxF -- "$1" "$STATE"; }

ensure_net() {
    net_exists || docker network create "$NET" >/dev/null 2>&1 || net_exists
}

connect() {
    ensure_net || return 0
    is_running "$1" || return 0
    is_connected "$1" && return 0
    out=$(docker network connect "$NET" "$1" 2>&1) || {
        case "$out" in *"already exists"*) ;; *) echo "connect $1: $out" >&2 ;; esac
    }
    return 0
}

reconcile() {
    [ -f "$STATE" ] || return 0
    while IFS= read -r c; do
        [[ "$c" =~ $NAME_RE ]] && connect "$c"
    done < "$STATE"
    return 0
}

ensure_state() {
    mkdir -p --mode=0700 "$(dirname "$STATE")"
    [ -f "$STATE" ] || ( umask 077; : > "$STATE" )
    chmod 600 "$STATE"
}

case "${1:-}" in
    add)
        need_name "${2:-}"; ensure_state
        listed "$2" || echo "$2" >> "$STATE"
        connect "$2"
        echo "listed $2" ;;
    remove)
        need_name "${2:-}"; ensure_state
        tmp=$(mktemp "$STATE.XXXXXX"); chmod 600 "$tmp"
        grep -vxF -- "$2" "$STATE" > "$tmp"; mv "$tmp" "$STATE"
        if net_exists && is_connected "$2"; then
            docker network disconnect "$NET" "$2" >/dev/null 2>&1 || true
        fi
        echo "unlisted $2" ;;
    reconcile)
        ensure_state; reconcile ;;
    watch)
        ensure_state
        reconcile
        # the network appears when the first database engine is installed: look again now and then
        ( while sleep 60; do reconcile; done ) &
        ticker=$!
        trap 'kill "$ticker" 2>/dev/null' EXIT
        while true; do
            docker events --filter type=container --filter event=start \
                --format '{{.Actor.Attributes.name}}' 2>/dev/null | while IFS= read -r c; do
                if [[ "$c" =~ $NAME_RE ]] && listed "$c"; then connect "$c"; fi
            done
            sleep 5   # docker went away or restarted: reconnect to the event stream
            reconcile
        done ;;
    *) echo "usage: $0 add|remove <container> | reconcile | watch" >&2; exit 2 ;;
esac
