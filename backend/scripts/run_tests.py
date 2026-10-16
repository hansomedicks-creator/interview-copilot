"""Bounded async-runtime preflight, then isolated offline tests; never alters permissions."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
PROBE = "import asyncio; asyncio.run(asyncio.sleep(0)); print('ASYNC_RUNTIME_OK')"


def isolated_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("INTERVIEW_")}
    env.update(INTERVIEW_ENV="test", INTERVIEW_PROVIDER_MODE="mock", INTERVIEW_ASR_PROVIDER="disabled",
               INTERVIEW_DATABASE_URL="sqlite:///:memory:", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return env


def check_runtime(env: dict[str, str]) -> tuple[bool, str]:
    try:
        result = subprocess.run([sys.executable, "-u", "-c", PROBE], cwd=BACKEND, env=env,
                                capture_output=True, text=True, timeout=8)
    except subprocess.TimeoutExpired:
        return False, ("ASYNC_RUNTIME_BLOCKED: the Python event loop did not start within 8 seconds. "
                       "On Windows, blocked loopback/socketpair can cause this. Tests have NOT run. "
                       "Use an explicitly approved test command or your normal project terminal; "
                       "do not disable sandbox protections. See docs/windows-test-runtime.md.")
    if result.returncode or "ASYNC_RUNTIME_OK" not in result.stdout:
        return False, "ASYNC_RUNTIME_FAILED: " + (result.stderr or result.stdout)[-1800:]
    return True, "ASYNC_RUNTIME_OK"


def main(args: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if args is None else args)
    env = isolated_environment()
    ok, message = check_runtime(env)
    print(message, flush=True)
    if not ok:
        return 78
    if args == ["--check-only"]:
        return 0
    # Unique workspace-local temp paths avoid stale permissions and pytest global temp roots.
    # Keep the name short: nested Vault fixtures can exceed Windows MAX_PATH.
    temp = BACKEND / f".pytest-run-{uuid4().hex[:8]}"
    command = [sys.executable, "-m", "pytest", *args]
    if not any(arg.startswith("--basetemp") for arg in args):
        command.append(f"--basetemp={temp}")
    return subprocess.call(command, cwd=BACKEND, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
