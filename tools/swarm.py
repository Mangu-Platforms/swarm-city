#!/usr/bin/env python3
"""Host-side CLI for the LLM Swarm coding API."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


_PROJECT_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


class ApiError(RuntimeError):
    """HTTP or protocol error returned by the swarm API."""


def _dotenv_value(name: str, path: Path | None = None) -> str | None:
    """Read one literal value from the project .env without shell expansion."""

    source = path or _PROJECT_ENV_PATH
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or key.strip() != name:
            continue
        normalized = value.strip()
        if (
            len(normalized) >= 2
            and normalized[0] == normalized[-1]
            and normalized[0] in {"'", '"'}
        ):
            normalized = normalized[1:-1]
        return normalized
    return None


def _environment_default(name: str, fallback: str = "") -> str:
    """Prefer the process environment, then the project .env, then fallback."""

    explicit = os.environ.get(name)
    if explicit is not None:
        return explicit
    from_file = _dotenv_value(name)
    return fallback if from_file is None else from_file


class SwarmClient:
    """Small standard-library client for local and remote orchestrators."""

    def __init__(self, base_url: str, token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ApiError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"cannot reach {self.base_url}: {exc.reason}") from exc
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError as exc:
            raise ApiError("server returned invalid JSON") from exc


def _client(args: argparse.Namespace) -> SwarmClient:
    return SwarmClient(args.url, args.token)


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _task_text(args: argparse.Namespace) -> str:
    if args.task_file:
        return Path(args.task_file).read_text(encoding="utf-8").strip()
    if args.task:
        return args.task.strip()
    raise ApiError("provide TASK or --task-file")


def _wait_for_task(client: SwarmClient, task_id: str, timeout: int) -> dict:
    started = time.monotonic()
    previous_phase = ""
    while True:
        state = client.request("GET", f"/tasks/{task_id}")
        phase = state.get("phase", "")
        if phase != previous_phase:
            print(f"[{state.get('status')}] {phase}", file=sys.stderr, flush=True)
            previous_phase = phase
        if state.get("status") in {"done", "error", "cancelled"}:
            return state
        if timeout > 0 and time.monotonic() - started > timeout:
            raise ApiError(f"timed out waiting for task {task_id}")
        time.sleep(1.5)


def command_task(args: argparse.Namespace) -> int:
    client = _client(args)
    payload = {
        "task": _task_text(args),
        "context_paths": args.context,
        "constraints": args.constraint,
        "language": args.language,
        "mode": args.mode,
        "auto_context": not args.no_auto_context,
        "seed": args.seed,
        "apply": args.apply,
        "expected_head": args.expected_head,
        "allow_high_risk_paths": args.approve_high_risk,
    }
    headers = (
        {"Idempotency-Key": args.idempotency_key}
        if args.idempotency_key
        else None
    )
    submitted = client.request("POST", "/tasks", payload, extra_headers=headers)
    task_id = submitted["task_id"]
    if args.no_wait:
        _print_json(submitted)
        return 0

    state = _wait_for_task(client, task_id, args.timeout)
    if args.json:
        _print_json(state)
    else:
        result = state.get("result", {})
        final = result.get("final")
        if final:
            print(final)
        else:
            _print_json(state)
        gate = result.get("release_gate", {})
        if gate:
            print(
                f"release_gate={gate.get('status')} score={gate.get('score')}",
                file=sys.stderr,
            )
            for reason in gate.get("reasons", []):
                print(f"  - {reason}", file=sys.stderr)
        git_result = result.get("git")
        if git_result:
            print(
                f"git_applied={git_result.get('applied')} "
                f"branch={git_result.get('branch')} "
                f"commit={git_result.get('commit')}",
                file=sys.stderr,
            )

    if args.output:
        final = state.get("result", {}).get("final", "")
        Path(args.output).write_text(final, encoding="utf-8")

    if state.get("status") != "done":
        return 1
    result = state.get("result", {})
    gate_status = result.get("release_gate", {}).get("status")
    if gate_status not in {None, "ready"}:
        return 3
    if args.apply and not result.get("git", {}).get("applied"):
        return 4
    return 0


def command_status(args: argparse.Namespace) -> int:
    _print_json(_client(args).request("GET", f"/tasks/{args.task_id}"))
    return 0


def command_list(args: argparse.Namespace) -> int:
    path = f"/tasks?limit={args.limit}"
    if args.status:
        path += f"&status={args.status}"
    _print_json(_client(args).request("GET", path))
    return 0


def command_cancel(args: argparse.Namespace) -> int:
    _print_json(_client(args).request("DELETE", f"/tasks/{args.task_id}"))
    return 0


def command_context(args: argparse.Namespace) -> int:
    payload = {
        "task": args.task,
        "context_paths": args.context,
        "language": args.language,
        "auto_context": not args.no_auto_context,
    }
    _print_json(_client(args).request("POST", "/context/preview", payload))
    return 0


def command_models(args: argparse.Namespace) -> int:
    _print_json(_client(args).request("GET", "/model-list"))
    return 0


def command_health(args: argparse.Namespace) -> int:
    result = _client(args).request("GET", "/readyz")
    _print_json(result)
    return 0 if result.get("ready") else 1


def command_version(args: argparse.Namespace) -> int:
    _print_json(_client(args).request("GET", "/version"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swarm",
        description="Submit repository-aware coding tasks to LLM Swarm.",
    )
    parser.add_argument(
        "--url",
        default=_environment_default("SWARM_URL", "http://localhost:8000"),
    )
    parser.add_argument(
        "--token",
        default=_environment_default("SWARM_API_TOKEN"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    task = subparsers.add_parser("task", help="submit a coding task")
    task.add_argument("task", nargs="?")
    task.add_argument("--task-file")
    task.add_argument("-C", "--context", action="append", default=[])
    task.add_argument("--constraint", action="append", default=[])
    task.add_argument("--language")
    task.add_argument(
        "--mode",
        choices=["auto", "build", "fix", "refactor", "review", "test", "docs"],
        default="auto",
    )
    task.add_argument("--seed", type=int)
    task.add_argument("--apply", action="store_true")
    task.add_argument(
        "--approve-high-risk",
        action="store_true",
        help="explicitly approve high-risk paths when used with --apply",
    )
    task.add_argument(
        "--expected-head",
        help="abort if the repository HEAD differs from this commit prefix",
    )
    task.add_argument(
        "--idempotency-key",
        help="reuse the existing task when an identical submission is retried",
    )
    task.add_argument("--no-auto-context", action="store_true")
    task.add_argument("--no-wait", action="store_true")
    task.add_argument("--timeout", type=int, default=1900)
    task.add_argument("--json", action="store_true")
    task.add_argument("-o", "--output")
    task.set_defaults(handler=command_task)

    status_parser = subparsers.add_parser("status", help="show one task")
    status_parser.add_argument("task_id")
    status_parser.set_defaults(handler=command_status)

    list_parser = subparsers.add_parser("list", help="list recent tasks")
    list_parser.add_argument("--status")
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.set_defaults(handler=command_list)

    cancel = subparsers.add_parser("cancel", help="cancel a task")
    cancel.add_argument("task_id")
    cancel.set_defaults(handler=command_cancel)

    context = subparsers.add_parser(
        "context",
        help="preview repository context selection and redactions",
    )
    context.add_argument("task")
    context.add_argument("-C", "--context", action="append", default=[])
    context.add_argument("--language")
    context.add_argument("--no-auto-context", action="store_true")
    context.set_defaults(handler=command_context)

    models = subparsers.add_parser("models", help="show agents and model readiness")
    models.set_defaults(handler=command_models)

    health = subparsers.add_parser("health", help="check full model readiness")
    health.set_defaults(handler=command_health)

    version = subparsers.add_parser("version", help="show API version")
    version.set_defaults(handler=command_version)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "approve_high_risk", False) and not getattr(args, "apply", False):
        parser.error("--approve-high-risk requires --apply")
    try:
        return int(args.handler(args))
    except (ApiError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
