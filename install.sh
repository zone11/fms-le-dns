#!/bin/bash
set -euo pipefail

PROJECT_DIR="/opt/fms-le-dns"
CONFIG_DIR="/etc/fms-le-dns"
CONFIG_FILE="${CONFIG_DIR}/fms-le-dns.conf"
PKI_DIR="${CONFIG_DIR}/pki"
CREDENTIAL_DIR="/etc/letsencrypt/credentials"
CREDENTIAL_FILE="${CREDENTIAL_DIR}/cloudflare.ini"
EVENT_LOG="/opt/FileMaker/FileMaker Server/Logs/Event.log"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

FQDN=""
LINEAGE_NAME=""
PKI_KEY_NAME="fms-le-dns"
TOKEN_FILE=""
UPDATE_CODE=false

usage() {
    cat <<'EOF'
Usage:
  sudo ./install.sh [options]
  sudo ./install.sh --update-code

Options:
  --fqdn NAME                   Certificate and FileMaker Server FQDN
  --lineage-name NAME           Certbot certificate name; defaults to FQDN
  --pki-key-name NAME           FileMaker Admin API PKI key name
                                (default: fms-le-dns)
  --cloudflare-token-file PATH  File containing only the Cloudflare API token
  --update-code                 Update installed program/helper files only
  -h, --help                    Show this help

Without value options, the installer prompts interactively. It does not issue a
certificate, register the public key, restart FileMaker, or enable the hook.
EOF
}

fail() {
    echo "fms-le-dns installer: $*" >&2
    exit 1
}

while (($#)); do
    case "$1" in
        --fqdn)
            [[ $# -ge 2 ]] || fail "--fqdn requires a value"
            FQDN="$2"
            shift 2
            ;;
        --lineage-name)
            [[ $# -ge 2 ]] || fail "--lineage-name requires a value"
            LINEAGE_NAME="$2"
            shift 2
            ;;
        --pki-key-name)
            [[ $# -ge 2 ]] || fail "--pki-key-name requires a value"
            PKI_KEY_NAME="$2"
            shift 2
            ;;
        --cloudflare-token-file)
            [[ $# -ge 2 ]] || fail "--cloudflare-token-file requires a value"
            TOKEN_FILE="$2"
            shift 2
            ;;
        --update-code)
            UPDATE_CODE=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown option: $1"
            ;;
    esac
done

[[ $EUID -eq 0 ]] || fail "run as root"

for source_file in \
    "${SCRIPT_DIR}/fms-le-dns-deploy.py" \
    "${SCRIPT_DIR}/fms-le-dns-hook.sh" \
    "${SCRIPT_DIR}/enable-deploy-hook.sh" \
    "${SCRIPT_DIR}/fms-le-dns.conf.template"; do
    [[ -f "$source_file" ]] || fail "missing installation-kit file: $source_file"
done

if ! $UPDATE_CODE; then
    [[ ! -e "$CONFIG_FILE" ]] || fail "$CONFIG_FILE already exists; use --update-code or perform a documented migration"
    [[ ! -e "${PKI_DIR}/certbot.key" ]] || fail "PKI private key already exists"
    [[ ! -e "$CREDENTIAL_FILE" ]] || fail "$CREDENTIAL_FILE already exists"
fi

for command_path in \
    /usr/bin/install \
    /usr/bin/openssl \
    /usr/bin/python3 \
    /usr/bin/certbot \
    /usr/bin/grep \
    /usr/bin/tail \
    /usr/sbin/service; do
    [[ -x "$command_path" ]] || fail "missing required executable: $command_path"
done

/usr/bin/python3 - <<'PY'
required = ("cryptography", "jwt", "requests", "urllib3")
missing = []
for module in required:
    try:
        __import__(module)
    except ImportError:
        missing.append(module)
if missing:
    raise SystemExit("Missing required Python modules: " + ", ".join(missing))
print("Required Python modules: OK")
PY

/usr/bin/certbot plugins 2>/dev/null |
    grep -Eq '^[[:space:]]*[-*][[:space:]]+dns-cloudflare$' ||
    fail "Certbot dns-cloudflare plugin is not available"

install -d -o root -g root -m 0755 "${PROJECT_DIR}/bin" "${PROJECT_DIR}/libexec"
install -o root -g root -m 0755 \
    "${SCRIPT_DIR}/fms-le-dns-deploy.py" \
    "${PROJECT_DIR}/bin/fms-le-dns-deploy.py"
install -o root -g root -m 0755 \
    "${SCRIPT_DIR}/enable-deploy-hook.sh" \
    "${PROJECT_DIR}/bin/fms-le-dns-enable-hook"
install -o root -g root -m 0755 \
    "${SCRIPT_DIR}/fms-le-dns-hook.sh" \
    "${PROJECT_DIR}/libexec/fms-le-dns-hook.sh"

if $UPDATE_CODE; then
    [[ -f "$CONFIG_FILE" ]] || fail "cannot update: missing $CONFIG_FILE"
    /usr/bin/python3 -c \
        'import ast, pathlib, sys; p=pathlib.Path(sys.argv[1]); ast.parse(p.read_text(), filename=str(p))' \
        "${PROJECT_DIR}/bin/fms-le-dns-deploy.py"
    /usr/bin/bash -n "${PROJECT_DIR}/libexec/fms-le-dns-hook.sh"
    echo "Updated fms-le-dns program and helper files; configuration and secrets unchanged."
    exit 0
fi

if [[ -z "$FQDN" ]]; then
    read -r -p "FileMaker Server FQDN: " FQDN
fi
FQDN="${FQDN%.}"
FQDN="${FQDN,,}"
[[ "$FQDN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ && "$FQDN" == *.* ]] ||
    fail "invalid FQDN"

if [[ -z "$LINEAGE_NAME" ]]; then
    read -r -p "Certbot certificate name [${FQDN}]: " LINEAGE_NAME
    LINEAGE_NAME="${LINEAGE_NAME:-$FQDN}"
fi
[[ "$LINEAGE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] ||
    fail "invalid Certbot certificate name"
[[ "$PKI_KEY_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] ||
    fail "invalid PKI key name"

cloudflare_token=""
if [[ -n "$TOKEN_FILE" ]]; then
    [[ -f "$TOKEN_FILE" ]] || fail "token file not found: $TOKEN_FILE"
    cloudflare_token="$(<"$TOKEN_FILE")"
else
    [[ -t 0 ]] || fail "use --cloudflare-token-file for non-interactive installation"
    read -r -s -p "Cloudflare API token: " cloudflare_token
    echo
fi
[[ -n "$cloudflare_token" && "$cloudflare_token" != *[[:space:]]* ]] ||
    fail "Cloudflare token is empty or contains whitespace"

install -d -o root -g root -m 0755 "$CONFIG_DIR"
install -d -o root -g root -m 0700 "$PKI_DIR" "$CREDENTIAL_DIR"
install -d -o root -g root -m 0755 /etc/letsencrypt/renewal-hooks/deploy

umask 077
temporary_config="$(mktemp)"
temporary_credential="$(mktemp)"
cleanup() {
    rm -f "$temporary_config" "$temporary_credential"
    unset cloudflare_token
}
trap cleanup EXIT

sed \
    -e "s/{{FQDN}}/${FQDN}/g" \
    -e "s/{{LINEAGE_NAME}}/${LINEAGE_NAME}/g" \
    -e "s/{{PKI_KEY_NAME}}/${PKI_KEY_NAME}/g" \
    "${SCRIPT_DIR}/fms-le-dns.conf.template" >"$temporary_config"
printf 'dns_cloudflare_api_token = %s\n' "$cloudflare_token" >"$temporary_credential"

install -o root -g root -m 0600 "$temporary_config" "$CONFIG_FILE"
install -o root -g root -m 0600 "$temporary_credential" "$CREDENTIAL_FILE"

openssl genpkey \
    -algorithm RSA \
    -pkeyopt rsa_keygen_bits:4096 \
    -out "${PKI_DIR}/certbot.key"
openssl pkey \
    -in "${PKI_DIR}/certbot.key" \
    -pubout \
    -out "${PKI_DIR}/certbot.key.pub"
chown root:root "${PKI_DIR}/certbot.key" "${PKI_DIR}/certbot.key.pub"
chmod 0600 "${PKI_DIR}/certbot.key"
chmod 0644 "${PKI_DIR}/certbot.key.pub"

/usr/bin/python3 -c \
    'import ast, pathlib, sys; p=pathlib.Path(sys.argv[1]); ast.parse(p.read_text(), filename=str(p))' \
    "${PROJECT_DIR}/bin/fms-le-dns-deploy.py"
/usr/bin/bash -n "${PROJECT_DIR}/libexec/fms-le-dns-hook.sh"

echo
echo "Staged installation complete."
echo "Configuration: ${CONFIG_FILE}"
echo "Public PKI key: ${PKI_DIR}/certbot.key.pub"
echo "Certbot deploy hook: not enabled"

latest_ssl_state="$(
    /usr/bin/grep -F 'SECURITY: Secure (SSL) Network Encryption:' "${EVENT_LOG}" 2>/dev/null |
        /usr/bin/tail -n 1 || true
)"
case "${latest_ssl_state}" in
    *"Network Encryption: Enabled")
        echo "Database Server SSL runtime precheck: enabled"
        ;;
    *"Network Encryption: Disabled")
        echo "WARNING: Database Server SSL runtime precheck: disabled" >&2
        echo "Enable UseSecureConnection before the controlled first activation." >&2
        ;;
    *)
        echo "WARNING: Database Server SSL runtime precheck: not available" >&2
        echo "Verify UseSecureConnection and Event.log before enabling the hook." >&2
        ;;
esac

echo "Continue with the registration and initial-activation sections in MANUAL.md."
