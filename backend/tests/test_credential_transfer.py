"""Synthetic-only transfer validation; real credentials must never enter tests."""
from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = spec_from_file_location('credential_transfer', ROOT / 'deploy/linux/credential_transfer.py')
transfer = module_from_spec(SPEC)
SPEC.loader.exec_module(transfer)


def values():
    return {key: 'synthetic-not-real-' + key for key in transfer.REQUIRED}


def test_local_export_is_allowlisted_and_does_not_import_server_state(tmp_path):
    env = tmp_path / '.env.local'
    config = values() | {'INTERVIEW_DATABASE_URL': 'sqlite:///private.db',
                         'INTERVIEW_SESSION_SECRET': 'private-session',
                         'INTERVIEW_KNOWLEDGE_VAULT_DIR': 'D:/private'}
    env.write_text('\n'.join(f'{key}={value}' for key, value in config.items()), encoding='utf-8')
    exported = transfer.read_local_values(env)
    assert exported == values()
    assert 'private-session' not in str(exported)


@pytest.mark.parametrize('extra', [
    {'INTERVIEW_DATABASE_URL': 'replacement'},
    {'FEISHU_APP_SECRET': 'value\nINJECTED=true'},
    {'INTERVIEW_LLM_BASE_URL': 'https://other.example.test'},
    {'FEISHU_ADMIN_OPEN_IDS': ''},
])
def test_rejects_unreviewed_fields_endpoints_and_injection(extra):
    with pytest.raises(ValueError):
        transfer.validate_values(values() | extra)


def test_merge_preserves_database_session_paths_and_quoting():
    original = ('# server-local\nINTERVIEW_ENV=production\n'
                'INTERVIEW_DATABASE_URL=postgresql+psycopg://local-secret\n'
                'INTERVIEW_SESSION_SECRET=server-secret\n'
                'INTERVIEW_RECORDING_DIR=/var/lib/interview-copilot/recordings\n'
                'FEISHU_APP_SECRET=\n')
    incoming = values() | {'TENCENT_ASR_HOTWORDS': '热词 "quoted" $literal # \\value'}
    merged = transfer.read_server_values(transfer.merge_environment(original, incoming))
    assert all(merged[key] == value for key, value in incoming.items())
    assert merged['INTERVIEW_DATABASE_URL'] == 'postgresql+psycopg://local-secret'
    assert merged['INTERVIEW_SESSION_SECRET'] == 'server-secret'
    assert merged['INTERVIEW_RECORDING_DIR'] == '/var/lib/interview-copilot/recordings'
    with pytest.raises(ValueError):
        transfer.merge_environment('INTERVIEW_ENV=development\n', incoming)


def test_openssl_cms_roundtrip_tampering_pinning_and_one_use(tmp_path, capsys):
    executable = os.getenv('COPILOT_TEST_OPENSSL') or shutil.which('openssl')
    if not executable:
        pytest.skip('OpenSSL executable unavailable; no crypto integration claim')
    directory = tmp_path / 'recipient'
    metadata = transfer.create_recipient(directory, executable)
    local = tmp_path / 'local.env'
    local.write_text('\n'.join(f'{key}={value}' for key, value in values().items()), encoding='utf-8')
    cert = directory / 'recipient.pem'
    envelope = tmp_path / 'encrypted.cms'
    with pytest.raises(ValueError, match='fingerprint'):
        transfer.seal(local, cert, '0' * 64, envelope, executable)
    transfer.seal(local, cert, metadata['recipient_sha256'], envelope, executable)
    assert b'synthetic-not-real' not in envelope.read_bytes()
    original = 'INTERVIEW_ENV=production\nINTERVIEW_SESSION_SECRET=server-only\n'
    target = tmp_path / 'production.env'
    target.write_text(original, encoding='utf-8')
    damaged = tmp_path / 'damaged.cms'
    data = bytearray(envelope.read_bytes())
    data[-1] ^= 1
    damaged.write_bytes(data)
    with pytest.raises(RuntimeError):
        transfer.install(directory, damaged, target, executable)
    assert target.read_text() == original
    transfer.install(directory, envelope, target, executable)
    assert transfer.read_server_values(target.read_text())['FEISHU_APP_SECRET'] == values()['FEISHU_APP_SECRET']
    assert not (directory / 'recipient.key').exists()
    assert list(tmp_path.glob('production.env.before-transfer-*'))[0].read_text() == original
    with pytest.raises(ValueError, match='consumed'):
        transfer.install(directory, envelope, target, executable)
    assert 'synthetic-not-real' not in capsys.readouterr().out
