# fms-le-dns Installation and Operations Manual

## 1. Purpose

`fms-le-dns` obtains a public TLS certificate through Certbot and Cloudflare DNS-01, imports it through FileMaker Admin API v2 using PKI authentication, gracefully stops Database Server, restarts FileMaker Server through `fmshelper`, and verifies the externally presented certificate.

The installation kit contains no deployment hostname, certificate name, Cloudflare token, certificate, or private key. The installer collects site-specific values and writes them to protected files on the target server.

## 2. Supported design

- Ubuntu 24.04 LTS on `x86_64`.
- FileMaker Server 26 with Admin API v2 and Linux `fmshelper` service control.
- Official Snap distribution of Certbot and its Cloudflare DNS plugin.
- One configured FileMaker FQDN and one Certbot lineage per installation.
- A dedicated FileMaker Admin API PKI identity.
- No stored FileMaker Admin Console password.
- No FileMaker scripts or schedules.
- No direct modification of FileMaker `CStore`.

Confirm compatibility again after an operating-system, FileMaker Server, Admin API, Certbot, or plugin upgrade.

## 3. Installation stages

Installation is deliberately staged:

1. Prepare prerequisites and a restricted Cloudflare token.
2. Run `install.sh` to install code, configuration, credentials, and a new PKI key pair.
3. Register the generated public key in FileMaker Admin Console.
4. Request the initial certificate while the deploy hook is still disabled.
5. Validate the certificate files without changing FileMaker.
6. Enable FileMaker Database Server SSL through the supported interactive CLI.
7. Perform the controlled first import and activation.
8. Verify trusted HTTPS, Database Server SSL, and FileMaker status.
9. Enable the Certbot deploy hook.
10. Test renewal and the enabled hook.

The installer never issues a certificate, registers a FileMaker key, restarts FileMaker, or enables the deploy hook.
It performs a passwordless precheck of the latest FileMaker Event log entry and warns when Database Server SSL is disabled or cannot be determined. This warning is expected on some new installations and does not make the staged installation fail.

## 4. Security model

The installation uses these protected assets:

| Asset | Default path | Required mode |
|---|---|---|
| Configuration | `/etc/fms-le-dns/fms-le-dns.conf` | `root:root 0600` |
| PKI private key | `/etc/fms-le-dns/pki/certbot.key` | `root:root 0600` |
| PKI public key | `/etc/fms-le-dns/pki/certbot.key.pub` | `root:root 0644` |
| Cloudflare credential | `/etc/letsencrypt/credentials/cloudflare.ini` | `root:root 0600` |
| Deployment program | `/opt/fms-le-dns/bin/fms-le-dns-deploy.py` | `root:root 0755` |
| Staged hook | `/opt/fms-le-dns/libexec/fms-le-dns-hook.sh` | `root:root 0755` |
| Enabled hook | `/etc/letsencrypt/renewal-hooks/deploy/fms-le-dns.sh` | `root:root 0755` |
| Deployment log | `/var/log/fms-le-dns.log` | `root:root 0600` |

The Cloudflare token should have only `Zone:DNS:Edit` and `Zone:Zone:Read` for the DNS zone containing the FileMaker hostname. Do not use the Cloudflare global API key.

## 5. Prerequisites

Run as a user with `sudo` access and confirm the host baseline:

```bash
uname -m
lsb_release -ds
openssl version
python3 --version
timedatectl status
sudo /usr/sbin/service fmshelper status
```

The system clock must be synchronized. FileMaker Server must be operational before installation.

Install Certbot and the Cloudflare plugin from Snap when they are not already present:

```bash
sudo snap install core
sudo snap refresh core
sudo snap install --classic certbot
sudo ln -s /snap/bin/certbot /usr/bin/certbot
sudo snap set certbot trust-plugin-with-root=ok
sudo snap install certbot-dns-cloudflare
```

If `/usr/bin/certbot` already exists, inspect it before creating the link. Do not overwrite an unknown executable.

Validate Certbot:

```bash
certbot --version
sudo certbot plugins
sudo snap connections certbot
sudo systemctl status snap.certbot.renew.timer --no-pager
```

The deployment program also requires the Python modules `cryptography`, `jwt`, `requests`, and `urllib3`. Install the Ubuntu packages if the installer reports them missing:

```bash
sudo apt-get update
sudo apt-get install python3-cryptography python3-jwt python3-requests python3-urllib3
```

Do not install Debian Certbot alongside Snap Certbot. On a host with old Debian Certbot residue, inspect its package removal script before purging it: some package versions remove `/etc/letsencrypt` during purge.

## 6. Prepare the installation kit

Place the complete kit in a root-readable working directory. It must contain:

```text
README.md
MANUAL.md
LICENSE
install.sh
fms-le-dns-deploy.py
fms-le-dns-hook.sh
enable-deploy-hook.sh
fms-le-dns.conf.template
```

Validate the supplied scripts before use:

```bash
bash -n install.sh
bash -n enable-deploy-hook.sh
bash -n fms-le-dns-hook.sh
python3 - <<'PY'
import ast
from pathlib import Path

path = Path("fms-le-dns-deploy.py")
ast.parse(path.read_text(), filename=str(path))
print("Python syntax: OK")
PY
```

## 7. Run the staged installer

Make the installer executable and run it interactively:

```bash
chmod 0755 install.sh
sudo ./install.sh
```

The installer prompts for:

- FileMaker Server FQDN.
- Certbot certificate/lineage name, defaulting to the FQDN.
- Cloudflare API token, entered without echo.

The FileMaker PKI key name defaults to `fms-le-dns`. Override it with `--pki-key-name` when necessary.

For a non-interactive staging run, put only the Cloudflare token in a temporary mode-`0600` file and use:

```bash
sudo ./install.sh \
  --fqdn '<FILEMAKER_FQDN>' \
  --lineage-name '<CERTBOT_CERTIFICATE_NAME>' \
  --pki-key-name '<FILEMAKER_PKI_KEY_NAME>' \
  --cloudflare-token-file '<TOKEN_FILE>'
```

Delete the temporary token file securely according to local policy after installation. Never place the token directly on the command line.

The installer refuses to overwrite existing configuration, PKI keys, or Cloudflare credentials. Use `--update-code` only for a reviewed program update; that mode preserves configuration and secrets.

## 8. Inspect generated configuration

Review the configuration without displaying the Cloudflare credential:

```bash
sudo cat /etc/fms-le-dns/fms-le-dns.conf
sudo stat -c '%U:%G %a %n' \
  /etc/fms-le-dns/fms-le-dns.conf \
  /etc/fms-le-dns/pki \
  /etc/fms-le-dns/pki/certbot.key \
  /etc/fms-le-dns/pki/certbot.key.pub \
  /etc/letsencrypt/credentials/cloudflare.ini
```

Load non-secret values from the configuration for the remaining commands:

```bash
CONFIG=/etc/fms-le-dns/fms-le-dns.conf

FQDN="$(sudo python3 -c \
  'import configparser,sys; c=configparser.ConfigParser(); c.read(sys.argv[1]); print(c["server"]["fqdn"])' \
  "$CONFIG")"

LINEAGE="$(sudo python3 -c \
  'import configparser,sys; c=configparser.ConfigParser(); c.read(sys.argv[1]); print(c["server"]["lineage"])' \
  "$CONFIG")"

CERT_NAME="${LINEAGE##*/}"

PKI_KEY_NAME="$(sudo python3 -c \
  'import configparser,sys; c=configparser.ConfigParser(); c.read(sys.argv[1]); print(c["server"]["pki_key_name"])' \
  "$CONFIG")"

printf 'FQDN=%s\nCERT_NAME=%s\nPKI_KEY_NAME=%s\n' \
  "$FQDN" "$CERT_NAME" "$PKI_KEY_NAME"
```

## 9. Register the FileMaker PKI public key

In FileMaker Admin Console, open the Admin API PKI-key management view and add a key with:

- Name: the configured `pki_key_name`, including exact case.
- Public key: `/etc/fms-le-dns/pki/certbot.key.pub`.

Only the public key is registered. Never upload or display `certbot.key`.

Confirm that the configured key name appears in the Admin Console key list before continuing.

## 10. Request the initial certificate

Set the account email for the current shell:

```bash
read -r -p 'ACME account email: ' ACME_EMAIL
```

Request an RSA certificate with the hook still disabled:

```bash
sudo certbot certonly \
  --dns-cloudflare \
  --dns-cloudflare-credentials /etc/letsencrypt/credentials/cloudflare.ini \
  --dns-cloudflare-propagation-seconds 30 \
  --cert-name "$CERT_NAME" \
  --key-type rsa \
  --rsa-key-size 2048 \
  --email "$ACME_EMAIL" \
  --agree-tos \
  --no-eff-email \
  -d "$FQDN"
```

Confirm that the certificate appears under the configured lineage:

```bash
sudo certbot certificates
sudo openssl x509 \
  -in "$LINEAGE/cert.pem" \
  -noout -subject -issuer -serial -dates -ext subjectAltName
```

## 11. Validate without changing FileMaker

```bash
sudo /usr/bin/python3 \
  /opt/fms-le-dns/bin/fms-le-dns-deploy.py \
  --config /etc/fms-le-dns/fms-le-dns.conf \
  --check
```

This checks file presence, ownership and permissions, SAN, validity period, certificate/private-key match, and chain trust. It does not contact FileMaker or restart services.

## 12. Enable Database Server SSL

Before activation, enable SSL for FileMaker Pro and FileMaker Go Database Server connections through FileMaker's supported CLI. This setting is not exposed by the FileMaker Admin API v2 endpoint used by this project.

Inspect the setting interactively:

```bash
sudo /usr/bin/fmsadmin get serverprefs |
grep -E 'UseSecureConnection|AuthenticatedStream'
```

Enter the Admin Console credentials when prompted. Do not put the password on the command line or in a script.

If the output shows `UseSecureConnection = false`, enable it:

```bash
sudo /usr/bin/fmsadmin \
  set serverprefs \
  UseSecureConnection=true
```

Confirm the persisted value:

```bash
sudo /usr/bin/fmsadmin get serverprefs |
grep -E 'UseSecureConnection|AuthenticatedStream'
```

Expected:

```text
UseSecureConnection = true
```

Do not confuse `UseSecureConnection` with `SecureFilesOnly` or the Admin API property `requireSecureDB`. Those settings require password-protected Full Access accounts for hosted files; they do not enable TLS on port 5003.

Do not restart FileMaker separately at this point. The controlled activation below performs the required full restart, applying both the certificate and `UseSecureConnection=true` in one maintenance interruption.

## 13. Controlled first activation

This step imports the certificate and restarts all FileMaker Server processes. Schedule a maintenance window and ensure a current FileMaker backup exists.

While FileMaker still presents its initial self-signed certificate, run:

```bash
sudo /usr/bin/python3 \
  /opt/fms-le-dns/bin/fms-le-dns-deploy.py \
  --config /etc/fms-le-dns/fms-le-dns.conf \
  --bootstrap-insecure
```

`--bootstrap-insecure` is only for this initial transition. It connects to the local Admin API at `127.0.0.1` without validating the existing self-signed certificate. Do not use it after the trusted certificate is active.

The program performs these operations:

1. Validates the Certbot lineage.
2. Authenticates to Admin API v2 with a short-lived PKI assertion.
3. Imports `cert.pem`, `privkey.pem`, and `chain.pem`.
4. Requests a graceful Database Server stop.
5. Stops and starts all FileMaker Server processes through `fmshelper`.
6. Waits for the configured FQDN to present the expected trusted certificate.
7. Confirms from the FileMaker Event log that Database Server SSL network encryption is enabled.
8. Invalidates the Admin API session.

## 14. Verify the active service

```bash
curl --fail --silent --show-error \
  --output /dev/null \
  --write-out 'HTTPS status: %{http_code}\n' \
  "https://${FQDN}/fmi/admin/apidoc/"

openssl s_client \
  -connect "${FQDN}:443" \
  -servername "$FQDN" \
  -verify_return_error \
  </dev/null 2>/dev/null |
openssl x509 \
  -noout -subject -issuer -serial -dates -fingerprint -sha256

sudo /usr/sbin/service fmshelper status

sudo grep -F \
  'SECURITY: Secure (SSL) Network Encryption:' \
  "/opt/FileMaker/FileMaker Server/Logs/Event.log" |
tail -n 5
```

The newest Event log entry must end with:

```text
SECURITY: Secure (SSL) Network Encryption: Enabled
```

Open a hosted file from FileMaker Pro using the exact certificate FQDN. The client must show a verified secure lock; `Get(ConnectionState)` must return `3`.

Run the deployment program again without bootstrap mode. It should report that the target certificate is already active and make no changes:

```bash
sudo /usr/bin/python3 \
  /opt/fms-le-dns/bin/fms-le-dns-deploy.py \
  --config /etc/fms-le-dns/fms-le-dns.conf
```

## 15. Enable and test the deploy hook

Enable the hook only after successful first activation and external verification:

```bash
sudo /opt/fms-le-dns/bin/fms-le-dns-enable-hook
```

The helper refuses to enable the hook unless the latest FileMaker Event log state reports Database Server SSL network encryption as enabled.

Verify the installed hook:

```bash
sudo stat -c '%U:%G %a %n' \
  /etc/letsencrypt/renewal-hooks/deploy/fms-le-dns.sh
sudo bash -n /etc/letsencrypt/renewal-hooks/deploy/fms-le-dns.sh
```

Test renewal and hook discovery:

```bash
sudo certbot renew \
  --cert-name "$CERT_NAME" \
  --dry-run \
  --run-deploy-hooks
```

The dry run must succeed. When the production certificate is already active, the hook should take the idempotent no-op path and must not restart FileMaker.

## 16. Routine operation

Certbot's Snap timer owns renewal. Do not create a second timer or cron schedule.

Useful checks:

```bash
sudo systemctl status snap.certbot.renew.timer --no-pager
sudo systemctl status snap.certbot.renew.service --no-pager
sudo journalctl -u snap.certbot.renew.service
sudo tail -n 100 /var/log/letsencrypt/letsencrypt.log
sudo tail -n 100 /var/log/fms-le-dns.log
sudo certbot certificates
```

The deployment program uses `/run/fms-le-dns.lock` to reject concurrent runs. It writes normal messages to stdout, errors to stderr, and rotates `/var/log/fms-le-dns.log` at 1 MiB with five backups.

No external alert destination is built into the project. Integrate the non-zero Certbot service result or journal with the site's monitoring system when automated notification is required.

## 17. Updating the program

Review changes and take a protected backup first. From the new installation-kit directory, run:

```bash
sudo ./install.sh --update-code
```

This updates the deployment program, staged hook, and hook-enablement helper. It does not overwrite configuration, credentials, PKI keys, or the currently enabled hook.

After review, update the enabled hook by running:

```bash
sudo /opt/fms-le-dns/bin/fms-le-dns-enable-hook
```

Then repeat the read-only check and Certbot dry run with deploy hooks.

## 18. Backup and recovery

The recovery bundle contains private keys and credentials. Store it as `root:root 0600`, encrypt it before off-host transfer, and keep the decryption key separately according to organizational policy.

Create a local recovery bundle:

```bash
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/var/backups/fms-le-dns/fms-le-dns-${stamp}.tar.gz"

sudo install -d -o root -g root -m 0700 /var/backups/fms-le-dns
sudo tar --acls --xattrs --numeric-owner -C / -czf "${backup}.tmp" \
  opt/fms-le-dns \
  etc/fms-le-dns \
  etc/letsencrypt
sudo chown root:root "${backup}.tmp"
sudo chmod 0600 "${backup}.tmp"
sudo mv "${backup}.tmp" "$backup"
sudo tar -tzf "$backup" >/dev/null
sudo sha256sum "$backup"
```

Test extraction into an isolated directory before relying on the backup. Do not extract over the live filesystem during a test.

For recovery:

1. Stop scheduled renewal and FileMaker maintenance activity.
2. Install compatible FileMaker Server, Certbot, and plugin versions.
3. Verify the recovery-bundle checksum.
4. Extract as root at filesystem root, preserving numeric ownership, ACLs, attributes, and symlinks.
5. Confirm secret permissions before starting automation.
6. Confirm that the configured PKI public key is registered in FileMaker Admin Console.
7. Run `--check`, then a Certbot dry run.
8. Activate only through the supported Admin API and `fmshelper` workflow.
9. Verify HTTPS trust, hostname, fingerprint, FileMaker service state, and logs.

Never restore, replace, or edit FileMaker `CStore` directly.

## 19. Troubleshooting

| Symptom | Check | Response |
|---|---|---|
| DNS-01 authorization fails | Certbot log, token scope, zone selection, propagation delay | Correct the restricted token or DNS configuration and rerun a dry run. |
| PKI authentication fails | Clock, exact registered key name, key pair match, permissions | Correct or re-register the public key. Do not store an Admin Console password. |
| Certificate validation fails | SAN, dates, key match, chain, permissions | Correct the Certbot lineage before contacting FileMaker. |
| Import fails | Deployment log and Admin API result | Correct the input or API configuration; never edit `CStore`. |
| FileMaker does not return | `service fmshelper status`, journal, FileMaker Event log | Use supported `fmshelper` service control and investigate startup. |
| Old certificate remains visible | Compare Certbot and external SHA-256 fingerprints | Perform the full controlled activation; an Admin API Database Server stop/start alone is insufficient. |
| FileMaker Pro reports an unencrypted connection | Latest Event log SSL state and `fmsadmin get serverprefs` | Set `UseSecureConnection=true` interactively, restart FileMaker Server, and require the Event log to report `Enabled`. |
| Another deployment is running | Active process and `/run/fms-le-dns.lock` | Wait for the active run. Do not remove lock state without confirming no deployment is active. |
| Certbot renews but deployment fails | Both logs and externally presented certificate | Correct the deployment fault and rerun the program against the configured lineage. |

## 20. Acceptance checklist

- [ ] Configuration contains the intended FQDN, lineage, and PKI key name.
- [ ] All project files are root-owned with documented modes.
- [ ] Cloudflare token is zone-restricted and stored mode `0600`.
- [ ] PKI private/public fingerprints match.
- [ ] Public key is registered under the exact configured name.
- [ ] Production certificate SAN, dates, key, and chain validate.
- [ ] Read-only deployment check succeeds.
- [ ] `UseSecureConnection = true` is confirmed through `fmsadmin get serverprefs`.
- [ ] Controlled first activation succeeds during a maintenance window.
- [ ] Public HTTPS presents the expected trusted certificate.
- [ ] Latest Event log state reports Database Server SSL network encryption enabled.
- [ ] FileMaker Pro shows a verified secure lock and `Get(ConnectionState)` returns `3`.
- [ ] Normal deployment rerun is an idempotent no-op.
- [ ] Deploy hook is enabled only after first activation.
- [ ] Certbot dry run with deploy hooks succeeds.
- [ ] Snap renewal timer is enabled and active.
- [ ] Protected backup and isolated restoration test succeed.
- [ ] Off-host encrypted copy and monitoring responsibilities are assigned.
