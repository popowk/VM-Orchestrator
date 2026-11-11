"""Safely ensure a TLS certificate matching the exact Guest Gateway IPv4.

The legacy v1.3.0 CA key was not saved. IP changes therefore require a new
CA, and existing enrolled guests MUST be migrated manually. A certificate
rotation is automatically permitted only when no active Guest is enrolled.
"""
from __future__ import annotations
from pathlib import Path
import ipaddress
import sqlite3
import sys
import shutil
from datetime import datetime, timedelta, timezone
from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding

FILES = ('ca.pem', 'server.key', 'server.pem')


def validate_ipv4(ip_text: str):
    ip = ipaddress.ip_address(ip_text)
    if not isinstance(ip, ipaddress.IPv4Address) or not any(ip in net for net in (ipaddress.IPv4Network('10.0.0.0/8'), ipaddress.IPv4Network('172.16.0.0/12'), ipaddress.IPv4Network('192.168.0.0/16'))):
        raise ValueError('Guest Gateway requires a specific private IPv4 assigned to this Windows host')
    return ip


def cert_matches(cert_dir: Path, ip_text: str) -> bool:
    try:
        cert = x509.load_pem_x509_certificate((cert_dir / 'server.pem').read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        if validate_ipv4(ip_text) not in san.get_values_for_type(x509.IPAddress):
            return False
        if cert.not_valid_after_utc <= datetime.now(timezone.utc) + timedelta(days=7):
            return False
        private = serialization.load_pem_private_key((cert_dir / 'server.key').read_bytes(), password=None)
        ca = x509.load_pem_x509_certificate((cert_dir / 'ca.pem').read_bytes())
        # Reject legacy certificates missing extensions needed for strict TLS clients.
        for extension in (x509.BasicConstraints, x509.KeyUsage, x509.SubjectKeyIdentifier):
            ca.extensions.get_extension_for_class(extension)
        for extension in (x509.BasicConstraints, x509.KeyUsage, x509.AuthorityKeyIdentifier, x509.ExtendedKeyUsage):
            cert.extensions.get_extension_for_class(extension)
        if ca.not_valid_after_utc <= datetime.now(timezone.utc) + timedelta(days=7):
            return False
        ca.public_key().verify(cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(), cert.signature_hash_algorithm)
        return private.public_key().public_numbers() == cert.public_key().public_numbers()
    except Exception:
        return False


def active_guests(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    # Do not silently infer "no active guests" if the DB is unreadable.
    with sqlite3.connect('file:' + db_path.resolve().as_posix() + '?mode=ro', uri=True, timeout=5) as conn:
        present = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='guest_enrollment'").fetchone()
        if not present:
            return 0
        return conn.execute('SELECT COUNT(*) FROM guest_enrollment WHERE revoked_at IS NULL').fetchone()[0]


def generate(ip_text: str, output: Path):
    ip = validate_ipv4(ip_text)
    output.mkdir(parents=True, exist_ok=False)
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'VMO Private Guest CA')])
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca_usage = x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                             data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                             encipher_only=False, decipher_only=False)
    server_usage = x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=True,
                                 data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
                                 encipher_only=False, decipher_only=False)
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1))
          .not_valid_after(now+timedelta(days=720))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(ca_usage, critical=True)
          .add_extension(ca_ski, critical=False)
          .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'VMO Guest Gateway')])
    server = (x509.CertificateBuilder().subject_name(subject).issuer_name(name).public_key(key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1))
              .not_valid_after(now+timedelta(days=350))
              .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
              .add_extension(server_usage, critical=True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
              .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False)
              .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip)]), critical=False)
              .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
              .sign(ca_key, hashes.SHA256()))
    (output / 'ca.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (output / 'server.pem').write_bytes(server.public_bytes(serialization.Encoding.PEM))
    (output / 'server.key').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))


def ensure(ip_text: str, output: Path, db_path: Path) -> str:
    validate_ipv4(ip_text)
    output.mkdir(parents=True, exist_ok=True)
    present = [x for x in FILES if (output / x).exists()]
    if present and len(present) != len(FILES):
        raise RuntimeError('Incomplete TLS files. Restore a backup; do not overwrite a partial certificate set.')
    if len(present) == len(FILES) and cert_matches(output, ip_text):
        return 'Existing TLS certificate SAN matches the Gateway IP; CA preserved.'
    if present and active_guests(db_path):
        raise RuntimeError('TLS endpoint change would disconnect registered Guests. Migration required before certificate rotation; old CA must not be replaced automatically.')
    temp = output / ('.new-cert-' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f'))
    generate(ip_text, temp)
    if not cert_matches(temp, ip_text):
        shutil.rmtree(temp)
        raise RuntimeError('Generated TLS certificate failed IP SAN/key validation.')
    backup = None
    if present:
        backup = output / ('backup-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f'))
        backup.mkdir()
        for name in (*FILES, 'gateway-url.txt'):
            if (output / name).exists():
                shutil.move(str(output / name), str(backup / name))
    try:
        for name in FILES:
            shutil.move(str(temp / name), str(output / name))
        (output / 'gateway-url.txt').write_text('https://' + ip_text + ':8766\n', encoding='ascii')
    except Exception:
        for name in FILES:
            (output / name).unlink(missing_ok=True)
        if backup:
            for name in (*FILES, 'gateway-url.txt'):
                if (backup / name).exists():
                    shutil.move(str(backup / name), str(output / name))
        raise
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    if backup:
        return 'Rotated TLS CA+server certificate; old chain backed up to ' + str(backup) + '. Existing unregistered Guest configurations must be refreshed.'
    return 'Created TLS CA and IP-SAN server certificate for ' + ip_text


if __name__ == '__main__':
    if len(sys.argv) != 5 or sys.argv[1] != '--ensure':
        raise SystemExit('Usage: Create-GuestGatewayTLS.py --ensure <listen-ipv4> <tls-dir> <sqlite-db>')
    try:
        print(ensure(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])))
    except Exception as exc:
        print('TLS preparation BLOCKED: ' + str(exc), file=sys.stderr)
        raise SystemExit(2)
