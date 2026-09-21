#!/usr/bin/env python3
"""Deploy a Certbot certificate to FileMaker Server through Admin API v2."""

from __future__ import annotations

import argparse
import configparser
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time
from typing import Any

import jwt
import requests
import urllib3
from cryptography import x509
from cryptography.hazmat.primitives import serialization


DEFAULT_CONFIG_PATH = Path("/etc/fms-le-dns/fms-le-dns.conf")
LOCK_PATH = Path("/run/fms-le-dns.lock")
SERVICE_COMMAND = "/usr/sbin/service"
LOG_MAX_BYTES = 1_048_576
LOG_BACKUP_COUNT = 5


class DeploymentError(RuntimeError):
    """A deployment step failed."""


@dataclass(frozen=True)
class Settings:
    fqdn: str
    pki_key_name: str
    pki_key_path: Path
    lineage: Path
    service_name: str
    event_log_path: Path
    log_path: Path
    request_timeout: int
    state_timeout: int
    verify_timeout: int
    poll_interval: int
    grace_time: int
    service_timeout: int

    @property
    def production_api_url(self) -> str:
        return f"https://{self.fqdn}/fmi/admin/api/v2"

    @property
    def bootstrap_api_url(self) -> str:
        return "https://127.0.0.1/fmi/admin/api/v2"


def load_settings(path: Path) -> Settings:
    if path.is_symlink() or not path.is_file():
        raise DeploymentError(f"missing configuration file: {path}")
    file_stat = path.stat()
    if file_stat.st_uid != 0 or file_stat.st_mode & 0o077:
        raise DeploymentError(f"configuration must be root-owned and mode 0600: {path}")

    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open("r", encoding="utf-8") as config_file:
            parser.read_file(config_file)
        fqdn = parser.get("server", "fqdn").strip().rstrip(".").lower()
        fqdn.encode("idna")
        if not fqdn or "." not in fqdn or any(char.isspace() for char in fqdn):
            raise ValueError("fqdn must be a fully qualified domain name")

        settings = Settings(
            fqdn=fqdn,
            pki_key_name=parser.get("server", "pki_key_name").strip(),
            pki_key_path=Path(parser.get("server", "pki_key_path")),
            lineage=Path(parser.get("server", "lineage")),
            service_name=parser.get("server", "service_name", fallback="fmshelper"),
            event_log_path=Path(
                parser.get(
                    "server",
                    "event_log_path",
                    fallback="/opt/FileMaker/FileMaker Server/Logs/Event.log",
                )
            ),
            log_path=Path(parser.get("logging", "path", fallback="/var/log/fms-le-dns.log")),
            request_timeout=parser.getint("timeouts", "request", fallback=30),
            state_timeout=parser.getint("timeouts", "state", fallback=300),
            verify_timeout=parser.getint("timeouts", "verify", fallback=300),
            poll_interval=parser.getint("timeouts", "poll", fallback=5),
            grace_time=parser.getint("timeouts", "grace", fallback=60),
            service_timeout=parser.getint("timeouts", "service", fallback=180),
        )
    except (configparser.Error, KeyError, ValueError) as exc:
        raise DeploymentError(f"invalid configuration file {path}: {exc}") from exc

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", settings.pki_key_name):
        raise DeploymentError(f"invalid pki_key_name in {path}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@._-]*", settings.service_name):
        raise DeploymentError(f"invalid service_name in {path}")
    for value_name, value in (
        ("pki_key_path", settings.pki_key_path),
        ("lineage", settings.lineage),
        ("event_log_path", settings.event_log_path),
        ("log_path", settings.log_path),
    ):
        if not value.is_absolute():
            raise DeploymentError(f"{value_name} must be an absolute path")
    for value_name, value in (
        ("request timeout", settings.request_timeout),
        ("state timeout", settings.state_timeout),
        ("verify timeout", settings.verify_timeout),
        ("poll interval", settings.poll_interval),
        ("grace time", settings.grace_time),
        ("service timeout", settings.service_timeout),
    ):
        if value <= 0:
            raise DeploymentError(f"{value_name} must be positive")
    return settings


class BelowErrorFilter(logging.Filter):
    """Route INFO and WARNING records away from stderr."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.ERROR


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import and activate a Certbot certificate in FileMaker Server."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="root-owned configuration file",
    )
    parser.add_argument(
        "--lineage",
        type=Path,
        help="Certbot lineage directory; defaults to the configured lineage",
    )
    parser.add_argument(
        "--from-hook",
        action="store_true",
        help="Skip unrelated Certbot lineages successfully",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate inputs only; do not contact FileMaker or change state",
    )
    parser.add_argument(
        "--activate-only",
        action="store_true",
        help=(
            "Skip certificate import and activate the certificate already imported "
            "into FileMaker. Intended for the controlled initial activation only."
        ),
    )
    parser.add_argument(
        "--bootstrap-insecure",
        action="store_true",
        help=(
            "One-time bootstrap mode for the current self-signed certificate. "
            "Uses localhost and disables Admin API TLS verification."
        ),
    )
    return parser.parse_args()


def acquire_lock() -> Any:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = LOCK_PATH.open("w", encoding="ascii")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise DeploymentError("another fms-le-dns deployment is already running") from exc
    lock_file.write(f"{os.getpid()}\n")
    lock_file.flush()
    return lock_file


def require_root() -> None:
    if os.geteuid() != 0:
        raise DeploymentError("must run as root")


def configure_logging(log_path: Path) -> None:
    os.umask(0o077)
    if log_path.is_symlink():
        raise DeploymentError(f"log path must not be a symlink: {log_path}")
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.addFilter(BelowErrorFilter())
    stdout_handler.setFormatter(formatter)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.ERROR)
    stderr_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    os.chmod(log_path, 0o600)
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(stdout_handler)
    root_logger.addHandler(stderr_handler)
    root_logger.addHandler(file_handler)


def certificate_paths(lineage: Path) -> dict[str, Path]:
    return {
        "cert": lineage / "cert.pem",
        "key": lineage / "privkey.pem",
        "chain": lineage / "chain.pem",
    }


def validate_inputs(
    settings: Settings, lineage: Path
) -> tuple[dict[str, Path], bytes, str]:
    paths = certificate_paths(lineage)
    for label, path in paths.items():
        if not path.is_file():
            raise DeploymentError(f"missing {label} file: {path}")

    if settings.pki_key_path.is_symlink() or not settings.pki_key_path.is_file():
        raise DeploymentError(f"missing PKI private key: {settings.pki_key_path}")

    for secret_path in (paths["key"], settings.pki_key_path):
        secret_stat = secret_path.stat()
        if secret_stat.st_uid != 0:
            raise DeploymentError(f"secret is not owned by root: {secret_path}")
        if secret_stat.st_mode & 0o077:
            raise DeploymentError(f"secret has group or other permissions: {secret_path}")

    cert_bytes = paths["cert"].read_bytes()
    cert = x509.load_pem_x509_certificate(cert_bytes)
    private_key = serialization.load_pem_private_key(
        paths["key"].read_bytes(), password=None
    )

    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound as exc:
        raise DeploymentError("certificate has no Subject Alternative Name") from exc

    if settings.fqdn not in dns_names:
        raise DeploymentError(
            f"certificate SAN does not contain configured FQDN {settings.fqdn}"
        )

    now = datetime.now(timezone.utc)
    not_before = cert.not_valid_before.replace(tzinfo=timezone.utc)
    not_after = cert.not_valid_after.replace(tzinfo=timezone.utc)
    if now < not_before:
        raise DeploymentError("certificate is not valid yet")
    if now >= not_after:
        raise DeploymentError("certificate has expired")

    cert_public = cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_public = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if cert_public != key_public:
        raise DeploymentError("certificate and private key do not match")

    verify = subprocess.run(
        [
            "/usr/bin/openssl",
            "verify",
            "-untrusted",
            str(paths["chain"]),
            "-CApath",
            "/etc/ssl/certs",
            str(paths["cert"]),
        ],
        capture_output=True,
        text=True,
        timeout=settings.request_timeout,
        check=False,
    )
    if verify.returncode != 0:
        detail = (verify.stderr or verify.stdout).strip()
        raise DeploymentError(f"certificate chain verification failed: {detail}")

    cert_der = cert.public_bytes(serialization.Encoding.DER)
    fingerprint = hashlib.sha256(cert_der).hexdigest()
    return paths, cert_der, fingerprint


def presented_certificate(settings: Settings) -> bytes:
    context = ssl._create_unverified_context()
    with socket.create_connection(
        (settings.fqdn, 443), timeout=settings.request_timeout
    ) as raw_socket:
        with context.wrap_socket(
            raw_socket, server_hostname=settings.fqdn
        ) as tls_socket:
            return tls_socket.getpeercert(binary_form=True)


class AdminAPI:
    def __init__(self, settings: Settings, bootstrap_insecure: bool) -> None:
        self.settings = settings
        self.base_url = (
            settings.bootstrap_api_url
            if bootstrap_insecure
            else settings.production_api_url
        )
        self.verify_tls = not bootstrap_insecure
        self.session = requests.Session()
        self.access_token: str | None = None

        if bootstrap_insecure:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def authenticate(self) -> None:
        now = int(time.time())
        pki_token = jwt.encode(
            {
                "iss": self.settings.pki_key_name,
                "aud": "fmsadminapi",
                "iat": now,
                "exp": now + 300,
            },
            self.settings.pki_key_path.read_bytes(),
            algorithm="RS256",
        )
        response = self.session.post(
            f"{self.base_url}/user/auth",
            headers={"Authorization": f"PKI {pki_token}"},
            timeout=self.settings.request_timeout,
            verify=self.verify_tls,
        )
        body = self._validated_json(response, "PKI authentication")
        try:
            self.access_token = body["response"]["token"]
        except (KeyError, TypeError) as exc:
            raise DeploymentError("authentication response contained no token") from exc

    def headers(self, content_type: str | None = None) -> dict[str, str]:
        if not self.access_token:
            raise DeploymentError("Admin API session is not authenticated")
        headers = {"Authorization": f"Bearer {self.access_token}"}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def import_certificate(self, paths: dict[str, Path]) -> None:
        with (
            paths["cert"].open("rb") as cert_file,
            paths["key"].open("rb") as key_file,
            paths["chain"].open("rb") as chain_file,
        ):
            response = self.session.patch(
                f"{self.base_url}/server/certificate/importcertfiles",
                headers=self.headers(),
                files={
                    "certfile": ("cert.pem", cert_file, "application/x-pem-file"),
                    "keyfile": ("privkey.pem", key_file, "application/x-pem-file"),
                    "intermediatefile": (
                        "chain.pem",
                        chain_file,
                        "application/x-pem-file",
                    ),
                },
                timeout=max(60, self.settings.request_timeout),
                verify=self.verify_tls,
            )
        self._validated_json(response, "certificate import")

    def get_status(self) -> str:
        response = self.session.get(
            f"{self.base_url}/server/status",
            headers=self.headers(),
            timeout=self.settings.request_timeout,
            verify=self.verify_tls,
        )
        body = self._validated_json(response, "get server status")
        try:
            return str(body["response"]["status"])
        except (KeyError, TypeError) as exc:
            raise DeploymentError("server status response contained no status") from exc

    def set_status(self, status: str, grace_time: int = 0) -> None:
        payload: dict[str, Any] = {"status": status}
        if status == "STOPPED":
            payload.update(
                {
                    "messageText": "TLS certificate maintenance",
                    "graceTime": grace_time,
                }
            )
        response = self.session.patch(
            f"{self.base_url}/server/status",
            headers=self.headers("application/json"),
            json=payload,
            timeout=self.settings.request_timeout,
            verify=self.verify_tls,
        )
        self._validated_json(response, f"set server status to {status}")

    def wait_for_status(self, expected: str) -> None:
        deadline = time.monotonic() + self.settings.state_timeout
        last_status = "unknown"
        while time.monotonic() < deadline:
            try:
                last_status = self.get_status()
                if last_status == expected:
                    return
            except (requests.RequestException, DeploymentError) as exc:
                logging.info("waiting for Database Server %s: %s", expected, exc)
            time.sleep(self.settings.poll_interval)
        raise DeploymentError(
            f"Database Server did not reach {expected}; last status was {last_status}"
        )

    def close(self) -> None:
        if self.access_token:
            try:
                self.session.delete(
                    f"{self.base_url}/user/auth/{self.access_token}",
                    headers=self.headers(),
                    timeout=self.settings.request_timeout,
                    verify=self.verify_tls,
                )
            except requests.RequestException as exc:
                logging.warning("could not invalidate Admin API session: %s", exc)
        self.access_token = None
        self.session.close()

    @staticmethod
    def _validated_json(response: requests.Response, operation: str) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise DeploymentError(
                f"{operation} returned HTTP {response.status_code} with non-JSON data"
            ) from exc

        messages = body.get("messages", [])
        message_text = "; ".join(
            f"{item.get('code')}: {item.get('text')}" for item in messages
        )
        if response.status_code != 200 or any(
            str(item.get("code")) != "0" for item in messages
        ):
            raise DeploymentError(
                f"{operation} failed with HTTP {response.status_code}: {message_text}"
            )
        return body


def wait_for_presented_certificate(settings: Settings, expected_der: bytes) -> None:
    expected_fingerprint = hashlib.sha256(expected_der).hexdigest()
    deadline = time.monotonic() + settings.verify_timeout
    last_error = "no certificate received"

    while time.monotonic() < deadline:
        try:
            actual_der = presented_certificate(settings)
            actual_fingerprint = hashlib.sha256(actual_der).hexdigest()
            if actual_fingerprint == expected_fingerprint:
                context = ssl.create_default_context()
                with socket.create_connection(
                    (settings.fqdn, 443), timeout=settings.request_timeout
                ) as raw_socket:
                    with context.wrap_socket(
                        raw_socket, server_hostname=settings.fqdn
                    ):
                        pass
                return
            last_error = f"unexpected SHA-256 fingerprint {actual_fingerprint}"
        except (OSError, ssl.SSLError) as exc:
            last_error = str(exc)
        time.sleep(settings.poll_interval)

    raise DeploymentError(
        "new certificate was not presented and trusted before timeout: " + last_error
    )


def control_filemaker_service(settings: Settings, action: str) -> None:
    if action not in {"start", "stop"}:
        raise DeploymentError(f"unsupported fmshelper action: {action}")

    result = subprocess.run(
        [SERVICE_COMMAND, settings.service_name, action],
        capture_output=True,
        text=True,
        timeout=settings.service_timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise DeploymentError(
            f"{settings.service_name} {action} failed with exit "
            f"{result.returncode}: {detail}"
        )


def verify_database_ssl_enabled(settings: Settings) -> None:
    if not settings.event_log_path.is_file():
        raise DeploymentError(
            f"missing FileMaker Event log: {settings.event_log_path}"
        )

    marker = "SECURITY: Secure (SSL) Network Encryption:"
    latest = ""
    with settings.event_log_path.open("r", encoding="utf-8", errors="replace") as log:
        for line in log:
            if marker in line:
                latest = line.strip()

    if not latest:
        raise DeploymentError(
            "FileMaker Event log contains no Database Server SSL state"
        )
    if not latest.endswith("Enabled"):
        raise DeploymentError(
            "Database Server SSL is not enabled; set FileMaker server preference "
            "UseSecureConnection=true and restart FileMaker Server"
        )
    logging.info("verified Database Server SSL network encryption is enabled")


def main() -> int:
    args = parse_args()
    require_root()
    settings = load_settings(args.config)
    configure_logging(settings.log_path)
    lock_file = acquire_lock()
    lineage = args.lineage or settings.lineage
    if args.from_hook and os.path.abspath(lineage) != os.path.abspath(settings.lineage):
        logging.info("renewed lineage %s is not configured; skipping", lineage)
        return 0
    logging.info("validating Certbot lineage %s", lineage)
    paths, cert_der, expected_fingerprint = validate_inputs(settings, lineage)
    logging.info("validated target certificate SHA-256 %s", expected_fingerprint)

    if args.check:
        logging.info("check completed; no FileMaker state was changed")
        return 0

    try:
        current_fingerprint = hashlib.sha256(
            presented_certificate(settings)
        ).hexdigest()
        if current_fingerprint == expected_fingerprint:
            verify_database_ssl_enabled(settings)
            logging.info("target certificate is already active; nothing to do")
            return 0
    except (OSError, ssl.SSLError) as exc:
        logging.warning("could not inspect current TLS certificate: %s", exc)

    api = AdminAPI(settings, args.bootstrap_insecure)
    database_stopped = False
    service_stop_attempted = False
    try:
        logging.info("authenticating to FileMaker Admin API")
        api.authenticate()
        if args.activate_only:
            logging.info("activation-only mode; using certificate already imported")
        else:
            logging.info("importing certificate files")
            api.import_certificate(paths)
        logging.info(
            "requesting Database Server STOPPED with %s-second grace",
            settings.grace_time,
        )
        api.set_status("STOPPED", settings.grace_time)
        api.wait_for_status("STOPPED")
        database_stopped = True
        api.close()

        logging.info("stopping all FileMaker Server processes through fmshelper")
        service_stop_attempted = True
        control_filemaker_service(settings, "stop")
        logging.info("starting all FileMaker Server processes through fmshelper")
        control_filemaker_service(settings, "start")
        database_stopped = False
        service_stop_attempted = False
    finally:
        api.close()
        if database_stopped or service_stop_attempted:
            logging.warning(
                "deployment failed after Database Server stop; attempting fmshelper start"
            )
            try:
                control_filemaker_service(settings, "start")
            except Exception as recovery_error:  # noqa: BLE001
                logging.error("fmshelper recovery start failed: %s", recovery_error)

    logging.info("verifying externally presented certificate")
    wait_for_presented_certificate(settings, cert_der)
    verify_database_ssl_enabled(settings)
    logging.info("certificate deployment completed successfully")
    del lock_file
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DeploymentError, requests.RequestException, OSError, ValueError) as exc:
        logging.error("deployment failed: %s", exc)
        raise SystemExit(1)
