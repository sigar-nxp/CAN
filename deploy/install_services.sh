#!/usr/bin/env bash
# ==============================================================================
# PS Locks OIP - Production Deployment Installer
# Automated systemd service installation for can0 and pslocks-gateway
# ==============================================================================

set -euo pipefail

# 1. Validate root / sudo execution
if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run as root or with sudo privileges." >&2
    echo "Usage: sudo $0" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAN_SERVICE="can0.service"
GATEWAY_SERVICE="pslocks-gateway.service"
SYSTEMD_DIR="/etc/systemd/system"

echo "=== Installing PS Locks OIP Systemd Services ==="

# 2. Verify source service files exist
for svc_file in "${CAN_SERVICE}" "${GATEWAY_SERVICE}"; do
    if [ ! -f "${SCRIPT_DIR}/${svc_file}" ]; then
        echo "Error: Service file not found: ${SCRIPT_DIR}/${svc_file}" >&2
        exit 1
    fi
done

# 3. Copy service files to /etc/systemd/system/
echo "Copying service files to ${SYSTEMD_DIR}/..."
cp "${SCRIPT_DIR}/${CAN_SERVICE}" "${SYSTEMD_DIR}/${CAN_SERVICE}"
cp "${SCRIPT_DIR}/${GATEWAY_SERVICE}" "${SYSTEMD_DIR}/${GATEWAY_SERVICE}"
chmod 644 "${SYSTEMD_DIR}/${CAN_SERVICE}" "${SYSTEMD_DIR}/${GATEWAY_SERVICE}"

# 4. Reload systemd daemon
echo "Reloading systemd daemon..."
systemctl daemon-reload

# 5. Enable and start both services
echo "Enabling and starting ${CAN_SERVICE} and ${GATEWAY_SERVICE}..."
systemctl enable --now "${CAN_SERVICE}" "${GATEWAY_SERVICE}"

# 6. Display clean status feedback with systemctl is-active
echo ""
echo "=== Service Status Feedback ==="
for svc in "${CAN_SERVICE}" "${GATEWAY_SERVICE}"; do
    status=$(systemctl is-active "${svc}" 2>/dev/null || true)
    if [ "${status}" = "active" ]; then
        echo "  [OK] ${svc}: ${status}"
    else
        echo "  [WARN] ${svc}: ${status}"
    fi
done

echo ""
echo "Deployment installation completed successfully."
