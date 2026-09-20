#!/bin/sh
set -eu

DEPLOY_PROGRAM="/opt/fms-le-dns/bin/fms-le-dns-deploy.py"
CONFIG_FILE="/etc/fms-le-dns/fms-le-dns.conf"

if [ "${RENEWED_LINEAGE:-}" = "" ]; then
    echo "fms-le-dns: RENEWED_LINEAGE is not set" >&2
    exit 1
fi

exec /usr/bin/python3 "${DEPLOY_PROGRAM}" \
    --config "${CONFIG_FILE}" \
    --from-hook \
    --lineage "${RENEWED_LINEAGE}"
