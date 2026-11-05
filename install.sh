#!/usr/bin/env bash
# catlink-local installer. Run as root from the repository checkout:
#
#   ./install.sh               install or upgrade (idempotent)
#   ./install.sh --uninstall   stop + disable the units, remove the code and the unit files
#
# What it does:
#   1. creates /opt/catlink-local and /etc/catlink-local
#   2. copies gateway.py, authresp.py and catlink_local/*.py to /opt/catlink-local/ (stale .py removed first)
#   3. installs systemd/*.service to /etc/systemd/system/ and runs `systemctl daemon-reload`
#   4. copies config/catlink-local.env.example to /etc/catlink-local/catlink-local.env if that file is absent
#   5. prints the remaining manual steps
# It does NOT generate the TLS certificate, create the Mosquitto user, touch DNS or the firewall, or
# enable/start anything. Those are separate steps in docs/SETUP.md.
set -euo pipefail

CODE_DIR=/opt/catlink-local
CONF_DIR=/etc/catlink-local
STATE_DIR=/var/lib/catlink-local
UNIT_DIR=/etc/systemd/system
ENV_FILE="$CONF_DIR/catlink-local.env"
UNITS=(catlink-gateway.service catlink-authresp.service)

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "install.sh: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"
command -v systemctl >/dev/null 2>&1 || die "systemd (systemctl) is required"

uninstall() {
    for unit in "${UNITS[@]}"; do
        systemctl disable --now "$unit" 2>/dev/null || true
        rm -f "$UNIT_DIR/$unit"
    done
    systemctl daemon-reload
    rm -rf "$CODE_DIR"
    echo "catlink-local removed: units stopped, disabled and deleted; $CODE_DIR deleted."
    echo "Left in place: $CONF_DIR (configuration, password file, certificate) and $STATE_DIR (state)."
    echo "Delete them by hand if you no longer need them."
}

install_all() {
    for f in gateway.py authresp.py systemd/catlink-gateway.service systemd/catlink-authresp.service \
             config/catlink-local.env.example catlink_local/__init__.py; do
        [ -f "$SRC_DIR/$f" ] || die "missing $f: run this from the catlink-local checkout"
    done
    [ -x /usr/bin/python3 ] || die "/usr/bin/python3 not found (the units run it)"
    /usr/bin/python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
        || die "Python 3.9 or newer is required"

    # 1. directories
    install -d -o root -g root -m 0755 "$CODE_DIR" "$CODE_DIR/catlink_local" "$CONF_DIR"

    # 2. code: remove stale .py (and bytecode) first, then copy
    rm -f "$CODE_DIR"/*.py "$CODE_DIR"/catlink_local/*.py
    rm -rf "$CODE_DIR/__pycache__" "$CODE_DIR/catlink_local/__pycache__"
    install -o root -g root -m 0644 "$SRC_DIR/gateway.py" "$SRC_DIR/authresp.py" "$CODE_DIR/"
    install -o root -g root -m 0644 "$SRC_DIR"/catlink_local/*.py "$CODE_DIR/catlink_local/"

    # 3. units
    for unit in "${UNITS[@]}"; do
        install -o root -g root -m 0644 "$SRC_DIR/systemd/$unit" "$UNIT_DIR/$unit"
    done
    systemctl daemon-reload

    # 4. configuration (never overwritten)
    if [ -e "$ENV_FILE" ]; then
        echo "Kept existing $ENV_FILE"
    else
        install -o root -g root -m 0644 "$SRC_DIR/config/catlink-local.env.example" "$ENV_FILE"
        echo "Created $ENV_FILE from the example"
    fi

    # 5. what is left to do
    cat <<EOF

catlink-local installed to $CODE_DIR (units: ${UNITS[*]}). Nothing was enabled or started.

Remaining steps (details in docs/SETUP.md):
  1. Edit $ENV_FILE: set CATLINK_BOX_MAC and CATLINK_BOX_BIND_IP (and CATLINK_BOX_IP if you have it).
  2. Generate the TLS certificate and key: $CONF_DIR/cert.pem (0644) and $CONF_DIR/key.pem (0600).
  3. Create the Mosquitto user and put its password in $CONF_DIR/ha_mqtt.pass (root, 0600).
  4. Make sure DNS redirects the box to this host and the firewall lets only the box reach 443 and 1883.
  5. systemctl enable --now catlink-authresp catlink-gateway
  6. journalctl -u catlink-gateway -o cat -f     (expect connect, subscribe, publish, ha_connected)
EOF
}

case "${1:-}" in
    "") install_all ;;
    --uninstall) uninstall ;;
    *) die "usage: $0 [--uninstall]" ;;
esac
