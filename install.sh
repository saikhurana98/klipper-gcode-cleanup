#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# klipper-gcode-cleanup — install / update script
# Called by Moonraker's update_manager after every git pull, and by the user
# on first install.
#
# Requires NO sudo / root. Everything is installed into the user's home.
#
# What this script does:
#   1. Copies the default config to ~/printer_data/config/gcode_cleanup.cfg
#      (only on first install — never overwrites user edits on update).
#   2. Installs and enables a systemd USER timer (~/.config/systemd/user/).
#   3. Enables session lingering so the timer survives without an active login.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-/usr/bin/python3}"

CONFIG_DEST="${HOME}/printer_data/config/gcode_cleanup.cfg"
CONFIG_SRC="${SCRIPT_DIR}/gcode_cleanup.cfg"
UNIT_DIR="${HOME}/.config/systemd/user"

# ── 1. Default config (never overwrite on update) ─────────────────────────────
mkdir -p "$(dirname "${CONFIG_DEST}")"
if [ ! -f "${CONFIG_DEST}" ]; then
    cp "${CONFIG_SRC}" "${CONFIG_DEST}"
    echo "[install] Default config installed → ${CONFIG_DEST}"
else
    echo "[install] Config exists → ${CONFIG_DEST}  (not overwritten)"
fi

# ── 2. Systemd user unit files ────────────────────────────────────────────────
mkdir -p "${UNIT_DIR}"
cp "${SCRIPT_DIR}/klipper-cleanup.service" "${UNIT_DIR}/klipper-cleanup.service"
cp "${SCRIPT_DIR}/klipper-cleanup.timer"   "${UNIT_DIR}/klipper-cleanup.timer"

systemctl --user daemon-reload
systemctl --user enable --now klipper-cleanup.timer
echo "[install] User timer enabled: klipper-cleanup.timer"

# ── 3. Session linger (user services survive without active login) ─────────────
# loginctl enable-linger for *your own* user requires no sudo on systemd ≥ 239.
if loginctl enable-linger 2>/dev/null; then
    echo "[install] Session linger enabled."
else
    echo "[install] WARNING: Could not enable linger automatically."
    echo "          Run once (may need sudo once):  loginctl enable-linger \$(whoami)"
fi

# ── 4. Python dependencies ────────────────────────────────────────────────────
if ! "${PYTHON}" -c "import requests" 2>/dev/null; then
    echo "[install] WARNING: 'requests' not found for ${PYTHON}."
    echo "          Run:  pip3 install --user requests"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "[install] klipper-gcode-cleanup installed/updated."
echo "          Schedule and retention rules → edit in Fluidd/Mainsail:"
echo "          Config files → gcode_cleanup.cfg"
echo ""
echo "          Verify timer:"
echo "            systemctl --user list-timers klipper-cleanup.timer"
echo "          Dry-run:"
echo "            ${PYTHON} ${SCRIPT_DIR}/cleanup.py --cleanup --dry-run"
echo "            ${PYTHON} ${SCRIPT_DIR}/cleanup.py --purge   --dry-run"
echo "          Logs:"
echo "            journalctl --user -t klipper-cleanup -f"
echo "            tail -f ~/printer_data/logs/gcode-cleanup.log"
