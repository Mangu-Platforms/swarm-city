"""Async process boundary for bounded and cancellable git transactions."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from .config import get_settings


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        await process.wait()


async def run_git_transaction(
    final_output: str,
    task_name: str,
    *,
    expected_head: str | None,
    allow_high_risk_paths: bool,
    task_id: str | None,
) -> dict:
    """Run the synchronous git transaction in a killable child process."""

    settings = get_settings()
    payload = json.dumps(
        {
            "final_output": final_output,
            "task_name": task_name,
            "expected_head": expected_head,
            "allow_high_risk_paths": allow_high_risk_paths,
            "task_id": task_id,
        }
    ).encode("utf-8")
    # Resolve the package parent explicitly. Relying on the orchestrator's
    # ambient working directory to make `app` importable breaks the worker
    # whenever the process is started from anywhere else.
    package_parent = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    existing_path = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = (
        f"{package_parent}{os.pathsep}{existing_path}"
        if existing_path
        else str(package_parent)
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.git_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        cwd=str(package_parent),
        env=environment,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(payload),
            timeout=settings.git_operation_timeout_s + 10,
        )
    except asyncio.CancelledError:
        await _terminate(process)
        raise
    except TimeoutError:
        await _terminate(process)
        return {
            "applied": False,
            "errors": [
                "git transaction worker exceeded GIT_OPERATION_TIMEOUT_S and was terminated"
            ],
        }

    # The worker JSON can contain a bounded test-output tail plus metadata.
    # Keep the payload cap strict while allowing that envelope overhead.
    limit = settings.max_command_output_chars + 20_000
    if len(stdout) > limit:
        return {
            "applied": False,
            "errors": ["git worker response exceeded MAX_COMMAND_OUTPUT_CHARS"],
        }
    try:
        result = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError:
        detail = stderr.decode("utf-8", errors="replace")[-limit:]
        return {
            "applied": False,
            "errors": [f"git worker returned invalid JSON: {detail}"],
        }
    if not isinstance(result, dict):
        return {"applied": False, "errors": ["git worker returned a non-object"]}
    if process.returncode != 0 and not result.get("errors"):
        detail = stderr.decode("utf-8", errors="replace")[-limit:]
        result.setdefault("errors", []).append(
            f"git worker exited with {process.returncode}: {detail}"
        )
    return result
