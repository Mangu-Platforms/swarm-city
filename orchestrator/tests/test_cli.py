"""Host-side CLI configuration tests."""

from __future__ import annotations

import argparse
import http.server
import sys
import threading
from pathlib import Path

import pytest

from tools import swarm


def test_dotenv_value_is_literal_and_supports_export(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nexport SWARM_API_TOKEN='file-token'\nOTHER=$NOT_EXPANDED\n",
        encoding="utf-8",
    )

    assert swarm._dotenv_value("SWARM_API_TOKEN", env_file) == "file-token"
    assert swarm._dotenv_value("OTHER", env_file) == "$NOT_EXPANDED"
    assert swarm._dotenv_value("MISSING", env_file) is None


def test_process_environment_precedes_project_env(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SWARM_API_TOKEN=file-token\n", encoding="utf-8")
    monkeypatch.setattr(swarm, "_PROJECT_ENV_PATH", env_file)
    monkeypatch.delenv("SWARM_API_TOKEN", raising=False)
    assert swarm._environment_default("SWARM_API_TOKEN") == "file-token"

    monkeypatch.setenv("SWARM_API_TOKEN", "process-token")
    assert swarm._environment_default("SWARM_API_TOKEN") == "process-token"


class _RedirectingHandler(http.server.BaseHTTPRequestHandler):
    """Answers every request with a redirect to a different origin."""

    received: list[str | None] = []

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        _RedirectingHandler.received.append(self.headers.get("Authorization"))
        self.send_response(302)
        self.send_header("Location", "http://127.0.0.1:1/collect")
        self.end_headers()

    def log_message(self, *args) -> None:
        del args


def test_bearer_token_is_never_replayed_to_a_redirect_target() -> None:
    """urllib follows 3xx and replays Authorization; the client must not."""

    _RedirectingHandler.received.clear()
    server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        client = swarm.SwarmClient(f"http://{host}:{port}", "s3cret-swarm-token")
        with pytest.raises(swarm.ApiError, match="redirect"):
            client.request("GET", "/tasks")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert _RedirectingHandler.received == ["Bearer s3cret-swarm-token"]


def test_terminal_output_strips_escape_sequences(capsys, monkeypatch) -> None:
    hostile = "\x1b[2J\x1b]0;PWNED\x07\x1b]8;;http://evil.example\x07click\x1b]8;;\x07"
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)

    swarm._echo(hostile)

    printed = capsys.readouterr().out
    assert "\x1b" not in printed
    assert "\x07" not in printed
    assert "click" in printed


def test_output_file_is_not_destroyed_when_the_task_failed(tmp_path: Path) -> None:
    destination = tmp_path / "patch.diff"
    destination.write_text("previous patch\n", encoding="utf-8")
    args = argparse.Namespace(
        url="http://127.0.0.1:1",
        token="",
        json=False,
        output=str(destination),
        apply=False,
        no_wait=False,
        timeout=1,
    )

    exit_code = swarm._render_result(args, {"status": "error", "result": None})

    assert exit_code == 1
    assert destination.read_text(encoding="utf-8") == "previous patch\n"


def test_list_query_parameters_cannot_be_injected(monkeypatch) -> None:
    seen: list[str] = []

    class _Recorder(swarm.SwarmClient):
        def request(self, method, path, payload=None, extra_headers=None):
            del method, payload, extra_headers
            seen.append(path)
            return []

    monkeypatch.setattr(swarm, "_client", lambda args: _Recorder(args.url, args.token))
    args = argparse.Namespace(
        url="http://127.0.0.1:1",
        token="",
        limit=20,
        status="queued&limit=100000",
    )

    swarm.command_list(args)

    assert seen == ["/tasks?limit=20&status=queued%26limit%3D100000"]
