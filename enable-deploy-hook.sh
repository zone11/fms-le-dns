#!/bin/sh
set -eu

SOURCE_HOOK="/opt/fms-le-dns/libexec/fms-le-dns-hook.sh"
TARGET_HOOK="/etc/letsencrypt/renewal-hooks/deploy/fms-le-dns.sh"
DEPLOY_PROGRAM="/opt/fms-le-dns/bin/fms-le-dns-deploy.py"
CONFIG_FILE="/etc/fms-le-dns/fms-le-dns.conf"

if [ "$(id -u)" -ne 0 ]; then
    echo "fms-le-dns: run as root" >&2
    exit 1
fi

/usr/bin/python3 "${DEPLOY_PROGRAM}" --config "${CONFIG_FILE}" --check
/usr/bin/install -o root -g root -m 0755 "${SOURCE_HOOK}" "${TARGET_HOOK}"
/usr/bin/bash -n "${TARGET_HOOK}"
echo "Enabled Certbot deploy hook: ${TARGET_HOOK}"
