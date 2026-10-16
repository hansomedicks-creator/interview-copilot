import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("test_runtime_runner", Path(__file__).parents[1] / "scripts" / "run_tests.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_runtime_timeout_does_not_start_pytest(monkeypatch, capsys):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 8
        raise subprocess.TimeoutExpired(args[0], 8)
    monkeypatch.setattr(runner.subprocess, "run", timeout)
    monkeypatch.setattr(runner.subprocess, "call", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not start tests")))
    assert runner.main(["-q"]) == 78
    assert "Tests have NOT run" in capsys.readouterr().out


def test_test_environment_does_not_inherit_production_settings(monkeypatch):
    monkeypatch.setenv("INTERVIEW_LLM_API_KEY", "synthetic-not-a-key")
    monkeypatch.setenv("INTERVIEW_DATABASE_URL", "sqlite:///production.db")
    env = runner.isolated_environment()
    assert "INTERVIEW_LLM_API_KEY" not in env
    assert env["INTERVIEW_DATABASE_URL"] == "sqlite:///:memory:"
    assert env["INTERVIEW_PROVIDER_MODE"] == "mock"


def test_runner_forwards_exit_code_and_uses_workspace_temp(monkeypatch):
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="ASYNC_RUNTIME_OK", stderr=""))
    commands = []
    def execute(command, **kw):
        commands.append(command)
        assert kw["cwd"] == runner.BACKEND
        return 1
    monkeypatch.setattr(runner.subprocess, "call", execute)
    assert runner.main(["-q"]) == 1
    assert commands[0][-1].startswith(f"--basetemp={runner.BACKEND}")
    assert runner.main(["--check-only"]) == 0
    assert len(commands) == 1
