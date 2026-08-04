"""Agent registry validation, expansion, and capability tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.agents import AgentRegistry
from app.config import get_settings


def _write_registry(path: Path, extra: str = "") -> None:
    path.write_text(
        f"""
agents:
  - role: draft
    model: generic
  - role: specialist_web
    model: web
    tags: [js, ts]
  - role: critic
    model: reviewer
  - role: security
    model: security-reviewer
  - role: finalizer
    model: final
{extra}
""",
        encoding="utf-8",
    )


def test_registry_adds_matching_specialist_and_distributes_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "agents.yaml"
    _write_registry(config)
    monkeypatch.setenv("AGENTS_CONFIG", str(config))
    monkeypatch.setenv("OLLAMA_URLS", "http://one:11434,http://two:11434")
    get_settings.cache_clear()

    registry = AgentRegistry()

    assert [agent.model for agent in registry.drafters("typescript")] == [
        "generic",
        "web",
    ]
    assert registry.local_finalizers()[0].model == "final"
    assert {agent.endpoint for agent in registry.agents} == {
        "http://one:11434",
        "http://two:11434",
    }
    required = registry.required_models_by_endpoint()
    assert set().union(*required.values()) == {
        "generic",
        "web",
        "reviewer",
        "security-reviewer",
        "final",
    }


def test_registry_expands_environment_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "agents.yaml"
    config.write_text(
        """
agents:
  - role: draft
    model: ${CODER_MODEL:-fallback-coder}
    count: ${DRAFT_COUNT:-2}
  - role: critic
    model: reviewer
  - role: security
    model: security-reviewer
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTS_CONFIG", str(config))
    monkeypatch.setenv("CODER_MODEL", "chosen-coder")
    monkeypatch.setenv("DRAFT_COUNT", "3")
    get_settings.cache_clear()

    registry = AgentRegistry()

    assert [agent.model for agent in registry.by_role("draft")] == [
        "chosen-coder",
        "chosen-coder",
        "chosen-coder",
    ]


def test_registry_fails_closed_without_security_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "agents.yaml"
    config.write_text(
        """
agents:
  - role: draft
    model: coder
  - role: critic
    model: reviewer
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTS_CONFIG", str(config))
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="security agent"):
        AgentRegistry()
