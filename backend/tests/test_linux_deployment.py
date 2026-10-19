"""Offline checks for credential handling and backup failure boundaries."""
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = spec_from_file_location('linux_deployment', ROOT / 'deploy/linux/manage.py')
deployment = module_from_spec(SPEC)
SPEC.loader.exec_module(deployment)


def configured(tmp_path):
    target = tmp_path / 'production.env'
    deployment.init_env(ROOT / 'deploy/linux/production.env.example', target)
    return deployment.read_env(target)


def test_environment_initialization_preserves_existing_secrets(tmp_path):
    target = tmp_path / 'production.env'
    template = ROOT / 'deploy/linux/production.env.example'
    assert deployment.init_env(template, target)
    original = target.read_bytes()
    config = deployment.read_env(target)
    assert config['INTERVIEW_ENV'] == 'production'
    assert len(config['INTERVIEW_SESSION_SECRET']) == 96
    assert len(deployment.pg_environment(config)['PGPASSWORD']) == 48
    assert not deployment.init_env(template, target)
    assert target.read_bytes() == original


def test_status_reports_presence_without_secret_values(tmp_path, capsys):
    config = configured(tmp_path)
    config['INTERVIEW_LLM_API_KEY'] = 'synthetic-not-a-real-key'
    status = deployment.configuration_status(config)
    output = capsys.readouterr().out
    assert 'INTERVIEW_LLM_API_KEY' in status['configured']
    assert 'synthetic-not-a-real-key' not in output
    assert deployment.pg_environment(config)['PGPASSWORD'] not in output
    assert not status['external_api_tests_performed']


@pytest.mark.parametrize('url', ['sqlite:///demo.db',
    'postgresql+psycopg://interview_copilot:fake@external.test/interview_copilot',
    'postgresql+psycopg://postgres:fake@127.0.0.1/interview_copilot',
    'postgresql+psycopg://interview_copilot:fake@127.0.0.1/another'])
def test_preparation_rejects_shared_or_external_database(url):
    with pytest.raises(ValueError):
        deployment.pg_environment({'INTERVIEW_DATABASE_URL': url})


def test_env_parser_keeps_dollar_characters_literal(tmp_path):
    target = tmp_path / 'quoted.env'
    target.write_text('FEISHU_APP_SECRET="fake $value # literal"\nEMPTY=\n', encoding='utf-8')
    assert deployment.read_env(target) == {'FEISHU_APP_SECRET': 'fake $value # literal', 'EMPTY': ''}


def test_failed_backup_does_not_publish_incomplete_snapshot(tmp_path, monkeypatch):
    config = configured(tmp_path)
    root = tmp_path / 'backups'
    state = tmp_path / 'state'
    (state / 'recordings').mkdir(parents=True)
    (state / 'knowledge').mkdir()
    monkeypatch.setattr(deployment.shutil, 'disk_usage', lambda _: SimpleNamespace(free=5 * 1024**3))
    monkeypatch.setattr(deployment, 'run', lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('failed')))
    with pytest.raises(RuntimeError):
        deployment.backup(config, root, state)
    assert not (root / 'latest').exists()
    assert not list(root.glob('*/manifest.json'))


def test_secret_error_output_does_not_expose_subprocess_stderr(monkeypatch):
    monkeypatch.setattr(deployment.subprocess, 'run',
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stderr=b'synthetic-password'))
    with pytest.raises(RuntimeError, match='failed') as error:
        deployment.run(['psql'])
    assert 'synthetic-password' not in str(error.value)


def test_smoke_checks_real_application_production_auth(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app

    settings = Settings(environment='production', provider_mode='mock',
        session_secret='synthetic-test-session-secret-no-real-users',
        database_url=f'sqlite:///{(tmp_path / "smoke.db").as_posix()}')
    with TestClient(create_app(settings=settings)) as client:
        def result(_base, path, *, post=False):
            response = client.post(path, json={}) if post else client.get(path)
            return response.status_code, response.content
        monkeypatch.setattr(deployment, 'http_result', result)
        assert all(deployment.smoke('http://127.0.0.1:8000').values())
    with pytest.raises(ValueError):
        deployment.smoke('http://public.example.test')
