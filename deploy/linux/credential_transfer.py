"""One-use, pinned-recipient CMS transfer. Never print plaintext or subprocess errors.

The control plane transports a public certificate and an encrypted envelope only.
Recipient authenticity comes from checking the fingerprint through the authenticated
server console, not from trusting this self-signed certificate as a web certificate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import time

REQUIRED = frozenset({
    'FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'FEISHU_ADMIN_OPEN_IDS',
    'INTERVIEW_LLM_API_KEY', 'TENCENT_ASR_APP_ID',
    'TENCENT_ASR_SECRET_ID', 'TENCENT_ASR_SECRET_KEY',
})
ALLOWED = REQUIRED | {
    'FEISHU_HR_OPEN_IDS', 'FEISHU_OAUTH_SCOPES',
    'INTERVIEW_LLM_BASE_URL', 'INTERVIEW_LLM_MODEL', 'INTERVIEW_LLM_PLANNING_MODEL',
    'INTERVIEW_ASR_ENGINE_MODEL_TYPE', 'TENCENT_ASR_HOTWORDS',
}
TRANSFER_ROOT = Path('/run/interview-copilot-transfer')
ENV_FILE = Path('/etc/interview-copilot/production.env')
DESTINATION = 'interview-copilot.ink@124.220.191.54'
MAX_BYTES = 32768


def openssl_run(executable: str, *args: str, data: bytes | None = None) -> bytes:
    result = subprocess.run([executable, *args], input=data, capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError('OpenSSL operation failed; private diagnostics suppressed')
    return result.stdout


def write_exclusive(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def certificate_fingerprint(cert: Path, executable: str) -> str:
    der = openssl_run(executable, 'x509', '-in', str(cert), '-outform', 'DER')
    return hashlib.sha256(der).hexdigest()


def create_recipient(directory: Path, executable: str) -> dict:
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    key, cert = directory / 'recipient.key', directory / 'recipient.pem'
    # Restrictive umask also applies while OpenSSL is creating the key.
    previous = os.umask(0o077)
    try:
        openssl_run(executable, 'req', '-x509', '-newkey', 'rsa:3072', '-nodes',
                    '-sha256', '-days', '1', '-subj', '/CN=Interview-Copilot-One-Use',
                    '-keyout', str(key), '-out', str(cert))
    finally:
        os.umask(previous)
    metadata = {'format': 1, 'destination': DESTINATION,
                'recipient_sha256': certificate_fingerprint(cert, executable),
                'expires_at': int(time.time()) + 3600}
    write_exclusive(directory / 'metadata.json', json.dumps(metadata).encode())
    return dict(metadata, transfer_dir=str(directory), certificate_pem=cert.read_text())


def validate_values(values: dict) -> dict[str, str]:
    if not isinstance(values, dict) or set(values) - ALLOWED:
        raise ValueError('Unexpected configuration fields')
    for value in values.values():
        if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Invalid configuration value')
    if any(not values.get(key, '').strip() for key in REQUIRED):
        raise ValueError('Required service configuration is missing')
    if values.get('INTERVIEW_LLM_BASE_URL', 'https://api.deepseek.com').rstrip('/') != 'https://api.deepseek.com':
        raise ValueError('Only the reviewed DeepSeek API destination is allowed')
    return values


def read_local_values(path: Path) -> dict[str, str]:
    """Match the local PowerShell launcher's raw KEY=value format, not shell eval."""
    values = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        key, sep, value = line.partition('=')
        key = key.strip()
        if sep and key in ALLOWED:
            if key in values:
                raise ValueError('Duplicate service configuration field')
            values[key] = value
    return validate_values(values)


def seal(source: Path, cert: Path, fingerprint: str, output: Path, executable: str) -> None:
    if not re.fullmatch('[a-f0-9]{64}', fingerprint):
        raise ValueError('A console-verified SHA-256 fingerprint is required')
    if certificate_fingerprint(cert, executable) != fingerprint:
        raise ValueError('Recipient fingerprint mismatch')
    openssl_run(executable, 'x509', '-in', str(cert), '-checkend', '0', '-noout')
    payload = json.dumps({'format': 1, 'destination': DESTINATION,
                          'recipient_sha256': fingerprint, 'created_at': int(time.time()),
                          'values': read_local_values(source)}, ensure_ascii=False).encode('utf-8')
    if len(payload) > MAX_BYTES:
        raise ValueError('Configuration exceeds transfer size limit')
    ciphertext = openssl_run(executable, 'cms', '-encrypt', '-binary', '-aes-256-gcm',
                             '-outform', 'DER', '-recip', str(cert),
                             '-keyopt', 'rsa_padding_mode:oaep',
                             '-keyopt', 'rsa_oaep_md:sha256', data=payload)
    write_exclusive(output, ciphertext)


def read_server_values(content: str) -> dict[str, str]:
    result = {}
    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not re.fullmatch('[A-Z][A-Z0-9_]*', key) or key in result:
            raise ValueError('Invalid or duplicate server setting')
        parts = shlex.split(value, comments=False)
        if len(parts) > 1:
            raise ValueError('Invalid server environment quoting')
        result[key] = parts[0] if parts else ''
    return result


def merge_environment(content: str, values: dict) -> str:
    validate_values(values)
    existing = read_server_values(content)
    if existing.get('INTERVIEW_ENV') != 'production':
        raise ValueError('Expected an existing production environment')
    lines = []
    for line in content.splitlines():
        key = line.partition('=')[0]
        if key not in values:
            lines.append(line)
    for key, value in sorted(values.items()):
        quoted = value.replace('\\', '\\\\').replace('"', '\\"')
        lines.append(f'{key}="{quoted}"')
    merged = '\n'.join(lines) + '\n'
    parsed = read_server_values(merged)
    if any(parsed.get(key) != value for key, value in existing.items() if key not in values):
        raise ValueError('An unrelated server setting would change')
    if any(parsed.get(key) != value for key, value in values.items()):
        raise ValueError('Configuration failed quoting round trip')
    return merged


def install(directory: Path, envelope: Path, target: Path, executable: str) -> None:
    metadata = json.loads((directory / 'metadata.json').read_text())
    now = int(time.time())
    if metadata['expires_at'] < now or (directory / 'used').exists():
        raise ValueError('Transfer expired or already consumed')
    if envelope.stat().st_size > MAX_BYTES + 4096:
        raise ValueError('Envelope too large')
    plaintext = openssl_run(executable, 'cms', '-decrypt', '-binary', '-inform', 'DER',
                            '-recip', str(directory / 'recipient.pem'),
                            '-inkey', str(directory / 'recipient.key'), data=envelope.read_bytes())
    payload = json.loads(plaintext)
    if (set(payload) != {'format', 'destination', 'recipient_sha256', 'created_at', 'values'}
            or payload['format'] != 1 or payload['destination'] != DESTINATION
            or payload['recipient_sha256'] != metadata['recipient_sha256']
            or certificate_fingerprint(directory / 'recipient.pem', executable) != metadata['recipient_sha256']
            or not isinstance(payload['created_at'], int)
            or not now - 3600 <= payload['created_at'] <= now + 60):
        raise ValueError('Envelope metadata rejected')
    if target.is_symlink() or not target.is_file():
        raise ValueError('Expected a regular server configuration file')
    original = target.read_bytes()
    merged = merge_environment(original.decode('utf-8'), payload['values'])
    suffix = secrets.token_hex(8)
    backup = target.with_name(target.name + '.before-transfer-' + suffix)
    pending = target.with_name(target.name + '.pending-' + suffix)
    write_exclusive(backup, original)
    # A one-use marker is committed first: an interrupted transfer must be re-created,
    # never retried with a potentially consumed recipient.
    write_exclusive(directory / 'used', b'one-use\n')
    try:
        write_exclusive(pending, merged.encode('utf-8'))
        pending.replace(target)
    finally:
        # Remove only the ephemeral private key created for this transfer. This is
        # unlink, not a claim of secure erasure; /run is normally volatile storage.
        (directory / 'recipient.key').unlink(missing_ok=True)
    print('SERVICE_CONFIGURATION_INSTALLED; original retained root-only; restart not performed')


def require_server_root() -> None:
    if os.name != 'posix' or os.geteuid() != 0:
        raise ValueError('This operation requires root on the server')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--openssl', default='openssl')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('init')
    command = commands.add_parser('seal')
    command.add_argument('--env-file', type=Path, required=True)
    command.add_argument('--certificate', type=Path, required=True)
    command.add_argument('--fingerprint', required=True)
    command.add_argument('--output', type=Path, required=True)
    command = commands.add_parser('install')
    command.add_argument('--transfer-dir', type=Path, required=True)
    command.add_argument('--envelope', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'seal':
        seal(args.env_file, args.certificate, args.fingerprint, args.output, args.openssl)
        print('ENCRYPTED_ENVELOPE_CREATED; plaintext not exported')
    else:
        require_server_root()
        if args.action == 'init':
            TRANSFER_ROOT.mkdir(mode=0o700, exist_ok=True)
            if (TRANSFER_ROOT.is_symlink() or TRANSFER_ROOT.stat().st_uid != 0
                    or TRANSFER_ROOT.stat().st_mode & 0o077):
                raise ValueError('Expected a root-only transfer parent')
            directory = TRANSFER_ROOT / secrets.token_hex(12)
            print(json.dumps(create_recipient(directory, args.openssl)))
        else:
            directory = args.transfer_dir
            if directory.is_symlink() or directory.resolve().parent != TRANSFER_ROOT.resolve():
                raise ValueError('Unexpected transfer directory')
            if any(path.stat().st_uid != 0 or path.stat().st_mode & 0o077
                   for path in (TRANSFER_ROOT, directory, directory / 'recipient.key', ENV_FILE)):
                raise ValueError('Server secrets must remain root-only')
            install(directory, args.envelope, ENV_FILE, args.openssl)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, RuntimeError, OSError, subprocess.TimeoutExpired):
        print('TRANSFER_FAILED; values and private diagnostics suppressed', file=sys.stderr)
        raise SystemExit(1)
