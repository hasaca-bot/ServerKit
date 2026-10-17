#!/usr/bin/env bash
# Shell access to windows/serverkit-windows.json (source this file).
#
#   . "$(dirname "$0")/lib/config.sh"
#   swk_cfg domain                  # domain.base (also: gateway_host, panel_host, tunnel,
#                                   #   wildcard, gateway_mode, gateway_port, panel_port, ...)
#   swk_cfg limits.rate_per_minute  # any dotted key; optional second argument = default
#   swk_require_config              # exit with instructions when the file is missing/invalid
#
# The JSON is parsed by windows/swk_config.py (python3), so shell and Python
# always agree. The file location is $SERVERKIT_WINDOWS_CONFIG or
# windows/serverkit-windows.json.

SWK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export SWK_DIR

swk_cfg() {
    python3 "$SWK_DIR/swk_config.py" get "$@"
}

swk_require_config() {
    python3 "$SWK_DIR/swk_config.py" check >/dev/null || exit 1
}
