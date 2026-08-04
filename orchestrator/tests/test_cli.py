"""Host-side CLI configuration tests."""
from __future__ import annotations

from pathlib import Path

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
