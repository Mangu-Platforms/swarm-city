#!/usr/bin/env python3
"""Host-side CLI for the LLM Swarm coding API."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


_PROJECT_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# C0 and C1 control characters other than tab and newline. Model output and
# release-gate text are untrusted, and printing them raw lets a patch clear the
# screen, rewrite the terminal title, or render a deceptive OSC-8 hyperlink.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class ApiError(RuntimeError):
    """HTTP or protocol error returned by the swarm API."""


def _sanitize(text: str) -> str:
    return _CONTROL_CHARACTERS.sub("", text)


def _echo(text: str, *, stream=None) -> None:
    """Print server-supplied text, stripping escape sequences for a terminal."""

    target = stream or sys.stdout
    print(_sanitize(text) if target.isatty() else text, file=target)


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


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the bearer token is never re-sent to another host.

    urllib follows 3xx by default and replays every original header, including
    Authorization, at whatever Location names. A misconfigured --url or a
    plaintext hop is enough to hand the API token to a third party.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, msg, headers
        raise ApiError(f"server returned an unexpected redirect ({code}) to {newurl}")


class SwarmClient:
    """Small standard-library client for local and remote orchestrators."""

    def __init__(self, base_url: str, token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._opener = urllib.request.build_opener(_NoRedirects)

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
            with self._opener.open(request, timeout=30) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1).decode(
                    "utf-8",
                    errors="replace",
                )
        except urllib.error.HTTPError as exc:
            detail = exc.read(8192).decode("utf-8", errors="replace")
            raise ApiError(f"HTTP {exc.code}: {_sanitize(detail)}") from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"cannot reach {self.base_url}: {exc.reason}") from exc
        if len(raw.encode("utf-8", errors="replace")) > MAX_RESPONSE_BYTES:
            raise ApiError("server response exceeded the client size limit")
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError as exc:
            raise ApiError("server returned invalid JSON") from exc

    def request_object(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Request a response the caller may index into without guarding."""

        value = self.request(method, path, payload, extra_headers)
        if not isinstance(value, dict):
            raise ApiError(f"server returned {type(value).__name__}, expected an object")
        return value


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
        state = client.request_object("GET", _task_path(task_id))
        phase = str(state.get("phase", ""))
        if phase != previous_phase:
            _echo(f"[{state.get('status')}] {phase}", stream=sys.stderr)
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
    submitted = client.request_object("POST", "/tasks", payload, extra_headers=headers)
    task_id = submitted.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ApiError("server accepted the task but returned no task_id")
    if args.no_wait:
        _print_json(submitted)
        return 0

    return _render_result(args, _wait_for_task(client, task_id, args.timeout))


def _render_result(args: argparse.Namespace, state: dict[str, Any]) -> int:
    """Report a finished task and map its outcome to an exit code."""

    if args.json:
        _print_json(state)
    else:
        result = state.get("result") or {}
        final = result.get("final")
        if final:
            _echo(final)
        else:
            _print_json(state)
        gate = result.get("release_gate") or {}
        if gate:
            print(
                f"release_gate={gate.get('status')} score={gate.get('score')}",
                file=sys.stderr,
            )
            for reason in gate.get("reasons", []):
                _echo(f"  - {reason}", stream=sys.stderr)
        git_result = result.get("git") or {}
        if git_result:
            print(
                f"git_applied={git_result.get('applied')} "
                f"branch={git_result.get('branch')} "
                f"commit={git_result.get('commit')}",
                file=sys.stderr,
            )

    result = state.get("result") or {}
    if args.output:
        final = result.get("final")
        # Only write a real patch. Truncating the destination when the task
        # failed destroys whatever the caller already had there.
        if state.get("status") == "done" and isinstance(final, str) and final:
            destination = Path(args.output)
            temporary = destination.with_name(f"{destination.name}.{os.getpid()}.tmp")
            temporary.write_text(final, encoding="utf-8")
            temporary.replace(destination)
        else:
            print(
                f"no patch to write; {args.output} was left unchanged",
                file=sys.stderr,
            )

    if state.get("status") != "done":
        return 1
    gate_status = (result.get("release_gate") or {}).get("status")
    if gate_status not in {None, "ready"}:
        return 3
    if args.apply and not (result.get("git") or {}).get("applied"):
        return 4
    return 0


def _task_path(task_id: str) -> str:
    return f"/tasks/{urllib.parse.quote(task_id, safe='')}"


def command_status(args: argparse.Namespace) -> int:
    _print_json(_client(args).request("GET", _task_path(args.task_id)))
    return 0


def command_list(args: argparse.Namespace) -> int:
    query = {"limit": args.limit}
    if args.status:
        query["status"] = args.status
    _print_json(
        _client(args).request("GET", f"/tasks?{urllib.parse.urlencode(query)}")
    )
    return 0


def command_cancel(args: argparse.Namespace) -> int:
    _print_json(_client(args).request("DELETE", _task_path(args.task_id)))
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
    except Exception as exc:  # noqa: BLE001 - a client bug must not look like a
        # failed task, so it gets its own exit code rather than colliding with 1.
        print(f"internal client error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
