# fms-le-dns

Automated Let's Encrypt certificate deployment for FileMaker Server on Linux, using Certbot, Cloudflare DNS-01, and FileMaker Admin API v2 PKI authentication.

`fms-le-dns` is designed for unattended renewal without storing a FileMaker Admin Console password and without using FileMaker scripts or schedules.

## What it does

After Certbot successfully renews a certificate, `fms-le-dns`:

1. Validates the certificate lineage, hostname, validity, key pair, chain, ownership, and permissions.
2. Detects when the target certificate is already active and exits without restarting FileMaker.
3. Authenticates to FileMaker Admin API v2 with a short-lived PKI assertion.
4. Imports the certificate, private key, and intermediate chain through the supported API.
5. Requests a graceful Database Server stop.
6. Restarts all FileMaker Server processes through the Linux `fmshelper` service.
7. Verifies that the configured hostname presents the expected trusted certificate.

## Security model

- No stored FileMaker administrator password.
- Dedicated RSA PKI identity with a root-only private key.
- Root-only Cloudflare API token with zone-restricted permissions.
- Root-owned configuration, scripts, and deployment hook.
- Short-lived FileMaker Admin API sessions.
- Input validation before FileMaker state changes.
- Runtime locking, bounded timeouts, recovery attempts, and secret-free logs.
- No direct access to or modification of FileMaker `CStore`.

## Supported environment

The validated design targets:

- Ubuntu 24.04 LTS on `x86_64`
- FileMaker Server 26 with Admin API v2
- Certbot installed from Snap
- Certbot Cloudflare DNS plugin
- A public DNS zone managed through Cloudflare

Revalidate the integration after major operating-system, FileMaker Server, Admin API, Certbot, or Cloudflare-plugin changes.

## Installation approach

Installation is deliberately staged. The installer does **not** issue a certificate, register the FileMaker public key, restart FileMaker, or enable the renewal hook.

The normal sequence is:

1. Install prerequisites.
2. Run the staged installer.
3. Register the generated public key in FileMaker Admin Console.
4. Request the initial certificate with the deploy hook disabled.
5. Run the read-only validation.
6. Perform the first controlled activation during a maintenance window.
7. Verify public HTTPS and FileMaker service health.
8. Enable and test the Certbot deploy hook.

Follow [MANUAL.md](MANUAL.md) for the complete procedure, validation commands, backup process, recovery instructions, and troubleshooting guidance.

## Quick start

Review the kit before running it:

```bash
bash -n install.sh
bash -n enable-deploy-hook.sh
bash -n fms-le-dns-hook.sh
```

Run the staged interactive installer:

```bash
chmod 0755 install.sh
sudo ./install.sh
```

The installer asks for the FileMaker hostname, Certbot certificate name, and Cloudflare token. Site-specific values are written to protected files on the target host; they are not embedded in this repository.

Do not enable the deploy hook until the initial certificate has been imported, activated, and externally verified. Continue with the public-key registration section in [MANUAL.md](MANUAL.md).

## Repository contents

| File | Purpose |
|---|---|
| `MANUAL.md` | Complete installation and operations manual |
| `install.sh` | Staged installer and code-update utility |
| `fms-le-dns-deploy.py` | Validation, Admin API import, restart, and verification program |
| `fms-le-dns-hook.sh` | Generic Certbot deploy hook |
| `enable-deploy-hook.sh` | Explicit post-verification hook activation helper |
| `fms-le-dns.conf.template` | Site-neutral configuration template |
| `LICENSE` | MIT License |

## Installed layout

```text
/opt/fms-le-dns/
├── bin/
│   ├── fms-le-dns-deploy.py
│   └── fms-le-dns-enable-hook
└── libexec/
    └── fms-le-dns-hook.sh

/etc/fms-le-dns/
├── fms-le-dns.conf
└── pki/
    ├── certbot.key
    └── certbot.key.pub

/etc/letsencrypt/
├── credentials/cloudflare.ini
└── renewal-hooks/deploy/fms-le-dns.sh
```

## Updating project code

Certificate renewal does not require a project-code update. Section 16 of the manual is used only when this repository's program or hook changes.

After reviewing a new release:

```bash
sudo ./install.sh --update-code
```

This preserves configuration, credentials, PKI keys, certificates, and the currently enabled hook. The updated staged hook is enabled separately after validation.

## Operational warning

Certificate activation restarts FileMaker Server processes and causes service interruption. Perform the initial activation and material integration changes during an approved maintenance window with current backups.

The software is provided without warranty. Test it in an environment representative of production and keep a documented recovery path.

## Project status

The implementation is based on a completed production deployment and validation of certificate issuance, PKI authentication, Admin API import, full FileMaker restart, external TLS verification, idempotent renewal handling, failure behavior, permissions, and recovery procedures.

## License

Licensed under the [MIT License](LICENSE).

FileMaker and Claris are trademarks of Claris International Inc. Let's Encrypt is a trademark of Internet Security Research Group. Cloudflare is a trademark of Cloudflare, Inc. This project is independent and is not affiliated with or endorsed by those organizations.
